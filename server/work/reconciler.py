"""Browser-independent reconciliation for active Cortex Work runs."""

from __future__ import annotations

from uuid import UUID

from db import work_repository as repository
from server import persistence as persistence_service
from server.work.config import WorkConfig, load_work_config
from server.work.registry import get_agent_provider
from server.work.service import reconcile_work_run
from utils.logger import get_logger

logger = get_logger(__name__)


def run_reconciliation_cycle(
    *, config: WorkConfig | None = None, limit: int = 100
) -> dict[str, int]:
    resolved = config or load_work_config()
    if not resolved.enabled or not resolved.reconciler_enabled:
        return {"examined": 0, "reconciled": 0, "errors": 0}
    with persistence_service.db_uow(commit_on_success=False) as db:
        rows = repository.list_reconcilable_work_runs(db, limit=limit)
    if not rows:
        return {"examined": 0, "reconciled": 0, "errors": 0}

    provider = get_agent_provider()
    reconciled = 0
    errors = 0
    for row in rows:
        try:
            reconcile_work_run(
                user_id=UUID(str(row["user_id"])),
                work_run_id=UUID(str(row["id"])),
                provider=provider,
                config=resolved,
            )
            reconciled += 1
        except Exception:
            errors += 1
            logger.exception(
                "Background Work reconciliation failed",
                extra={
                    "extra_fields": {
                        "event": "work.reconciler.run_failed",
                        "work_run_id": str(row["id"]),
                    }
                },
            )
    return {"examined": len(rows), "reconciled": reconciled, "errors": errors}
