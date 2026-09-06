"""Authenticated connector catalogue and remote MCP management routes."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse

from db import work_repository as repository
from server import persistence as persistence_service
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.work import (
    OAuthStartDTO,
    OAuthStartResponseDTO,
    ToolCatalogItemDTO,
    ToolConnectionCreateDTO,
    ToolConnectionDTO,
    ToolDiscoveryDTO,
    ToolTestDTO,
)
from server.work import tools_service
from server.work.errors import work_http_error
from utils.logger import get_logger

router = APIRouter(prefix="/v1/tools", tags=["Work tools"])
logger = get_logger(__name__)
_GUARD = SessionScopedAuthGuard(
    route_label="Work tools",
    rejection_event="work.tools.route.rejected.auth_mode",
    logger=logger,
)


def _identity(request: Request, auth: AuthResult) -> UUID:
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _GUARD.require(auth=auth, request_id=request_id)
    with persistence_service.db_uow() as db:
        return persistence_service.resolve_identity(
            auth=auth, request_id=request_id, db_session=db
        ).user_id


def _connection_dto(row: dict) -> ToolConnectionDTO:
    return ToolConnectionDTO(
        id=row["id"],
        connector_key=str(row["connector_key"]),
        connection_type=row["connection_type"],
        display_name=str(row["display_name"]),
        server_url=row.get("server_url"),
        auth_type=str(row["auth_type"]),
        status=row["status"],
        granted_scopes=list(row.get("granted_scopes") or []),
        metadata=dict(row.get("metadata") or {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_verified_at=row.get("last_verified_at"),
    )


@router.get("/catalog", response_model=list[ToolCatalogItemDTO])
async def tool_catalog(request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    return await asyncio.to_thread(tools_service.catalogue_for_user, user_id)


@router.get("/connections", response_model=list[ToolConnectionDTO])
async def list_connections(request: Request, auth: AuthResult = Depends(get_auth)):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        rows = repository.list_tool_connections_for_user(db, user_id)
    return [_connection_dto(row) for row in rows]


@router.post(
    "/connections",
    response_model=ToolConnectionDTO,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    body: ToolConnectionCreateDTO,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    row = await asyncio.to_thread(
        tools_service.create_custom_connection,
        user_id=user_id,
        display_name=body.display_name,
        server_url=body.server_url,
        auth_type=body.auth_type,
        credential_reference=body.credential_reference,
        provider_vault_id=body.provider_vault_id,
    )
    return _connection_dto(row)


@router.get("/connections/{connection_id}", response_model=ToolConnectionDTO)
async def get_connection(
    connection_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow(commit_on_success=False) as db:
        row = repository.get_tool_connection_for_user(db, connection_id, user_id)
    if row is None:
        raise work_http_error(404, "tool_connection_not_found", "Tool connection not found.")
    return _connection_dto(row)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    with persistence_service.db_uow() as db:
        deleted = repository.disable_tool_connection(db, connection_id, user_id)
    if not deleted:
        raise work_http_error(404, "tool_connection_not_found", "Tool connection not found.")
    return None


@router.post("/connections/{connection_id}/test", response_model=ToolTestDTO)
async def test_connection(
    connection_id: UUID,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    _, tools = await asyncio.to_thread(
        tools_service.test_connection, user_id=user_id, connection_id=connection_id
    )
    return ToolTestDTO(
        ok=True,
        status="connected",
        message=f"Connected and discovered {len(tools)} tool(s).",
    )


@router.get("/connections/{connection_id}/tools", response_model=ToolDiscoveryDTO)
async def connection_tools(
    connection_id: UUID,
    request: Request,
    refresh: bool = Query(default=False),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    if refresh:
        _, tools = await asyncio.to_thread(
            tools_service.test_connection, user_id=user_id, connection_id=connection_id
        )
    else:
        with persistence_service.db_uow(commit_on_success=False) as db:
            row = repository.get_tool_connection_for_user(db, connection_id, user_id)
        if row is None:
            raise work_http_error(404, "tool_connection_not_found", "Tool connection not found.")
        tools = list((row.get("metadata") or {}).get("discovered_tools") or [])
    return ToolDiscoveryDTO(tools=tools)


@router.post("/{connector_key}/oauth/start", response_model=OAuthStartResponseDTO)
async def oauth_start(
    connector_key: str,
    body: OAuthStartDTO,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    url, expires = await asyncio.to_thread(
        tools_service.begin_oauth,
        user_id=user_id,
        connector_key=connector_key,
        return_to=body.return_to,
    )
    return OAuthStartResponseDTO(authorization_url=url, expires_at=expires)


@router.get("/{connector_key}/oauth/callback", response_class=RedirectResponse)
async def oauth_callback(
    connector_key: str,
    request: Request,
    state: str = Query(min_length=20, max_length=500),
    code: str = Query(min_length=1, max_length=5000),
    auth: AuthResult = Depends(get_auth),
):
    user_id = _identity(request, auth)
    _, return_to = await asyncio.to_thread(
        tools_service.complete_oauth,
        user_id=user_id,
        connector_key=connector_key,
        state=state,
        code=code,
    )
    return RedirectResponse(
        url=f"{return_to}?connector={connector_key}&connected=1", status_code=303
    )
