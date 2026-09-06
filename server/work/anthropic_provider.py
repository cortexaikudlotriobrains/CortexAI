"""Anthropic Claude Managed Agents adapter.

The SDK surface is isolated here so routes/services never depend on Anthropic
event structures. Managed Agent events are normalized and thinking content is
never copied into Cortex payloads.
"""

from __future__ import annotations

from typing import Any, BinaryIO, Mapping, Sequence

from server.work.config import WorkConfig
from server.work.provider import (
    AgentProvider,
    ProviderArtifact,
    ProviderEvent,
    ProviderMcpServer,
    ProviderResource,
    ProviderSession,
)
from server.work.security import normalize_work_title, redact_mapping

_MANAGED_AGENTS_BETA = ["managed-agents-2026-04-01"]
_CORTEX_CREDITS_PER_PROVIDER_CENT = 10_000


def _provider_budget_cents(credit_budget: int) -> int:
    """Round a Cortex-credit ceiling up to Anthropic's whole-cent budget unit."""

    return max(
        1,
        (int(credit_budget) + _CORTEX_CREDITS_PER_PROVIDER_CENT - 1)
        // _CORTEX_CREDITS_PER_PROVIDER_CENT,
    )


def _provider_list_cost_cents(usage: Mapping[str, object]) -> int:
    raw = usage.get("list_cost")
    if raw is None:
        return 0
    value = _dump(raw)
    currency = str(value.get("currency") or "USD").upper()
    if currency != "USD":
        raise RuntimeError(f"Unsupported Managed Agent usage currency: {currency}")
    try:
        return max(0, int(str(value.get("amount") or "0")))
    except ValueError as exc:
        raise RuntimeError("Managed Agent usage returned an invalid list cost") from exc


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return dict(value)
    result: dict[str, Any] = {}
    for name in (
        "id",
        "type",
        "status",
        "content",
        "name",
        "input",
        "stop_reason",
        "usage",
        "agent",
        "model",
        "version",
        "effort",
        "speed",
        "multiagent",
        "agents",
        "filename",
        "mime_type",
        "size_bytes",
        "mcp_server_name",
        "server_name",
        "server",
        "tool_use_id",
    ):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _optional_positive_int(value: object) -> int | None:
    parsed = _optional_non_negative_int(value)
    return parsed if parsed and parsed > 0 else None


