"""History endpoints - retrieve and delete past chat records."""

from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from db import (
    clear_llm_history,
    delete_llm_history_entry,
    get_llm_history_entries,
    rename_history_session,
)
from server import persistence as persistence_service
from server.billing.response_credit_service import calculate_response_credit_usage
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["History"])
logger = get_logger(__name__)
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="History",
    rejection_event="history.route.rejected.auth_mode",
    logger=logger,
)

API_DB_ENABLED = persistence_service.API_DB_ENABLED
ApiKeyPersistenceResolution = persistence_service.ApiKeyPersistenceResolution
_resolve_identity = persistence_service.resolve_identity
_db_uow = persistence_service.db_uow


def _require_db_mode() -> None:
    if API_DB_ENABLED:
        return
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="History endpoints require DATABASE_URL (DB mode).",
    )


class HistoryEntry(BaseModel):
    id: int
    request_id: str
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    request_group_id: Optional[str] = None
    timestamp: str
    mode: str
    prompt: str
    provider: str
    model: str
    requested_model: Optional[str] = None
    served_model: Optional[str] = None
    pricing_model: Optional[str] = None
    model_lifecycle_status: Optional[str] = None
    alias_redirected: bool = False
    replacement_model: Optional[str] = None
    migration_reason: Optional[str] = None
    pricing_rule_applied: Optional[str] = None
    pricing_version: Optional[str] = None
    pricing_unknown: bool = False
    response_version: int = 1
    generation_profile: Optional[str] = None
    effective_max_output_tokens: Optional[int] = None
    effective_reasoning_mode: Optional[str] = None
    effective_reasoning_effort: Optional[str] = None
    routing_mode: Optional[str] = None
    generation_policy_version: Optional[str] = None
    completion_status: str = "complete"
    stop_cause: str = "unknown"
    response: str
    error_code: Optional[str] = None
    latency_ms: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    tokens: Optional[int] = None
    cost: Optional[float] = None
    ai_credits: Optional[int] = None
    credit_usage_estimated: bool = False
    cache_hit: bool = False
    cache_hit_ratio: float = 0.0
    cache_savings_ai_credits: int = 0
    uncached_equivalent_ai_credits: int = 0
    research_ai_credits: Optional[int] = None
    research_credit_usage_estimated: bool = False
    web_source_items: List[dict[str, str]] = Field(default_factory=list)


class RenameHistorySessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class RenameHistorySessionResponse(BaseModel):
    session_id: str
    title: str


@router.get("/history", response_model=List[HistoryEntry])
async def list_history(
    request: Request,
    limit: int = 100,
    session_id: str | None = None,
    auth: AuthResult = Depends(get_auth),
):
    """Return recent chat history entries (newest first)."""
    _require_db_mode()
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    with _db_uow(commit_on_success=False) as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        entries = get_llm_history_entries(
            db_session,
            resolution.user_id,
            limit=limit,
            session_id=session_id,
        )
    for entry in entries:
        credit_usage = calculate_response_credit_usage(
            provider=str(entry.get("provider") or ""),
            model=str(entry.get("model") or ""),
            requested_model=str(entry.get("requested_model") or ""),
            input_tokens=max(0, int(entry.get("prompt_tokens") or 0)),
            cached_input_tokens=max(0, int(entry.get("cached_input_tokens") or 0)),
            cache_write_tokens=max(0, int(entry.get("cache_write_tokens") or 0)),
            reasoning_tokens=max(0, int(entry.get("reasoning_tokens") or 0)),
            output_tokens=max(0, int(entry.get("completion_tokens") or 0)),
            output_text=str(entry.get("response") or ""),
            is_error=str(entry.get("response") or "").lower().startswith("[error]"),
            pricing_snapshot=(
                entry.get("pricing_snapshot")
                if isinstance(entry.get("pricing_snapshot"), dict)
                else None
            ),
            include_research_charge=False,
        )
        if entry.get("ai_credits") is None:
            entry["ai_credits"] = credit_usage.ai_credits
        entry["credit_usage_estimated"] = credit_usage.credit_usage_estimated
        entry["cache_hit"] = credit_usage.cache_hit
        entry["cache_hit_ratio"] = credit_usage.cache_hit_ratio
        entry["cache_savings_ai_credits"] = credit_usage.cache_savings_ai_credits
        entry["uncached_equivalent_ai_credits"] = (
            credit_usage.uncached_equivalent_ai_credits
        )
    return entries


@router.delete("/history/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    request: Request,
    entry_id: int,
    auth: AuthResult = Depends(get_auth),
):
    """Delete a single history entry by ID."""
    _require_db_mode()
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    with _db_uow() as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        removed = delete_llm_history_entry(db_session, resolution.user_id, entry_id)

    if not removed:
        raise HTTPException(status_code=404, detail="History entry not found")


@router.patch(
    "/history/session/{session_id}",
    response_model=RenameHistorySessionResponse,
)
async def rename_session(
    request: Request,
    session_id: str,
    payload: RenameHistorySessionRequest,
    auth: AuthResult = Depends(get_auth),
):
    """Rename one authenticated user's persisted chat session."""
    _require_db_mode()
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Session title cannot be empty")

    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    with _db_uow() as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        renamed = rename_history_session(
            db_session,
            resolution.user_id,
            session_id,
            title,
        )

    if not renamed:
        raise HTTPException(status_code=404, detail="History session not found")
    return RenameHistorySessionResponse(session_id=session_id, title=title)


@router.delete("/history", status_code=status.HTTP_204_NO_CONTENT)
async def clear_history(
    request: Request,
    session_id: str | None = None,
    auth: AuthResult = Depends(get_auth),
):
    """Delete all history entries, or one session when session_id is provided."""
    _require_db_mode()
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    with _db_uow() as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        clear_llm_history(db_session, resolution.user_id, session_id=session_id)
