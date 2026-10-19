"""Owned, atomic transitions for authorized synthesis jobs; no provider calls here."""

from __future__ import annotations

from typing import Any
from uuid import UUID
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update, func, or_, and_, case
from sqlalchemy.orm import Session

from db.tables import get_table


def require_synthesis_schema() -> None:
    runs = get_table("cortex_analysis_runs")
    intents = get_table("compare_analysis_intents")
    if not {"structured_result", "context_snapshot"} <= set(runs.c.keys()):
        raise RuntimeError("Apply 20261016_add_compare_synthesis.sql and restart the API")
    if not {"reservation_payload", "progress_stage"} <= set(intents.c.keys()):
        raise RuntimeError("Apply 20261016_add_compare_synthesis.sql and restart the API")


def get_intent(db: Session, user_id: UUID, group_id: UUID) -> dict[str, Any] | None:
    table = get_table("compare_analysis_intents")
    row = (
        db.execute(
            select(table).where(
                table.c.user_id == user_id,
                table.c.request_group_id == group_id,
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def create_intent(db: Session, **values: Any) -> None:
    db.execute(insert(get_table("compare_analysis_intents")).values(**values))


def transition(
    db: Session, user_id: UUID, group_id: UUID, *, expected: tuple[str, ...], **values: Any
) -> bool:
    table = get_table("compare_analysis_intents")
    row = db.execute(
        update(table)
        .where(
            table.c.user_id == user_id,
            table.c.request_group_id == group_id,
            table.c.state.in_(expected),
        )
        .values(**values, updated_at=func.now())
        .returning(table.c.request_group_id)
    ).first()
    return row is not None


def pending_intents(db: Session) -> list[dict[str, Any]]:
    table = get_table("compare_analysis_intents")
    requests = get_table("llm_requests")
    has_answers = (
        select(requests.c.id)
        .where(
            requests.c.user_id == table.c.user_id,
            requests.c.request_group_id == table.c.request_group_id,
        )
        .exists()
    )
    return [
        dict(row)
        for row in db.execute(
            select(table)
            .where(
                or_(table.c.state == "queued", and_(table.c.state == "waiting", has_answers)),
            )
            .order_by(case((table.c.state == "queued", 0), else_=1), table.c.updated_at)
            .limit(20)
        ).mappings()
    ]


def expire_interrupted(db: Session) -> None:
    """Never replay a provider call with an unknown outcome after interruption."""
    table = get_table("compare_analysis_intents")
    db.execute(
        update(table)
        .where(
            table.c.state == "processing",
            table.c.updated_at < datetime.now(timezone.utc) - timedelta(minutes=15),
        )
        .values(state="failed", error_code="analysis_interrupted", updated_at=func.now())
    )
