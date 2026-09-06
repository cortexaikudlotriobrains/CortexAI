"""Provider-neutral Managed Agent boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ProviderResource:
    provider_file_id: str
    mount_path: str


@dataclass(frozen=True)
class ProviderMcpServer:
    name: str
    url: str
    enabled_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderSession:
    id: str
    status: str
    usage: Mapping[str, object] = field(default_factory=dict)
    model_id: str | None = None
    agent_id: str | None = None
    agent_version: int | None = None
    effort: str | None = None
    speed: str | None = None
    additional_model_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderEvent:
    id: str
    type: str
    display_message: str | None
    payload: Mapping[str, object] = field(default_factory=dict)
    terminal_status: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class ProviderArtifact:
    id: str
    filename: str
    mime_type: str | None
    size_bytes: int | None
    downloadable: bool = True


class AgentProvider(Protocol):
    name: str

    def create_session(
        self,
        *,
        title: str,
        resources: Sequence[ProviderResource],
        mcp_servers: Sequence[ProviderMcpServer],
        vault_ids: Sequence[str],
        web_enabled: bool,
        max_credit_budget: int,
    ) -> ProviderSession: ...

    def send_instruction(self, session_id: str, instruction: str) -> None: ...

    def get_session(self, session_id: str) -> ProviderSession: ...

    def list_events(self, session_id: str) -> list[ProviderEvent]: ...

    def update_session_tools(
        self,
        session_id: str,
        *,
        mcp_servers: Sequence[ProviderMcpServer],
        web_enabled: bool,
    ) -> None: ...

    def extend_budget(
        self,
        session_id: str,
        additional_credit_budget: int,
        *,
        current_usage: Mapping[str, object],
    ) -> None: ...

    def interrupt(self, session_id: str) -> None: ...

    def confirm_tool(
        self, session_id: str, tool_use_id: str, *, allow: bool, deny_message: str | None
    ) -> None: ...

    def upload_file(self, source: BinaryIO, *, filename: str) -> str: ...

    def add_resource(self, session_id: str, resource: ProviderResource) -> None: ...

    def list_artifacts(self, session_id: str) -> list[ProviderArtifact]: ...

    def download_artifact(self, file_id: str) -> bytes: ...
