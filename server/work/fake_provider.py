"""Deterministic test-only Work provider."""

from __future__ import annotations

from typing import BinaryIO, Mapping, Sequence
from uuid import uuid4

from server.work.provider import (
    ProviderArtifact,
    ProviderEvent,
    ProviderMcpServer,
    ProviderResource,
    ProviderSession,
)


class FakeAgentProvider:
    name = "fake"

    def __init__(self):
        self.sessions: dict[str, dict[str, object]] = {}
        self.files: dict[str, bytes] = {}

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
        session_id = f"fake_session_{uuid4().hex}"
        self.sessions[session_id] = {
            "status": "idle",
            "events": [],
            "resources": list(resources),
            "mcp_servers": list(mcp_servers),
            "web_enabled": web_enabled,
        }
        return ProviderSession(
            id=session_id,
            status="idle",
            model_id="claude-haiku-4-5",
            agent_id="fake-agent",
            agent_version=1,
            effort="high",
            speed="standard",
        )

    def send_instruction(self, session_id: str, instruction: str) -> None:
        state = self.sessions[session_id]
        state["status"] = "running"
        events = state["events"]
        assert isinstance(events, list)
        events.extend(
            [
                ProviderEvent(
                    id=f"fake_event_{uuid4().hex}",
                    type="planning",
                    display_message="Creating a plan",
                ),
                ProviderEvent(
                    id=f"fake_event_{uuid4().hex}",
                    type="plan_created",
                    display_message="Plan ready",
                ),
                ProviderEvent(
                    id=f"fake_event_{uuid4().hex}",
                    type="progress",
                    display_message="Work is running",
                ),
            ]
        )

    def complete(self, session_id: str, message: str = "Work completed") -> None:
        state = self.sessions[session_id]
        state["status"] = "idle"
        events = state["events"]
        assert isinstance(events, list)
        events.append(
            ProviderEvent(
                id=f"fake_event_{uuid4().hex}", type="agent_message", display_message=message
            )
        )
        events.append(
            ProviderEvent(
                id=f"fake_event_{uuid4().hex}",
                type="run_completed",
                display_message="Work completed",
                terminal_status="completed",
            )
        )

    def get_session(self, session_id: str) -> ProviderSession:
        state = self.sessions[session_id]
        raw_usage = state.get("usage")
        usage = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
        return ProviderSession(
            id=session_id,
            status=str(state["status"]),
            usage=usage,
            model_id="claude-haiku-4-5",
            agent_id="fake-agent",
            agent_version=1,
            effort="high",
            speed="standard",
        )

    def list_events(self, session_id: str) -> list[ProviderEvent]:
        events = self.sessions[session_id]["events"]
        assert isinstance(events, list)
        return list(events)

    def update_session_tools(
        self,
        session_id: str,
        *,
        mcp_servers: Sequence[ProviderMcpServer],
        web_enabled: bool,
    ) -> None:
        state = self.sessions[session_id]
        state["mcp_servers"] = list(mcp_servers)
        state["web_enabled"] = web_enabled

    def extend_budget(
        self,
        session_id: str,
        additional_credit_budget: int,
        *,
        current_usage: Mapping[str, object],
    ) -> None:
        return None

    def interrupt(self, session_id: str) -> None:
        state = self.sessions[session_id]
        state["status"] = "idle"
        events = state["events"]
        assert isinstance(events, list)
        events.append(
            ProviderEvent(
                id=f"fake_event_{uuid4().hex}",
                type="run_cancelled",
                display_message="Work stopped",
                terminal_status="cancelled",
            )
        )

    def confirm_tool(
        self, session_id: str, tool_use_id: str, *, allow: bool, deny_message: str | None
    ) -> None:
        return None

    def upload_file(self, source: BinaryIO, *, filename: str) -> str:
        file_id = f"fake_file_{uuid4().hex}"
        self.files[file_id] = source.read()
        return file_id

    def add_resource(self, session_id: str, resource: ProviderResource) -> None:
        resources = self.sessions[session_id]["resources"]
        assert isinstance(resources, list)
        resources.append(resource)

    def list_artifacts(self, session_id: str) -> list[ProviderArtifact]:
        return []

    def download_artifact(self, file_id: str) -> bytes:
        return self.files[file_id]
