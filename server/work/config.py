"""Validated feature and provider configuration for CortexAI Work."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class WorkConfig:
    enabled: bool
    mcp_enabled: bool
    action_tools_enabled: bool
    artifact_import_enabled: bool
    web_enabled: bool
    provider: str
    agent_id: str | None
    environment_id: str | None
    default_credit_budget: int
    default_output_token_limit: int
    output_finalize_token_threshold: int
    reconciler_enabled: bool
    reconciler_interval_seconds: int
    event_sync_interval_seconds: int
    sse_heartbeat_seconds: int
    approval_timeout_seconds: int

    def validate_provider(self) -> None:
        if self.provider == "fake":
            return
        if self.provider != "anthropic_managed_agents":
            raise ValueError(f"Unsupported CORTEX_WORK_AGENT_PROVIDER '{self.provider}'")
        missing = []
        if not self.agent_id:
            missing.append("ANTHROPIC_MANAGED_AGENT_ID")
        if not self.environment_id:
            missing.append("ANTHROPIC_MANAGED_ENVIRONMENT_ID")
        if not os.getenv("ANTHROPIC_API_KEY"):
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise ValueError("Cortex Work provider configuration is missing: " + ", ".join(missing))


@lru_cache(maxsize=1)
def load_work_config() -> WorkConfig:
    config = WorkConfig(
        enabled=_bool("CORTEX_WORK_ENABLED"),
        mcp_enabled=_bool("CORTEX_WORK_MCP_ENABLED"),
        action_tools_enabled=_bool("CORTEX_WORK_ACTION_TOOLS_ENABLED"),
        artifact_import_enabled=_bool("CORTEX_WORK_ARTIFACT_IMPORT_ENABLED"),
        web_enabled=_bool("CORTEX_WORK_WEB_ENABLED"),
        provider=str(os.getenv("CORTEX_WORK_AGENT_PROVIDER", "anthropic_managed_agents") or "")
        .strip()
        .lower(),
        agent_id=str(os.getenv("ANTHROPIC_MANAGED_AGENT_ID", "") or "").strip() or None,
        environment_id=str(os.getenv("ANTHROPIC_MANAGED_ENVIRONMENT_ID", "") or "").strip() or None,
        default_credit_budget=_positive_int("CORTEX_WORK_DEFAULT_CREDIT_BUDGET", 1_000_000),
        default_output_token_limit=_positive_int("CORTEX_WORK_DEFAULT_OUTPUT_TOKENS", 40_000),
        output_finalize_token_threshold=_positive_int("CORTEX_WORK_OUTPUT_FINALIZE_TOKENS", 32_000),
        reconciler_enabled=_bool("CORTEX_WORK_RECONCILER_ENABLED", default=True),
        reconciler_interval_seconds=_positive_int("CORTEX_WORK_RECONCILER_INTERVAL_SECONDS", 2),
        event_sync_interval_seconds=_positive_int("CORTEX_WORK_SYNC_INTERVAL_SECONDS", 2),
        sse_heartbeat_seconds=_positive_int("CORTEX_WORK_SSE_HEARTBEAT_SECONDS", 15),
        approval_timeout_seconds=_positive_int("CORTEX_WORK_APPROVAL_TIMEOUT_SECONDS", 86_400),
    )
    if config.output_finalize_token_threshold >= config.default_output_token_limit:
        raise ValueError(
            "CORTEX_WORK_OUTPUT_FINALIZE_TOKENS must be lower than "
            "CORTEX_WORK_DEFAULT_OUTPUT_TOKENS"
        )
    return config
