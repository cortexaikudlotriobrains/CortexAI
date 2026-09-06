"""Provider-neutral public contracts for CortexAI Work."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from server.work.security import normalize_work_title

WorkSessionStatus = Literal[
    "idle", "running", "waiting_for_approval", "completed", "failed", "cancelled"
]
WorkRunStatus = Literal[
    "created",
    "planning",
    "running",
    "waiting_for_approval",
    "completed",
    "failed",
    "cancelled",
    "budget_exhausted",
    "output_limit_reached",
]


class WorkSessionCreateDTO(BaseModel):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        return normalize_work_title(value)


class WorkSessionDTO(BaseModel):
    id: UUID
    session_id: UUID
    title: str | None = None
    status: WorkSessionStatus
    agent_provider: str
    created_at: datetime
    updated_at: datetime
    latest_run_status: str | None = None


class WorkRunCreateDTO(BaseModel):
    instruction: str = Field(min_length=1, max_length=100_000)
    input_file_ids: list[UUID] = Field(default_factory=list, max_length=20)
    enabled_connection_ids: list[UUID] = Field(default_factory=list, max_length=20)
    web_mode: Literal["auto", "on", "off"] = "auto"
    web_enabled: bool | None = None
    max_credit_budget: int | None = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_web_setting(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        supplied_mode = normalized.get("web_mode")
        if supplied_mode is None and "web_enabled" in normalized:
            normalized["web_mode"] = "on" if bool(normalized.get("web_enabled")) else "off"
        elif supplied_mode is not None and normalized.get("web_enabled") is not None:
            expected = "on" if bool(normalized["web_enabled"]) else "off"
            if supplied_mode != expected:
                raise ValueError("web_mode conflicts with legacy web_enabled")
        return normalized

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("instruction must not be blank")
        return normalized

    @field_validator("input_file_ids", "enabled_connection_ids")
    @classmethod
    def reject_duplicate_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate IDs are not allowed")
        return value


class WorkRunDTO(BaseModel):
    id: UUID
    work_session_id: UUID
    request_id: str
    instruction: str
    status: WorkRunStatus
    provider: str
    max_credit_budget: int
    max_output_tokens: int
    actual_output_tokens: int
    reserved_credits: int
    actual_credits: int
    provider_model_id: str | None = None
    billing_model_id: str | None = None
    billing_model_source: str | None = None
    provider_agent_id: str | None = None
    provider_agent_version: int | None = None
    output_finalize_requested_at: datetime | None = None
    output_limit_interrupt_requested_at: datetime | None = None
    configuration_snapshot: dict[str, Any] = Field(default_factory=dict)
    usage_snapshot: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkEventDTO(BaseModel):
    id: UUID
    sequence: int
    type: str
    display_message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkEventsDTO(BaseModel):
    items: list[WorkEventDTO]
    latest_sequence: int


class WorkRunFileDTO(BaseModel):
    id: UUID
    file_id: UUID
    role: Literal["input", "artifact"]
    source: Literal["user", "agent", "connector"]
    filename: str
    mime_type: str
    size_bytes: int
    artifact_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkApprovalDTO(BaseModel):
    id: UUID
    work_run_id: UUID
    tool_call_id: UUID
    connection_id: UUID | None = None
    action_type: str
    tool_name: str
    description: str
    request_payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "denied", "expired"]
    requested_at: datetime
    decided_at: datetime | None = None


class WorkApprovalDecisionDTO(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    remember: bool = False


class ToolConnectionCreateDTO(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    server_url: str = Field(min_length=1, max_length=2048)
    auth_type: Literal["none", "bearer", "oauth2"] = "none"
    credential_reference: str | None = Field(default=None, max_length=1024)
    provider_vault_id: str | None = Field(default=None, min_length=1, max_length=255)


class ToolConnectionDTO(BaseModel):
    id: UUID
    connector_key: str
    connection_type: Literal["cortex_builtin", "mcp_remote"]
    display_name: str
    server_url: str | None = None
    auth_type: str
    status: Literal["pending", "connected", "expired", "error", "disabled"]
    granted_scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None = None


class ToolCatalogItemDTO(BaseModel):
    connector_key: str
    display_name: str
    description: str
    icon: str
    connection_state: str
    plan_requirement: str
    capabilities: list[str]
    risk_classes: list[str]
    configuration_required: bool = False


class ToolTestDTO(BaseModel):
    ok: bool
    status: str
    message: str


class ToolDiscoveryDTO(BaseModel):
    tools: list[dict[str, Any]] = Field(default_factory=list)


class OAuthStartDTO(BaseModel):
    return_to: str = Field(default="/work", max_length=500)


class OAuthStartResponseDTO(BaseModel):
    authorization_url: str
    expires_at: datetime