def _setting_name(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    data = _dump(value)
    return str(data.get("type") or data.get("id") or "").strip() or None


def _session_identity(data: Mapping[str, object]) -> dict[str, object]:
    agent = _dump(data.get("agent")) if data.get("agent") is not None else {}
    model_value = agent.get("model")
    model = _dump(model_value) if model_value is not None else {}
    model_id = str(
        model.get("id") or (model_value if isinstance(model_value, str) else "") or ""
    ).strip()
    additional: list[str] = []
    multiagent = _dump(agent.get("multiagent")) if agent.get("multiagent") is not None else {}
    roster = multiagent.get("agents")
    if isinstance(roster, (list, tuple)):
        for item in roster:
            item_data = _dump(item)
            nested_model = item_data.get("model")
            nested = _dump(nested_model) if nested_model is not None else {}
            nested_id = str(
                nested.get("id") or (nested_model if isinstance(nested_model, str) else "") or ""
            ).strip()
            if nested_id and nested_id != model_id and nested_id not in additional:
                additional.append(nested_id)
    return {
        "model_id": model_id or None,
        "agent_id": str(agent.get("id") or "").strip() or None,
        "agent_version": _optional_positive_int(agent.get("version")),
        "effort": _setting_name(model.get("effort")),
        "speed": _setting_name(model.get("speed")),
        "additional_model_ids": tuple(additional),
    }


def _provider_session(data: Mapping[str, object], *, default_status: str) -> ProviderSession:
    identity = _session_identity(data)
    return ProviderSession(
        id=str(data["id"]),
        status=str(data.get("status") or default_status),
        usage=_dump(data.get("usage")) if data.get("usage") is not None else {},
        model_id=identity["model_id"] if isinstance(identity["model_id"], str) else None,
        agent_id=identity["agent_id"] if isinstance(identity["agent_id"], str) else None,
        agent_version=(
            identity["agent_version"] if isinstance(identity["agent_version"], int) else None
        ),
        effort=identity["effort"] if isinstance(identity["effort"], str) else None,
        speed=identity["speed"] if isinstance(identity["speed"], str) else None,
        additional_model_ids=(
            identity["additional_model_ids"]
            if isinstance(identity["additional_model_ids"], tuple)
            else ()
        ),
    )


def _content_text(content: object) -> str | None:
    if not isinstance(content, (list, tuple)):
        return None
    parts: list[str] = []
    for block in content:
        data = _dump(block)
        if data.get("type") == "text" and data.get("text"):
            parts.append(str(data["text"]))
    normalized = "".join(parts).strip()
    return normalized[:20_000] or None


def _optional_non_negative_int(value: object) -> int | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _stop_reason(data: Mapping[str, object]) -> tuple[str | None, list[str]]:
    raw = data.get("stop_reason")
    value = _dump(raw) if raw is not None else {}
    reason = str(value.get("type") or raw or "").strip() or None
    event_ids = (
        [str(item) for item in value.get("event_ids", [])]
        if isinstance(value.get("event_ids"), list)
        else []
    )
    return reason, event_ids


def normalize_anthropic_event(event: Any) -> ProviderEvent:
    data = _dump(event)
    provider_type = str(data.get("type") or "provider.event")
    provider_id = str(data.get("id") or f"unidentified:{hash(str(data))}")
    payload: dict[str, object] = {"provider_type": provider_type}
    display: str | None = None
    terminal: str | None = None
    stop_reason: str | None = None
    normalized_type = "progress"

    if provider_type == "agent.message":
        normalized_type = "agent_message"
        display = _content_text(data.get("content")) or "Agent updated the result"
    elif provider_type == "agent.thinking":
        normalized_type = "progress"
        display = "Reasoning about the next step"
    elif provider_type in {"agent.tool_use", "agent.mcp_tool_use", "agent.custom_tool_use"}:
        normalized_type = "tool_started"
        tool_name = str(data.get("name") or "tool")
        display = f"Using {tool_name}"
        payload.update({"tool_name": tool_name, "tool_use_id": provider_id})
        server_name = data.get("mcp_server_name") or data.get("server_name") or data.get("server")
        if server_name:
            payload["mcp_server_name"] = str(server_name)
        raw_input = data.get("input")
        if isinstance(raw_input, dict):
            payload["input_keys"] = sorted(str(key) for key in raw_input)[:50]
            payload["input_summary"] = redact_mapping(raw_input)
    elif provider_type in {"agent.tool_result", "agent.mcp_tool_result"}:
        normalized_type = "tool_completed"
        display = "Tool completed"
        tool_use_id = data.get("tool_use_id")
        if tool_use_id:
            payload["tool_use_id"] = str(tool_use_id)
    elif provider_type == "user.tool_confirmation":
        tool_use_id = data.get("tool_use_id")
        if tool_use_id:
            payload["tool_use_id"] = str(tool_use_id)
        result = str(data.get("result") or "").strip().lower()
        if result in {"allow", "deny"}:
            payload["result"] = result
    elif provider_type == "session.status_running":
        normalized_type = "progress"
        display = "Work is running"
    elif provider_type == "session.status_idle":
        stop_reason, event_ids = _stop_reason(data)
        payload["stop_reason"] = stop_reason
        if event_ids:
            payload["blocking_event_ids"] = event_ids
        if stop_reason == "requires_action":
            normalized_type = "approval_required"
            display = "Your approval is required"
        elif stop_reason == "budget_reached":
            normalized_type = "budget_exhausted"
            display = "Credit budget reached"
            terminal = "budget_exhausted"
        else:
            normalized_type = "run_completed"
            display = "Work completed"
            terminal = "completed"
    elif provider_type == "session.usage":
        normalized_type = "progress"
        display = "Usage updated"
        usage = data.get("usage") or data
        if isinstance(usage, dict):
            payload["usage"] = usage
    elif provider_type.endswith("error") or ".error" in provider_type:
        normalized_type = "run_failed"
        display = "The agent could not continue"
        terminal = "failed"
    elif provider_type in {"session.file_created", "agent.file_created"}:
        normalized_type = "file_created"
        display = "Created a file"

    return ProviderEvent(
        id=provider_id,
        type=normalized_type,
        display_message=display,
        payload=payload,
        terminal_status=terminal,
        stop_reason=stop_reason,
    )


def _agent_override(
    *,
    agent_id: str | None,
    mcp_servers: Sequence[ProviderMcpServer],
    web_enabled: bool,
) -> dict[str, object]:
    toolsets: list[dict[str, object]] = [
        {
            "type": "agent_toolset_20260401",
            "default_config": {
                "enabled": True,
                "permission_policy": {"type": "always_ask"},
            },
            "configs": [
                *[
                    {
                        "name": name,
                        "permission_policy": {"type": "always_allow"},
                    }
                    for name in ("read", "glob", "grep")
                ],
                {
                    "name": "web_search",
                    "enabled": web_enabled,
                    "permission_policy": {"type": "always_allow"},
                },
                {
                    "name": "web_fetch",
                    "enabled": web_enabled,
                    "permission_policy": {"type": "always_allow"},
                },
            ],
        }
    ]
    for item in mcp_servers:
        toolset: dict[str, object] = {
            "type": "mcp_toolset",
            "mcp_server_name": item.name,
            "default_config": {
                "enabled": not bool(item.enabled_tools),
                "permission_policy": {"type": "always_ask"},
            },
        }
        if item.enabled_tools:
            toolset["configs"] = [
                {
                    "name": name,
                    "enabled": True,
                    "permission_policy": {"type": "always_ask"},
                }
                for name in item.enabled_tools
            ]
        toolsets.append(toolset)
    return {
        "type": "agent_with_overrides",
        "id": agent_id,
        "mcp_servers": [
            {"type": "url", "name": item.name, "url": item.url} for item in mcp_servers
        ],
        "tools": toolsets,
    }


class AnthropicManagedAgentProvider(AgentProvider):
    name = "anthropic_managed_agents"

    def __init__(self, config: WorkConfig, client: Any | None = None):
        config.validate_provider()
        if client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover - environment guard
                raise RuntimeError("Install requirements.txt to use Cortex Work") from exc
            client = Anthropic()
        if not hasattr(getattr(client, "beta", None), "sessions"):
            raise RuntimeError("The installed Anthropic SDK does not support Managed Agents")
        # Keep the beta SDK surface isolated behind the typed AgentProvider protocol.
        # Managed Agents evolves faster than the generated SDK type declarations.
        self._client: Any = client
        self._config = config

    def create_session(
        self,
        *,
        title: str,
        resources: Sequence[ProviderResource],
        mcp_servers: Sequence[ProviderMcpServer],
        vault_ids: Sequence[str],
        web_enabled: bool,
        max_credit_budget: int,
    ) -> ProviderSession:
        provider_title = normalize_work_title(title, max_length=200) or "Cortex Work"
        kwargs: dict[str, object] = {
            "agent": self._config.agent_id,
            "environment_id": self._config.environment_id,
            "title": provider_title,
        }
        kwargs["agent"] = _agent_override(
            agent_id=self._config.agent_id,
            mcp_servers=mcp_servers,
            web_enabled=web_enabled,
        )
        if resources:
            kwargs["resources"] = [
                {"type": "file", "file_id": item.provider_file_id, "mount_path": item.mount_path}
                for item in resources
            ]
        if vault_ids:
            kwargs["vault_ids"] = list(vault_ids)
        provider_budget_cents = _provider_budget_cents(max_credit_budget)
        if provider_budget_cents > 0:
            kwargs["budget"] = {
                "type": "limit",
                "max_list_cost": {"amount": str(provider_budget_cents), "currency": "USD"},
            }
        kwargs["betas"] = _MANAGED_AGENTS_BETA
        created = self._client.beta.sessions.create(**kwargs)
        data = _dump(created)
        return _provider_session(data, default_status="idle")

    def send_instruction(self, session_id: str, instruction: str) -> None:
        self._client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": instruction}]}],
            betas=_MANAGED_AGENTS_BETA,
        )

    def get_session(self, session_id: str) -> ProviderSession:
        result = self._client.beta.sessions.retrieve(session_id, betas=_MANAGED_AGENTS_BETA)
        data = _dump(result)
        return _provider_session(data, default_status="unknown")

    def list_events(self, session_id: str) -> list[ProviderEvent]:
        page = self._client.beta.sessions.events.list(
            session_id, order="asc", limit=1000, betas=_MANAGED_AGENTS_BETA
        )
        data = list(page) if hasattr(page, "__iter__") else getattr(page, "data", page)
        return [normalize_anthropic_event(event) for event in data]

    def update_session_tools(
        self,
        session_id: str,
        *,
        mcp_servers: Sequence[ProviderMcpServer],
        web_enabled: bool,
    ) -> None:
        override = _agent_override(
            agent_id=self._config.agent_id,
            mcp_servers=mcp_servers,
            web_enabled=web_enabled,
        )
        self._client.beta.sessions.update(
            session_id,
            # Session updates accept only full-replacement tools and MCP arrays;
            # the create-only agent identity/type fields must not be sent.
            agent={
                "mcp_servers": override["mcp_servers"],
                "tools": override["tools"],
            },
            betas=_MANAGED_AGENTS_BETA,
        )

    def extend_budget(
        self,
        session_id: str,
        additional_credit_budget: int,
        *,
        current_usage: Mapping[str, object],
    ) -> None:
        max_list_cost_cents = _provider_list_cost_cents(current_usage) + _provider_budget_cents(
            additional_credit_budget
        )
        self._client.beta.sessions.update(
            session_id,
            budget={
                "type": "limit",
                "max_list_cost": {
                    "amount": str(max_list_cost_cents),
                    "currency": "USD",
                },
            },
            betas=_MANAGED_AGENTS_BETA,
        )

    def interrupt(self, session_id: str) -> None:
        self._client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.interrupt"}],
            betas=_MANAGED_AGENTS_BETA,
        )

    def confirm_tool(
        self, session_id: str, tool_use_id: str, *, allow: bool, deny_message: str | None
    ) -> None:
        event: dict[str, object] = {
            "type": "user.tool_confirmation",
            "tool_use_id": tool_use_id,
            "result": "allow" if allow else "deny",
        }
        if not allow and deny_message:
            event["deny_message"] = deny_message[:500]
        self._client.beta.sessions.events.send(
            session_id, events=[event], betas=_MANAGED_AGENTS_BETA
        )

    def upload_file(self, source: BinaryIO, *, filename: str) -> str:
        result = self._client.files.upload(file=(filename, source))
        return str(_dump(result)["id"])

    def add_resource(self, session_id: str, resource: ProviderResource) -> None:
        self._client.beta.sessions.resources.add(
            session_id,
            type="file",
            file_id=resource.provider_file_id,
            mount_path=resource.mount_path,
            betas=_MANAGED_AGENTS_BETA,
        )

    def list_artifacts(self, session_id: str) -> list[ProviderArtifact]:
        page = self._client.beta.files.list(
            scope_id=session_id,
            betas=_MANAGED_AGENTS_BETA,
        )
        items = list(page) if hasattr(page, "__iter__") else getattr(page, "data", page)
        artifacts: list[ProviderArtifact] = []
        for item in items:
            data = _dump(item)
            artifacts.append(
                ProviderArtifact(
                    id=str(data.get("id")),
                    filename=str(data.get("filename") or "artifact"),
                    mime_type=(str(data["mime_type"]) if data.get("mime_type") else None),
                    size_bytes=_optional_non_negative_int(data.get("size_bytes")),
                    downloadable=bool(data.get("downloadable", True)),
                )
            )
        return artifacts

    def download_artifact(self, file_id: str) -> bytes:
        content = self._client.beta.files.download(file_id, betas=_MANAGED_AGENTS_BETA)
        if hasattr(content, "read"):
            return bytes(content.read())
        raw = getattr(content, "content", content)
        return bytes(raw)
