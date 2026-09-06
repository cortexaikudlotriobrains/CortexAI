"""Tool catalogue, remote MCP discovery, and OAuth connection lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx

from db import work_repository as repository
from server import persistence as persistence_service
from server.billing.subscription_service import resolve_effective_subscription
from server.work.config import load_work_config
from server.work.errors import work_http_error
from server.work.security import (
    classify_action,
    safe_return_path,
    schema_hash,
    validate_remote_https_url,
)

_CONNECTORS: tuple[dict[str, object], ...] = (
    {
        "connector_key": "cortex_files",
        "display_name": "Cortex Files",
        "description": "Use files you upload to CortexAI.",
        "icon": "files",
        "plan_requirement": "plus",
        "capabilities": ["read_files", "create_artifacts"],
        "risk_classes": ["READ", "WRITE"],
        "configuration_required": False,
    },
    {
        "connector_key": "cortex_web",
        "display_name": "Cortex Web",
        "description": "Search public web sources when enabled for the run.",
        "icon": "globe",
        "plan_requirement": "plus",
        "capabilities": ["web_search"],
        "risk_classes": ["READ"],
        "configuration_required": False,
    },
    {
        "connector_key": "github",
        "display_name": "GitHub",
        "description": "Repository and issue tools through an operator-configured official MCP endpoint.",
        "icon": "github",
        "plan_requirement": "plus",
        "capabilities": ["repositories", "issues"],
        "risk_classes": ["READ", "WRITE", "DESTRUCTIVE"],
        "configuration_required": True,
    },
    {
        "connector_key": "google_drive",
        "display_name": "Google Drive",
        "description": "Drive content through an operator-configured supported connector.",
        "icon": "drive",
        "plan_requirement": "plus",
        "capabilities": ["files"],
        "risk_classes": ["READ", "WRITE"],
        "configuration_required": True,
    },
    {
        "connector_key": "gmail",
        "display_name": "Gmail",
        "description": "Mail tools through an operator-configured supported connector.",
        "icon": "mail",
        "plan_requirement": "plus",
        "capabilities": ["mail"],
        "risk_classes": ["READ", "EXTERNAL_COMMUNICATION"],
        "configuration_required": True,
    },
    {
        "connector_key": "slack",
        "display_name": "Slack",
        "description": "Workspace tools through an operator-configured supported connector.",
        "icon": "slack",
        "plan_requirement": "plus",
        "capabilities": ["messages"],
        "risk_classes": ["READ", "EXTERNAL_COMMUNICATION"],
        "configuration_required": True,
    },
    {
        "connector_key": "jira",
        "display_name": "Jira",
        "description": "Issue tools through an operator-configured supported connector.",
        "icon": "jira",
        "plan_requirement": "plus",
        "capabilities": ["issues"],
        "risk_classes": ["READ", "WRITE"],
        "configuration_required": True,
    },
    {
        "connector_key": "notion",
        "display_name": "Notion",
        "description": "Workspace tools through an operator-configured supported connector.",
        "icon": "notion",
        "plan_requirement": "plus",
        "capabilities": ["pages"],
        "risk_classes": ["READ", "WRITE"],
        "configuration_required": True,
    },
    {
        "connector_key": "microsoft_365",
        "display_name": "Microsoft 365",
        "description": "Microsoft 365 tools through an operator-configured supported connector.",
        "icon": "microsoft",
        "plan_requirement": "plus",
        "capabilities": ["files", "mail", "calendar"],
        "risk_classes": ["READ", "WRITE", "EXTERNAL_COMMUNICATION"],
        "configuration_required": True,
    },
    {
        "connector_key": "custom_mcp",
        "display_name": "Custom Remote MCP",
        "description": "Connect a reviewed remote Streamable HTTP MCP endpoint.",
        "icon": "plug",
        "plan_requirement": "pro",
        "capabilities": ["remote_mcp"],
        "risk_classes": ["READ", "WRITE", "DESTRUCTIVE"],
        "configuration_required": False,
    },
)


def _connector_env_key(connector_key: str, suffix: str) -> str:
    prefix = "".join(ch if ch.isalnum() else "_" for ch in connector_key.upper())
    return f"CORTEX_WORK_CONNECTOR_{prefix}_{suffix}"


def catalogue_for_user(user_id: UUID) -> list[dict[str, object]]:
    config = load_work_config()
    with persistence_service.db_uow(commit_on_success=False) as db:
        connections = repository.list_tool_connections_for_user(db, user_id)
    connected = {str(item["connector_key"]): str(item["status"]) for item in connections}
    result: list[dict[str, object]] = []
    for source in _CONNECTORS:
        item = dict(source)
        key = str(item["connector_key"])
        configured = bool(os.getenv(_connector_env_key(key, "MCP_URL")))
        item["configuration_required"] = bool(item["configuration_required"]) and not configured
        if key == "cortex_web" and not config.web_enabled:
            item["connection_state"] = "disabled"
        elif key == "custom_mcp" and not config.mcp_enabled:
            item["connection_state"] = "disabled"
        else:
            item["connection_state"] = connected.get(
                key, "available" if not item["configuration_required"] else "configuration_required"
            )
        result.append(item)
    return result


def create_custom_connection(
    *,
    user_id: UUID,
    display_name: str,
    server_url: str,
    auth_type: str,
    credential_reference: str | None,
    provider_vault_id: str | None,
) -> dict[str, Any]:
    config = load_work_config()
    if not config.enabled or not config.mcp_enabled:
        raise work_http_error(404, "work_mcp_disabled", "Remote MCP connections are not enabled.")
    try:
        validated_url = validate_remote_https_url(server_url)
    except ValueError as exc:
        raise work_http_error(422, "invalid_mcp_url", str(exc)) from exc
    if credential_reference and not credential_reference.startswith("arn:aws:secretsmanager:"):
        raise work_http_error(
            422,
            "invalid_credential_reference",
            "Credential references must be AWS Secrets Manager ARNs.",
        )
    normalized_vault_id = str(provider_vault_id or "").strip() or None
    if auth_type != "none" and not normalized_vault_id:
        raise work_http_error(
            422,
            "provider_vault_required",
            "Authenticated MCP connections require a Managed Agent vault ID.",
        )
    with persistence_service.db_uow() as db:
        effective = resolve_effective_subscription(db, user_id)
        if not effective.plan.entitlements.custom_mcp_enabled:
            raise work_http_error(
                403, "custom_mcp_not_in_plan", "Custom Remote MCP requires the Pro plan."
            )
        if (
            repository.count_tool_connections(db, user_id)
            >= effective.plan.limits.max_tool_connections
        ):
            raise work_http_error(
                409, "tool_connection_limit", "This plan's tool connection limit has been reached."
            )
        return repository.create_tool_connection(
            db,
            user_id=user_id,
            connector_key="custom_mcp",
            connection_type="mcp_remote",
            display_name=display_name.strip(),
            server_url=validated_url,
            auth_type=auth_type,
            credential_reference=credential_reference,
            provider_vault_id=normalized_vault_id,
            status="pending",
        )


def _json_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        data = response.json()
    elif "text/event-stream" in content_type:
        candidates = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    candidates.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    continue
        data = next(
            (
                item
                for item in reversed(candidates)
                if isinstance(item, dict) and ("result" in item or "error" in item)
            ),
            None,
        )
        if data is None:
            raise ValueError("MCP server returned no JSON-RPC response in its event stream")
    else:
        raise ValueError("MCP discovery requires JSON or Streamable HTTP SSE")
    if not isinstance(data, dict):
        raise ValueError("MCP server returned an invalid JSON-RPC response")
    if data.get("error"):
        raise ValueError("MCP server returned a JSON-RPC error")
    return data


def discover_remote_mcp(server_url: str) -> list[dict[str, object]]:
    url = validate_remote_https_url(server_url)
    common_headers = {
        "Accept": "application/json, text/event-stream",
        "Origin": "https://cortex.invalid",
    }
    with httpx.Client(timeout=8.0, follow_redirects=False) as client:
        initialized = client.post(
            url,
            headers=common_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "CortexAI", "version": "1"},
                },
            },
        )
        initialized.raise_for_status()
        data = _json_response(initialized)
        negotiated = str((data.get("result") or {}).get("protocolVersion") or "2025-06-18")
        session_id = initialized.headers.get("mcp-session-id")
        headers = {**common_headers, "MCP-Protocol-Version": negotiated}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        ready = client.post(
            url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        ready.raise_for_status()
        listed = client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed.raise_for_status()
        tools = (_json_response(listed).get("result") or {}).get("tools") or []
    safe: list[dict[str, object]] = []
    for raw in tools[:500]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "")[:255]
        description = str(raw.get("description") or "")[:1000]
        if not name:
            continue
        schema = raw.get("inputSchema") if isinstance(raw.get("inputSchema"), dict) else {}
        safe.append(
            {
                "name": name,
                "description": description,
                "input_schema_hash": schema_hash(schema),
                "risk_classification": classify_action(name, description),
            }
        )
    return safe


def test_connection(
    *, user_id: UUID, connection_id: UUID
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    with persistence_service.db_uow(commit_on_success=False) as db:
        connection = repository.get_tool_connection_for_user(db, connection_id, user_id)
    if connection is None:
        raise work_http_error(404, "tool_connection_not_found", "Tool connection not found.")
    try:
        tools = discover_remote_mcp(str(connection.get("server_url") or ""))
    except Exception as exc:
        with persistence_service.db_uow() as db:
            updated = repository.update_tool_connection(
                db,
                connection_id,
                user_id,
                status="error",
                metadata={"last_test_error": type(exc).__name__},
            )
        raise work_http_error(
            502, "mcp_connection_failed", "The remote MCP server could not be verified."
        ) from exc
    metadata = dict(connection.get("metadata") or {})
    metadata.update(
        {
            "discovered_tools": tools,
            "enabled_tools": [item["name"] for item in tools],
            "last_discovered_at": datetime.now(UTC).isoformat(),
        }
    )
    with persistence_service.db_uow() as db:
        updated = repository.update_tool_connection(
            db, connection_id, user_id, status="connected", metadata=metadata, verified=True
        )
    return updated or connection, tools


def begin_oauth(*, user_id: UUID, connector_key: str, return_to: str) -> tuple[str, datetime]:
    connector = next((item for item in _CONNECTORS if item["connector_key"] == connector_key), None)
    if connector is None or connector_key in {"custom_mcp", "cortex_files", "cortex_web"}:
        raise work_http_error(404, "connector_not_found", "Connector not found.")
    auth_url = os.getenv(_connector_env_key(connector_key, "OAUTH_AUTHORIZATION_URL"), "").strip()
    client_id = os.getenv(_connector_env_key(connector_key, "OAUTH_CLIENT_ID"), "").strip()
    redirect_uri = os.getenv(_connector_env_key(connector_key, "OAUTH_REDIRECT_URI"), "").strip()
    scope = os.getenv(_connector_env_key(connector_key, "OAUTH_SCOPE"), "").strip()
    mcp_url = os.getenv(_connector_env_key(connector_key, "MCP_URL"), "").strip()
    if not all((auth_url, client_id, redirect_uri, mcp_url)):
        raise work_http_error(
            503,
            "connector_configuration_required",
            "This connector requires operator OAuth and MCP configuration.",
        )
    try:
        safe_return = safe_return_path(return_to)
        validate_remote_https_url(auth_url)
        validate_remote_https_url(mcp_url)
    except ValueError as exc:
        raise work_http_error(
            503,
            "connector_configuration_invalid",
            "The connector's server configuration is invalid.",
        ) from exc
    state = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(minutes=10)
    with persistence_service.db_uow() as db:
        effective = resolve_effective_subscription(db, user_id)
        if not effective.plan.entitlements.verified_connectors_enabled:
            raise work_http_error(
                403, "verified_connectors_not_in_plan", "Verified connectors require a paid plan."
            )
        repository.create_oauth_state(
            db,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            user_id=user_id,
            connector_key=connector_key,
            redirect_uri=safe_return,
            expires_at=expires,
        )
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if scope:
        query["scope"] = scope
    return f"{auth_url}?{urlencode(query)}", expires


def complete_oauth(
    *, user_id: UUID, connector_key: str, state: str, code: str
) -> tuple[dict[str, Any], str]:
    state_hash = hashlib.sha256(str(state).encode()).hexdigest()
    with persistence_service.db_uow() as db:
        consumed = repository.consume_oauth_state(db, state_hash=state_hash, user_id=user_id)
    if consumed is None or consumed["connector_key"] != connector_key:
        raise work_http_error(
            400, "invalid_oauth_state", "The OAuth state is invalid, expired, or already used."
        )
    token_url = os.getenv(_connector_env_key(connector_key, "OAUTH_TOKEN_URL"), "").strip()
    client_id = os.getenv(_connector_env_key(connector_key, "OAUTH_CLIENT_ID"), "").strip()
    client_secret = os.getenv(_connector_env_key(connector_key, "OAUTH_CLIENT_SECRET"), "").strip()
    redirect_uri = os.getenv(_connector_env_key(connector_key, "OAUTH_REDIRECT_URI"), "").strip()
    mcp_url = os.getenv(_connector_env_key(connector_key, "MCP_URL"), "").strip()
    if not all((token_url, client_id, client_secret, redirect_uri, mcp_url)):
        raise work_http_error(
            503,
            "connector_configuration_required",
            "This connector requires operator OAuth and MCP configuration.",
        )
    provider_vault_id = os.getenv(
        _connector_env_key(connector_key, "PROVIDER_VAULT_ID"), ""
    ).strip()
    if not provider_vault_id:
        raise work_http_error(
            503,
            "connector_provider_vault_required",
            "This connector requires an operator-configured Managed Agent vault.",
        )
    validate_remote_https_url(token_url)
    response = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=10.0,
        follow_redirects=False,
    )
    response.raise_for_status()
    token_payload = response.json()
    if not isinstance(token_payload, dict) or not token_payload.get("access_token"):
        raise work_http_error(
            502,
            "oauth_token_exchange_failed",
            "The connector did not return a usable OAuth credential.",
        )
    secret_prefix = (
        os.getenv("CORTEX_WORK_CONNECTOR_SECRET_PREFIX", "cortex/work/connectors")
        .strip()
        .strip("/")
    )
    secret_name = f"{secret_prefix}/{user_id}/{connector_key}"
    try:
        import boto3

        client = boto3.client("secretsmanager")
        serialized = json.dumps(token_payload, separators=(",", ":"))
        try:
            result = client.create_secret(Name=secret_name, SecretString=serialized)
        except client.exceptions.ResourceExistsException:
            client.put_secret_value(SecretId=secret_name, SecretString=serialized)
            result = client.describe_secret(SecretId=secret_name)
        secret_arn = str(result.get("ARN") or secret_name)
    except Exception as exc:
        raise work_http_error(
            503,
            "credential_store_unavailable",
            "The connector credential could not be stored securely.",
        ) from exc
    with persistence_service.db_uow() as db:
        effective = resolve_effective_subscription(db, user_id)
        if (
            repository.count_tool_connections(db, user_id)
            >= effective.plan.limits.max_tool_connections
        ):
            raise work_http_error(
                409, "tool_connection_limit", "This plan's tool connection limit has been reached."
            )
        connection = repository.create_tool_connection(
            db,
            user_id=user_id,
            connector_key=connector_key,
            connection_type="mcp_remote",
            display_name=next(
                str(item["display_name"])
                for item in _CONNECTORS
                if item["connector_key"] == connector_key
            ),
            server_url=mcp_url,
            auth_type="oauth2",
            credential_reference=secret_arn,
            provider_vault_id=provider_vault_id,
            status="connected",
            granted_scopes=str(token_payload.get("scope") or "").split(),
            metadata={},
        )
    return connection, str(consumed["redirect_uri"])
