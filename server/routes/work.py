"""CortexAI Work session, run, event, approval, and artifact routes."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from db import get_uploaded_file_for_user
from db import work_repository as repository
from server import persistence as persistence_service
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.work import (
    WorkApprovalDecisionDTO,
    WorkApprovalDTO,
    WorkEventDTO,
    WorkEventsDTO,
    WorkRunCreateDTO,
    WorkRunDTO,
    WorkRunFileDTO,
    WorkSessionCreateDTO,
    WorkSessionDTO,
)
from server.work.config import load_work_config
from server.work.errors import work_http_error
from server.work.registry import get_agent_provider
from server.object_storage import get_object_storage
from server.work import service
from utils.logger import get_logger

router = APIRouter(prefix="/v1/work", tags=["Work"])
logger = get_logger(__name__)
_GUARD = SessionScopedAuthGuard(
    route_label="Work",
    rejection_event="work.route.rejected.auth_mode",
    logger=logger,
)


def _identity(request: Request, auth: AuthResult) -> UUID:
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _GUARD.require(auth=auth, request_id=request_id)
    with persistence_service.db_uow() as db:
        return persistence_service.resolve_identity(
            auth=auth, request_id=request_id, db_session=db
        ).user_id


def _session_dto(row: dict, *, title: str | None = None) -> WorkSessionDTO:
    return WorkSessionDTO(
        id=row["id"],
        session_id=row["session_id"],
        title=row.get("title", title),
        status=row["status"],
        agent_provider=str(row["agent_provider"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        latest_run_status=row.get("latest_run_status"),
    )


def _run_dto(row: dict) -> WorkRunDTO:
    return WorkRunDTO.model_validate({key: row.get(key) for key in WorkRunDTO.model_fields})


def _event_dto(row: dict) -> WorkEventDTO:
    return WorkEventDTO(
        id=row["id"],
        sequence=int(row["sequence_number"]),
        type=str(row["event_type"]),
        display_message=row.get("display_message"),
        payload=dict(row.get("payload") or {}),
        created_at=row["created_at"],
    )


@router.post("/sessions", response_model=WorkSessionDTO, status_code=status.HTTP_201_CREATED)
async def create_work_session(
    body: WorkSessionCreateDTO,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    row = await asyncio.to_thread(
        service.create_owned_work_session, user_id=user_id, title=body.title
    )
    return _session_dto(row, title=body.title or "New work")


@router.get("/sessions", response_model=list[WorkSessionDTO])
async def list_work_sessions(request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    rows = await asyncio.to_thread(service.list_owned_work_sessions, user_id)
    return [_session_dto(row) for row in rows]


@router.get("/sessions/{work_session_id}", response_model=WorkSessionDTO)
async def get_work_session(
    work_session_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    row = await asyncio.to_thread(service.get_owned_work_session, user_id, work_session_id)
    return _session_dto(row)


@router.get("/sessions/{work_session_id}/runs/latest", response_model=WorkRunDTO)
async def get_latest_run(
    work_session_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        rows = repository.list_work_runs_for_session(db, work_session_id, user_id)
    if not rows:
        raise work_http_error(404, "work_run_not_found", "This Work session has no runs yet.")
    return _run_dto(rows[-1])


@router.get("/sessions/{work_session_id}/runs", response_model=list[WorkRunDTO])
async def list_runs(
    work_session_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        if repository.get_work_session_for_user(db, work_session_id, user_id) is None:
            raise work_http_error(404, "work_session_not_found", "Work session not found.")
        rows = repository.list_work_runs_for_session(db, work_session_id, user_id)
    return [_run_dto(row) for row in rows]


@router.post(
    "/sessions/{work_session_id}/runs",
    response_model=WorkRunDTO,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    work_session_id: UUID,
    body: WorkRunCreateDTO,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    request_id = str(idempotency_key or getattr(request.state, "request_id", "") or uuid4())[:255]
    row = await asyncio.to_thread(
        service.start_work_run,
        user_id=user_id,
        work_session_id=work_session_id,
        request_id=request_id,
        instruction=body.instruction,
        input_file_ids=body.input_file_ids,
        enabled_connection_ids=body.enabled_connection_ids,
        web_mode=body.web_mode,
        max_credit_budget=body.max_credit_budget,
    )
    return _run_dto(row)


@router.post(
    "/sessions/{work_session_id}/instructions",
    response_model=WorkRunDTO,
    status_code=status.HTTP_202_ACCEPTED,
)
async def follow_up(
    work_session_id: UUID,
    body: WorkRunCreateDTO,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auth: AuthResult = Depends(get_auth),
):
    return await start_run(work_session_id, body, request, idempotency_key, auth)


@router.get("/runs/{run_id}", response_model=WorkRunDTO)
async def get_run(run_id: UUID, request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    row: dict | None
    try:
        row = await asyncio.to_thread(
            service.reconcile_work_run, user_id=user_id, work_run_id=run_id
        )
    except Exception as exc:
        with persistence_service.db_uow(commit_on_success=False) as db:
            row = repository.get_work_run_for_user(db, run_id, user_id)
        if row is None:
            raise work_http_error(404, "work_run_not_found", "Work run not found.") from exc
    if row is None:  # pragma: no cover - repository contract guard
        raise work_http_error(404, "work_run_not_found", "Work run not found.")
    return _run_dto(row)


@router.get("/runs/{run_id}/events", response_model=WorkEventsDTO)
async def get_events(
    run_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        run = repository.get_work_run_for_user(db, run_id, user_id)
    if run is None:
        raise work_http_error(404, "work_run_not_found", "Work run not found.")
    if run["status"] not in repository.TERMINAL_RUN_STATUSES:
        try:
            await asyncio.to_thread(service.reconcile_work_run, user_id=user_id, work_run_id=run_id)
        except Exception:
            logger.warning("Work event catch-up reconciliation unavailable", exc_info=True)
    with persistence_service.db_uow(commit_on_success=False) as db:
        rows = repository.list_work_events_after_sequence(
            db, run_id, user_id, after_sequence=after_sequence
        )
    latest = int(rows[-1]["sequence_number"]) if rows else after_sequence
    return WorkEventsDTO(items=[_event_dto(row) for row in rows], latest_sequence=latest)


@router.get("/runs/{run_id}/stream")
async def stream_events(
    run_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    try:
        header_sequence = int(last_event_id or 0)
    except ValueError:
        header_sequence = 0
    cursor = max(after_sequence, header_sequence)
    with persistence_service.db_uow(commit_on_success=False) as db:
        if repository.get_work_run_for_user(db, run_id, user_id) is None:
            raise work_http_error(404, "work_run_not_found", "Work run not found.")
    config = load_work_config()

    async def generate():
        nonlocal cursor
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                await asyncio.to_thread(
                    service.reconcile_work_run, user_id=user_id, work_run_id=run_id
                )
            except Exception:
                logger.warning("Work SSE reconciliation unavailable", exc_info=True)
            with persistence_service.db_uow(commit_on_success=False) as db:
                rows = repository.list_work_events_after_sequence(
                    db, run_id, user_id, after_sequence=cursor
                )
                run = repository.get_work_run_for_user(db, run_id, user_id)
            for row in rows:
                dto = _event_dto(row)
                cursor = dto.sequence
                payload = dto.model_dump(mode="json")
                yield f"id: {cursor}\nevent: {dto.type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                last_heartbeat = time.monotonic()
            if run and run["status"] in repository.TERMINAL_RUN_STATUSES and not rows:
                break
            if time.monotonic() - last_heartbeat >= config.sse_heartbeat_seconds:
                yield f": heartbeat {cursor}\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(config.event_sync_interval_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runs/{run_id}/cancel", response_model=WorkRunDTO)
async def cancel_run(run_id: UUID, request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    row = await asyncio.to_thread(service.cancel_work_run, user_id=user_id, work_run_id=run_id)
    return _run_dto(row)


@router.get("/runs/{run_id}/artifacts", response_model=list[WorkRunFileDTO])
async def run_artifacts(run_id: UUID, request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        run = repository.get_work_run_for_user(db, run_id, user_id)
        if run is None:
            raise work_http_error(404, "work_run_not_found", "Work run not found.")
    config = load_work_config()
    if run["status"] in repository.TERMINAL_RUN_STATUSES and config.artifact_import_enabled:
        try:
            await asyncio.to_thread(
                service.import_work_artifacts,
                user_id=user_id,
                work_run_id=run_id,
            )
        except Exception:
            logger.warning(
                "Work artifact retry unavailable",
                extra={
                    "extra_fields": {
                        "event": "work.artifact.retry.failed",
                        "work_run_id": str(run_id),
                    }
                },
                exc_info=True,
            )
    with persistence_service.db_uow(commit_on_success=False) as db:
        rows = repository.list_work_run_files(db, run_id, user_id, role="artifact")
    return [
        WorkRunFileDTO(
            id=row["id"],
            file_id=row["file_id"],
            role=row["role"],
            source=row["source"],
            filename=row["original_filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            artifact_type=row.get("artifact_type"),
            metadata=dict(row.get("metadata") or {}),
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.get("/runs/{run_id}/artifacts/{file_id}/download")
async def download_artifact(
    run_id: UUID,
    file_id: UUID,
    request: Request,
    inline: bool = Query(default=False),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        artifacts = repository.list_work_run_files(db, run_id, user_id, role="artifact")
        artifact = next((row for row in artifacts if row["file_id"] == file_id), None)
        file_row = (
            get_uploaded_file_for_user(db, user_id=user_id, file_id=file_id)
            if artifact is not None
            else None
        )
    if artifact is None or file_row is None:
        raise work_http_error(404, "work_artifact_not_found", "Work artifact not found.")
    payload = await asyncio.to_thread(
        get_object_storage().get_bytes, key=str(file_row["storage_key"])
    )
    filename = str(artifact["original_filename"])
    return Response(
        content=payload,
        media_type=str(artifact["mime_type"] or "application/octet-stream"),
        headers={
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/approvals/{approval_id}", response_model=WorkApprovalDTO)
async def get_approval(
    approval_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        row = repository.get_approval_for_user(db, approval_id, user_id)
    if row is None:
        raise work_http_error(404, "work_approval_not_found", "Work approval not found.")
    return WorkApprovalDTO(**row)


async def _decide_approval(
    *,
    approval_id: UUID,
    user_id: UUID,
    decision: str,
    reason: str | None,
    remember: bool = False,
) -> WorkApprovalDTO:
    with persistence_service.db_uow(commit_on_success=False) as db:
        approval = repository.get_approval_for_user(db, approval_id, user_id)
        if approval is None:
            raise work_http_error(404, "work_approval_not_found", "Approval not found.")
        if approval["status"] != "pending":
            raise work_http_error(
                409, "work_approval_already_resolved", "This approval was already resolved."
            )
        run = repository.get_work_run_for_user(db, approval["work_run_id"], user_id)
        tool_call = repository.get_tool_call(db, approval["tool_call_id"])
        if run is None or tool_call is None:
            raise work_http_error(
                409,
                "work_approval_context_missing",
                "The managed tool request is unavailable.",
            )
        work_session = repository.get_work_session(db, UUID(str(run["work_session_id"])))
        if work_session is None:
            raise work_http_error(
                409,
                "work_approval_context_missing",
                "The Work session is unavailable.",
            )
    provider_session_id = str(
        work_session.get("provider_session_id") or run.get("provider_run_id") or ""
    )
    if not provider_session_id or not tool_call or not tool_call.get("provider_call_id"):
        raise work_http_error(
            503, "work_approval_provider_unavailable", "The managed tool request is unavailable."
        )
    with persistence_service.db_uow() as db:
        try:
            updated = repository.decide_approval(
                db, approval_id=approval_id, user_id=user_id, decision=decision
            )
        except ValueError as exc:
            raise work_http_error(
                409, "work_approval_already_resolved", "This approval was already resolved."
            ) from exc
        if updated is None:  # pragma: no cover - locked ownership guard
            raise work_http_error(404, "work_approval_not_found", "Approval not found.")
    try:
        await asyncio.to_thread(
            get_agent_provider().confirm_tool,
            provider_session_id,
            str(tool_call["provider_call_id"]),
            allow=decision == "approved",
            deny_message=reason,
        )
    except Exception as exc:
        with persistence_service.db_uow() as db:
            repository.reopen_approval_after_provider_failure(
                db,
                approval_id=approval_id,
                user_id=user_id,
                decision=decision,
            )
        raise work_http_error(
            503,
            "work_approval_provider_unavailable",
            "The managed tool request could not be confirmed. Try again.",
        ) from exc
    with persistence_service.db_uow() as db:
        remaining = repository.list_pending_approvals_for_run(db, approval["work_run_id"], user_id)
        resumed_status = "waiting_for_approval" if remaining else "running"
        repository.update_work_run(db, approval["work_run_id"], status=resumed_status)
        repository.update_work_session(db, UUID(str(run["work_session_id"])), status=resumed_status)
        remembered = False
        if (
            decision == "approved"
            and remember
            and approval["action_type"] == "WRITE"
            and approval.get("connection_id") is not None
        ):
            session_row = repository.get_work_session(db, UUID(str(run["work_session_id"])))
            policy = dict((session_row or {}).get("default_tool_policy") or {})
            grants = [
                dict(item)
                for item in policy.get("allowed_write_tools", [])
                if isinstance(item, dict)
            ]
            grant = {
                "connection_id": str(approval["connection_id"]),
                "tool_name": str(approval["tool_name"]),
            }
            if grant not in grants:
                grants.append(grant)
            policy["allowed_write_tools"] = grants
            repository.update_work_session(
                db,
                UUID(str(run["work_session_id"])),
                default_tool_policy=policy,
            )
            remembered = True
        repository.append_work_event(
            db,
            work_run_id=approval["work_run_id"],
            event_type="approval_resolved",
            display_message="Action approved" if decision == "approved" else "Action denied",
            payload={
                "approval_id": str(approval_id),
                "decision": decision,
                "remembered": remembered,
            },
        )
    return WorkApprovalDTO(**updated)


@router.post("/approvals/{approval_id}/approve", response_model=WorkApprovalDTO)
async def approve_action(
    approval_id: UUID,
    body: WorkApprovalDecisionDTO,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    return await _decide_approval(
        approval_id=approval_id,
        user_id=_identity(request, auth),
        decision="approved",
        reason=body.reason,
        remember=body.remember,
    )


@router.post("/approvals/{approval_id}/deny", response_model=WorkApprovalDTO)
async def deny_action(
    approval_id: UUID,
    body: WorkApprovalDecisionDTO,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    return await _decide_approval(
        approval_id=approval_id,
        user_id=_identity(request, auth),
        decision="denied",
        reason=body.reason or "The user denied this action.",
    )
