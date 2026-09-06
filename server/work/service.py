"""Application service for durable CortexAI Work execution."""

from __future__ import annotations

from io import BytesIO
import hashlib
import mimetypes
import os
from pathlib import PurePosixPath
import re
import socket
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status

from db import create_session, create_uploaded_file, get_uploaded_file_for_user
from db import billing_repository
from db import work_repository as repository
from server import persistence as persistence_service
from server import privacy as privacy_service
from server.billing.errors import UsageAllowanceExceededError
from server.billing.metering_service import (
    release_usage,
    reserve_usage,
    settle_usage_with_supplement,
)
from server.billing.subscription_service import resolve_effective_subscription
from server.object_storage import get_object_storage
from server.work.billing import (
    WorkBillingIdentityError,
    WorkCreditUsage,
    calculate_work_credit_usage,
    resolve_work_billing_model,
)
from server.work.config import WorkConfig, load_work_config
from server.work.errors import work_http_error
from server.work.output_policy import resolve_output_guardrail
from server.work.prompt_policy import resolve_work_web_mode
from server.work.provider import AgentProvider, ProviderMcpServer, ProviderResource, ProviderSession
from server.work.registry import get_agent_provider
from server.work.security import classify_action
from utils.logger import get_logger

logger = get_logger(__name__)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_INTERRUPTIBLE_PROVIDER_SESSION_STATUSES = frozenset({"running", "rescheduling"})
_MAX_PROVIDER_CONTINUATION_RUNS = 6
_MAX_PROVIDER_CONTINUATION_CHARS = 12_000


def require_work_enabled(config: WorkConfig | None = None) -> WorkConfig:
    resolved = config or load_work_config()
    if not resolved.enabled:
        raise work_http_error(
            status.HTTP_404_NOT_FOUND,
            "work_disabled",
            "CortexAI Work is not enabled.",
        )
    try:
        resolved.validate_provider()
    except ValueError as exc:
        raise work_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "work_provider_not_configured",
            "CortexAI Work is not configured for this environment.",
        ) from exc
    return resolved


def _safe_mount_name(filename: str, file_id: UUID) -> str:
    base = PurePosixPath(str(filename or "file").replace("\\", "/")).name
    sanitized = _SAFE_FILENAME.sub("_", base).strip("._")[:120] or "file"
    return f"/{str(file_id)[:8]}-{sanitized}"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _mapping_or_empty(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _can_reuse_provider_session(
    provider_session_id: str,
    prior_capabilities: object,
    current_connection_ids: Sequence[str],
    current_vault_ids: Sequence[str],
) -> bool:
    if not provider_session_id or not isinstance(prior_capabilities, Mapping):
        return False
    if "vault_ids" not in prior_capabilities:
        # Older capability snapshots did not record vault IDs. Reuse only when
        # the complete connection set is unchanged, because a removed legacy
        # connection may have mounted a vault that cannot be detached in place.
        return sorted(_string_list(prior_capabilities.get("connection_ids"))) == sorted(
            str(value) for value in current_connection_ids
        )
    prior_vault_ids = sorted(_string_list(prior_capabilities.get("vault_ids")))
    return prior_vault_ids == sorted(str(value) for value in current_vault_ids)


def _provider_continuation_context(
    *,
    user_id: UUID,
    work_session_id: UUID,
    current_run_id: UUID,
) -> tuple[str, list[str]]:
    """Build a bounded visible-result transcript when a provider session must be replaced."""

    with persistence_service.db_uow(commit_on_success=False) as db:
        prior_runs = [
            row
            for row in repository.list_work_runs_for_session(db, work_session_id, user_id)
            if row["id"] != current_run_id
        ][-_MAX_PROVIDER_CONTINUATION_RUNS:]
        rendered: list[tuple[str, str]] = []
        for run in reversed(prior_runs):
            events = repository.list_work_events_after_sequence(
                db,
                run["id"],
                user_id,
                after_sequence=0,
                limit=1_000_000,
            )
            response = next(
                (
                    str(event.get("display_message") or "").strip()
                    for event in reversed(events)
                    if event.get("event_type") == "agent_message"
                    and str(event.get("display_message") or "").strip()
                ),
                "",
            )
            artifacts = repository.list_work_run_files(
                db,
                run["id"],
                user_id,
                role="artifact",
            )
            artifact_names = ", ".join(str(item["original_filename"]) for item in artifacts)
            parts = [
                f"Previous user instruction:\n{str(run['instruction']).strip()}",
                f"Previous run status: {run['status']}",
            ]
            if response:
                parts.append(f"Previous Cortex result:\n{response}")
            if artifact_names:
                parts.append(f"Saved Cortex artifacts: {artifact_names}")
            rendered.append((str(run["id"]), "\n".join(parts)))

    selected: list[tuple[str, str]] = []
    remaining = _MAX_PROVIDER_CONTINUATION_CHARS
    for run_id, block in rendered:
        if len(block) > remaining:
            if selected:
                break
            block = block[-remaining:]
        selected.append((run_id, block))
        remaining -= len(block) + 2
        if remaining <= 0:
            break
    selected.reverse()
    return "\n\n".join(block for _, block in selected), [run_id for run_id, _ in selected]


def _plan_and_budget(db: Any, user_id: UUID, requested_budget: int | None, config: WorkConfig):
    effective = resolve_effective_subscription(db, user_id)
    plan = effective.plan
    if not plan.entitlements.work_enabled:
        raise work_http_error(
            status.HTTP_403_FORBIDDEN,
            "work_not_in_plan",
            f"CortexAI Work is not available on the {plan.display_name} plan.",
            current_plan=plan.code,
            recommended_plan="plus",
        )
    active = repository.count_active_work_runs(db, user_id)
    if active >= plan.limits.max_active_work_runs:
        raise work_http_error(
            status.HTTP_409_CONFLICT,
            "active_work_run_limit",
            "Finish or stop an active Work run before starting another.",
            limit=plan.limits.max_active_work_runs,
        )
    plan_max = plan.limits.max_work_credit_budget
    budget = requested_budget or min(config.default_credit_budget, plan_max)
    if budget <= 0 or budget > plan_max:
        raise work_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_work_credit_budget",
            "The Work credit budget exceeds the plan limit.",
            limit=plan_max,
        )
    return effective, budget


def create_owned_work_session(
    *,
    user_id: UUID,
    title: str | None,
    config: WorkConfig | None = None,
) -> dict[str, Any]:
    resolved = require_work_enabled(config)
    with persistence_service.db_uow() as db:
        effective = resolve_effective_subscription(db, user_id)
        if not effective.plan.entitlements.work_enabled:
            raise work_http_error(
                status.HTTP_403_FORBIDDEN,
                "work_not_in_plan",
                f"CortexAI Work is not available on the {effective.plan.display_name} plan.",
                current_plan=effective.plan.code,
                recommended_plan="plus",
            )
        session_id = create_session(db, user_id, mode="work", title=title or "New work")
        return repository.create_work_session(
            db,
            session_id=session_id,
            user_id=user_id,
            agent_provider=resolved.provider,
            provider_agent_id=resolved.agent_id,
            provider_environment_id=resolved.environment_id,
            default_tool_policy={
                "read": "allow",
                "write": "policy",
                "destructive": "ask",
                "external_communication": "ask",
                "financial": "ask",
                "deployment": "ask",
            },
        )


def list_owned_work_sessions(user_id: UUID) -> list[dict[str, Any]]:
    with persistence_service.db_uow(commit_on_success=False) as db:
        return repository.list_work_sessions_for_user(db, user_id)


def get_owned_work_session(user_id: UUID, work_session_id: UUID) -> dict[str, Any]:
    with persistence_service.db_uow(commit_on_success=False) as db:
        row = repository.get_work_session_for_user(db, work_session_id, user_id)
    if row is None:
        raise work_http_error(
            status.HTTP_404_NOT_FOUND, "work_session_not_found", "Work session not found."
        )
    return row


def _load_and_validate_inputs(
    db: Any, *, user_id: UUID, file_ids: Sequence[UUID], plan: Any
) -> list[dict[str, Any]]:
    if len(file_ids) > plan.limits.max_files_per_request:
        raise work_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "too_many_work_files",
            "Too many input files for this plan.",
            limit=plan.limits.max_files_per_request,
        )
    rows: list[dict[str, Any]] = []
    for file_id in file_ids:
        row = get_uploaded_file_for_user(db, user_id=user_id, file_id=file_id)
        if row is None:
            raise work_http_error(
                status.HTTP_404_NOT_FOUND, "work_file_not_found", "An input file was not found."
            )
        if row.get("status") != "ready":
            raise work_http_error(
                status.HTTP_409_CONFLICT, "work_file_not_ready", "An input file is not ready."
            )
        if int(row.get("size_bytes") or 0) > plan.limits.max_file_bytes:
            raise work_http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "work_file_too_large",
                "An input file exceeds the plan limit.",
            )
        rows.append(row)
    return rows


def _load_connections(
    db: Any, *, user_id: UUID, connection_ids: Sequence[UUID], plan: Any, config: WorkConfig
) -> list[dict[str, Any]]:
    if len(connection_ids) > plan.limits.max_mcp_servers_per_run:
        raise work_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "too_many_work_connections",
            "Too many tool connections for this run.",
        )
    rows: list[dict[str, Any]] = []
    for connection_id in connection_ids:
        row = repository.get_tool_connection_for_user(db, connection_id, user_id)
        if row is None:
            raise work_http_error(
                status.HTTP_404_NOT_FOUND,
                "tool_connection_not_found",
                "A tool connection was not found.",
            )
        if row.get("status") != "connected":
            raise work_http_error(
                status.HTTP_409_CONFLICT,
                "tool_connection_unavailable",
                "A selected tool connection is not connected.",
            )
        if row.get("connection_type") == "mcp_remote" and not config.mcp_enabled:
            raise work_http_error(
                status.HTTP_403_FORBIDDEN,
                "work_mcp_disabled",
                "Remote MCP connections are not enabled.",
            )
        if row.get("connector_key") == "custom_mcp" and not plan.entitlements.custom_mcp_enabled:
            raise work_http_error(
                status.HTTP_403_FORBIDDEN,
                "custom_mcp_not_in_plan",
                "Custom MCP connections require the Pro plan.",
            )
        rows.append(row)
    return rows


def _provider_billing_identity(
    provider_session: ProviderSession, *, provider_name: str
) -> dict[str, object]:
    reported_model = str(provider_session.model_id or "").strip()
    if not reported_model:
        raise WorkBillingIdentityError("The Managed Agent session did not expose agent.model.id")
    if provider_session.additional_model_ids:
        raise WorkBillingIdentityError(
            "Cortex Work cannot component-bill a multi-model Managed Agent session"
        )
    return {
        "provider_model_id": reported_model,
        "billing_model_id": resolve_work_billing_model(reported_model),
        "billing_model_source": f"{provider_name}_session_agent_snapshot",
        "provider_agent_id": provider_session.agent_id,
        "provider_agent_version": provider_session.agent_version,
        "provider_model_effort": provider_session.effort,
        "provider_model_speed": provider_session.speed,
    }


def start_work_run(
    *,
    user_id: UUID,
    work_session_id: UUID,
    request_id: str,
    instruction: str,
    input_file_ids: Sequence[UUID],
    enabled_connection_ids: Sequence[UUID],
    web_mode: str,
    max_credit_budget: int | None,
    provider: AgentProvider | None = None,
    config: WorkConfig | None = None,
) -> dict[str, Any]:
    resolved = require_work_enabled(config)
    web_decision = resolve_work_web_mode(instruction, web_mode)
    web_enabled = web_decision.effective_enabled
    if web_enabled and not resolved.web_enabled:
        raise work_http_error(
            status.HTTP_403_FORBIDDEN, "work_web_disabled", "Web access is not enabled for Work."
        )
    agent = provider or get_agent_provider()

    with persistence_service.db_uow() as db:
        work_session = repository.get_work_session_for_user(db, work_session_id, user_id)
        if work_session is None:
            raise work_http_error(
                status.HTTP_404_NOT_FOUND, "work_session_not_found", "Work session not found."
            )
        prior = repository.get_work_run_by_request_for_user(db, request_id, user_id)
        if prior is not None:
            if prior["work_session_id"] != work_session_id or prior["instruction"] != instruction:
                raise work_http_error(
                    status.HTTP_409_CONFLICT,
                    "work_request_conflict",
                    "The request ID was already used for a different Work run.",
                )
            return prior
        effective, budget = _plan_and_budget(db, user_id, max_credit_budget, resolved)
        files = _load_and_validate_inputs(
            db, user_id=user_id, file_ids=input_file_ids, plan=effective.plan
        )
        connections = _load_connections(
            db,
            user_id=user_id,
            connection_ids=enabled_connection_ids,
            plan=effective.plan,
            config=resolved,
        )
        try:
            reservation = reserve_usage(
                db,
                effective_subscription=effective,
                request_id=request_id,
                operation_type="work",
                requested_quantities={"ai_credits": budget},
            )
        except UsageAllowanceExceededError as exc:
            raise work_http_error(
                status.HTTP_402_PAYMENT_REQUIRED,
                "insufficient_credits",
                "The requested Work budget exceeds the remaining AI credits.",
                remaining=exc.remaining,
                required=exc.requested,
            ) from exc
        configuration = {
            "web_enabled": web_enabled,
            "requested_web_mode": web_decision.requested_mode,
            "effective_web_enabled": web_enabled,
            "web_current_information": web_decision.current_information,
            "web_resolution_reason": web_decision.reason,
            "web_policy_version": "work-web-v1",
            "input_file_ids": [str(value) for value in input_file_ids],
            "enabled_connection_ids": [str(value) for value in enabled_connection_ids],
            "plan_code": effective.plan.code,
            "provider_usage_baseline": {},
        }
        run, created = repository.create_work_run(
            db,
            work_session_id=work_session_id,
            request_id=request_id,
            instruction=instruction,
            provider=agent.name,
            max_credit_budget=budget,
            max_output_tokens=resolved.default_output_token_limit,
            reserved_credits=budget,
            billing_reservation_id=reservation.id,
            configuration_snapshot=configuration,
        )
        if not created:
            if run["work_session_id"] != work_session_id or run["instruction"] != instruction:
                raise work_http_error(
                    status.HTTP_409_CONFLICT,
                    "work_request_conflict",
                    "The request ID was already used for a different Work run.",
                )
            return run
        for item in files:
            repository.attach_work_file(
                db,
                work_run_id=run["id"],
                user_id=user_id,
                file_id=item["id"],
                role="input",
                source="user",
                metadata={"mount_path": _safe_mount_name(item["original_filename"], item["id"])},
            )
        for connection in connections:
            repository.snapshot_run_connection(
                db,
                work_run_id=run["id"],
                connection_id=connection["id"],
                configuration_snapshot={
                    "connector_key": connection["connector_key"],
                    "connection_type": connection["connection_type"],
                    "server_url": connection.get("server_url"),
                    "auth_type": connection["auth_type"],
                    "provider_vault_id": connection.get("provider_vault_id"),
                    "granted_scopes": list(connection.get("granted_scopes") or []),
                },
            )
        repository.append_work_event(
            db,
            work_run_id=run["id"],
            event_type="run_created",
            display_message="Work run created",
            payload={"status": "created"},
        )

    provider_session_id = str(work_session.get("provider_session_id") or "").strip()
    resources: list[ProviderResource] = []
    continuation_context = ""
    continuation_run_ids: list[str] = []
    try:
        storage = get_object_storage() if files else None
        for file_row in files:
            assert storage is not None
            payload = storage.get_bytes(key=str(file_row["storage_key"]))
            provider_file_id = agent.upload_file(
                BytesIO(payload), filename=str(file_row["original_filename"])
            )
            resource = ProviderResource(
                provider_file_id=provider_file_id,
                mount_path=_safe_mount_name(file_row["original_filename"], file_row["id"]),
            )
            resources.append(resource)
            with persistence_service.db_uow() as db:
                repository.update_work_run_file_provider_id(
                    db,
                    work_run_id=run["id"],
                    file_id=file_row["id"],
                    role="input",
                    provider_file_id=provider_file_id,
                    metadata={"mount_path": resource.mount_path},
                )

        mcp_servers = [
            ProviderMcpServer(
                name=f"cortex_{str(row['id']).replace('-', '')[:16]}",
                url=str(row["server_url"]),
                enabled_tools=tuple(
                    str(item) for item in (row.get("metadata") or {}).get("enabled_tools", [])
                ),
            )
            for row in connections
            if row.get("connection_type") == "mcp_remote" and row.get("server_url")
        ]
        vault_ids = [
            str(row["provider_vault_id"]) for row in connections if row.get("provider_vault_id")
        ]
        connection_ids = sorted(str(row["id"]) for row in connections)
        capability_snapshot = {
            "connection_ids": connection_ids,
            "vault_ids": sorted(vault_ids),
            "web_enabled": web_enabled,
        }
        prior_capabilities = (
            (work_session.get("metadata") or {}).get("provider_capabilities")
            if isinstance(work_session.get("metadata"), Mapping)
            else None
        )
        reuse_provider_session = _can_reuse_provider_session(
            provider_session_id,
            prior_capabilities,
            connection_ids,
            vault_ids,
        )
        resume_budget_pause = False
        if reuse_provider_session and provider_session_id:
            provider_session = agent.get_session(provider_session_id)
            baseline = dict(provider_session.usage)
            prior_provider_events = agent.list_events(provider_session_id)
            configuration["provider_event_baseline_ids"] = [
                event.id for event in prior_provider_events
            ]
            resume_budget_pause = (
                _latest_provider_stop_reason(prior_provider_events) == "budget_reached"
            )
            for resource in resources:
                agent.add_resource(provider_session_id, resource)
            agent.update_session_tools(
                provider_session_id,
                mcp_servers=mcp_servers,
                web_enabled=web_enabled,
            )
            agent.extend_budget(
                provider_session_id,
                budget,
                current_usage=baseline,
            )
        else:
            with persistence_service.db_uow(commit_on_success=False) as db:
                persisted_files = repository.list_work_session_files(
                    db, work_session_id, user_id, role="input"
                )
            resources_by_file = {
                str(item["file_id"]): ProviderResource(
                    provider_file_id=str(item["provider_file_id"]),
                    mount_path=str(
                        (item.get("metadata") or {}).get("mount_path")
                        or _safe_mount_name(item["original_filename"], item["file_id"])
                    ),
                )
                for item in persisted_files
                if item.get("provider_file_id")
            }
            resources_by_file.update(
                {
                    str(file_row["id"]): resource
                    for file_row, resource in zip(files, resources, strict=True)
                }
            )
            created_session = agent.create_session(
                title=str(work_session.get("title") or instruction)[:200],
                resources=list(resources_by_file.values()),
                mcp_servers=mcp_servers,
                vault_ids=vault_ids,
                web_enabled=web_enabled,
                max_credit_budget=budget,
            )
            provider_session_id = created_session.id
            provider_session = agent.get_session(provider_session_id)
            baseline = dict(provider_session.usage)
            continuation_context, continuation_run_ids = _provider_continuation_context(
                user_id=user_id,
                work_session_id=work_session_id,
                current_run_id=run["id"],
            )
        identity = _provider_billing_identity(provider_session, provider_name=agent.name)
        configuration.update(identity)
        configuration["provider_usage_baseline"] = baseline
        configuration["provider_context_replayed"] = bool(continuation_context)
        configuration["provider_context_run_ids"] = continuation_run_ids
        current_metadata = dict(work_session.get("metadata") or {})
        current_metadata["provider_capabilities"] = capability_snapshot
        with persistence_service.db_uow() as db:
            repository.update_work_run(
                db,
                run["id"],
                provider_run_id=provider_session_id,
                configuration_snapshot=configuration,
                provider_model_id=str(identity["provider_model_id"]),
                billing_model_id=str(identity["billing_model_id"]),
                billing_model_source=str(identity["billing_model_source"]),
                provider_agent_id=(
                    str(identity["provider_agent_id"])
                    if identity.get("provider_agent_id")
                    else None
                ),
                provider_agent_version=(
                    int(str(identity["provider_agent_version"]))
                    if identity.get("provider_agent_version")
                    else None
                ),
            )
            repository.update_work_session(
                db,
                work_session_id,
                provider_session_id=provider_session_id,
                metadata=current_metadata,
            )
        if not resume_budget_pause:
            provider_instruction = instruction
            if continuation_context:
                provider_instruction = (
                    "Cortex continuation context from earlier runs in this Work session follows. "
                    "Treat it as prior conversation, not as a new instruction.\n\n"
                    f"{continuation_context}\n\n"
                    f"Current user instruction:\n{instruction}"
                )
            agent.send_instruction(provider_session_id, provider_instruction)
        else:
            configuration["resumed_from_budget_pause"] = True
    except Exception as exc:
        billing_identity_error = isinstance(exc, WorkBillingIdentityError)
        error_code = (
            "work_billing_model_unavailable"
            if billing_identity_error
            else "work_provider_start_failed"
        )
        error_message = (
            "The managed agent's billing model could not be verified."
            if billing_identity_error
            else "The managed agent could not start."
        )
        logger.exception(
            "Work provider start failed",
            extra={
                "extra_fields": {
                    "event": "work.provider.start.failed",
                    "work_run_id": str(run["id"]),
                }
            },
        )
        with persistence_service.db_uow() as db:
            repository.update_work_run(
                db,
                run["id"],
                status="failed",
                error_code=error_code,
                error_message=error_message,
                completed=True,
            )
            repository.update_work_session(db, work_session_id, status="failed")
            repository.append_work_event(
                db,
                work_run_id=run["id"],
                event_type="run_failed",
                display_message=error_message,
                payload={"code": error_code},
            )
            release_usage(db, reservation_id=reservation.id, reason=error_code)
        raise work_http_error(
            (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if billing_identity_error
                else status.HTTP_502_BAD_GATEWAY
            ),
            error_code,
            error_message,
        ) from exc

    with persistence_service.db_uow() as db:
        repository.update_work_run(
            db,
            run["id"],
            status="running",
            provider_run_id=provider_session_id,
            configuration_snapshot=configuration,
            started=True,
        )
        repository.update_work_session(
            db,
            work_session_id,
            status="running",
            provider_session_id=provider_session_id,
            metadata=current_metadata,
        )
        repository.append_work_event(
            db,
            work_run_id=run["id"],
            event_type="planning",
            display_message="Creating a plan",
            payload={},
        )
        return repository.get_work_run(db, run["id"]) or run


def _usage_for_run(
    run: Mapping[str, object], current_usage: Mapping[str, object]
) -> WorkCreditUsage:
    snapshot = run.get("configuration_snapshot")
    config_snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    baseline_raw = config_snapshot.get("provider_usage_baseline")
    baseline = baseline_raw if isinstance(baseline_raw, Mapping) else {}
    model = str(run.get("billing_model_id") or config_snapshot.get("billing_model_id") or "")
    return calculate_work_credit_usage(current_usage, baseline, model=model)


def _settle_work_billing(db: Any, run: Mapping[str, object], usage: WorkCreditUsage) -> int:
    reservation_id = run.get("billing_reservation_id")
    if not isinstance(reservation_id, UUID):
        return 0
    if usage.total_credits == 0:
        release_usage(
            db,
            reservation_id=reservation_id,
            reason="work_completed_without_billable_usage",
        )
        return 0
    work_session_id = UUID(str(run["work_session_id"]))
    work_session = repository.get_work_session(db, work_session_id)
    if work_session is None:
        raise RuntimeError("Work session disappeared during billing settlement")
    settlement = settle_usage_with_supplement(
        db,
        reservation_id=reservation_id,
        actual_quantity=usage.total_credits,
        allowance_limit=resolve_effective_subscription(
            db,
            work_session["user_id"],
        ).plan.allowances.ai_credits,
    )
    billed = settlement.billed_quantity
    billing_repository.create_credit_transaction(
        db,
        billing_account_id=settlement.reservation.billing_account_id,
        usage_period_id=settlement.reservation.usage_period_id,
        reservation_id=settlement.reservation.id,
        request_id=str(run["request_id"]),
        operation_type="work",
        item_index=0,
        item_type="model",
        provider="claude",
        model=usage.model,
        input_tokens=usage.prompt_tokens,
        normal_input_tokens=max(
            0, usage.prompt_tokens - usage.cached_input_tokens - usage.cache_write_tokens
        ),
        cached_input_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        input_credits=min(billed, usage.input_credits),
        normal_input_credits=min(billed, usage.input_credits),
        output_credits=min(max(0, billed - usage.input_credits), usage.output_credits),
        fixed_credits=max(0, billed - min(billed, usage.model_credits)),
        total_credits=billed,
        provider_cost_usd=usage.provider_cost_usd,
        uncached_equivalent_credits=billed,
        usage_estimated=True,
        pricing_version=usage.pricing_version,
        metadata={
            "credit_activity_id": str(run["request_id"]),
            "initial_query": privacy_service.sanitize_user_message_for_storage(
                str(run["instruction"])
            )[:500],
            "provider_model_id": run.get("provider_model_id"),
            "billing_model_id": run.get("billing_model_id"),
            "billing_model_source": run.get("billing_model_source"),
            "provider_agent_id": run.get("provider_agent_id"),
            "provider_agent_version": run.get("provider_agent_version"),
            "managed_runtime_credits": usage.runtime_credits,
            "managed_active_seconds": usage.active_seconds,
            "managed_web_credits": usage.web_credits,
            "managed_web_searches": usage.web_searches,
            "managed_component_credits": usage.component_credits,
            "managed_provider_floor_credits": usage.provider_floor_credits,
            "managed_reported_provider_cost_usd": usage.reported_provider_cost_usd,
            "managed_reconstructed_provider_cost_usd": usage.reconstructed_provider_cost_usd,
            "calculated_credits": usage.total_credits,
            "unbilled_credits": max(0, usage.total_credits - billed),
        },
    )
    return billed


def _connection_id_for_provider_tool(
    db: Any,
    *,
    work_run_id: UUID,
    event_payload: Mapping[str, object],
) -> UUID | None:
    provider_name = str(event_payload.get("mcp_server_name") or "").strip()
    if not provider_name:
        return None
    for snapshot in repository.list_run_connection_snapshots(db, work_run_id):
        connection_id = snapshot["connection_id"]
        expected = f"cortex_{str(connection_id).replace('-', '')[:16]}"
        if provider_name == expected:
            return connection_id
    return None


def _has_saved_write_grant(
    work_session: Mapping[str, object],
    *,
    connection_id: UUID | None,
    tool_name: str,
) -> bool:
    if connection_id is None:
        return False
    policy = work_session.get("default_tool_policy")
    grants = policy.get("allowed_write_tools", []) if isinstance(policy, Mapping) else []
    expected = {"connection_id": str(connection_id), "tool_name": tool_name}
    return any(isinstance(item, Mapping) and dict(item) == expected for item in grants)


def _provider_session_is_interruptible(provider_status: object) -> bool:
    return str(provider_status or "").strip().lower() in _INTERRUPTIBLE_PROVIDER_SESSION_STATUSES


def _latest_provider_stop_reason(provider_events: Sequence[Any]) -> str | None:
    for event in reversed(provider_events):
        if str(event.payload.get("provider_type") or "") == "session.status_idle":
            return event.stop_reason
    return None


def _provider_events_for_run(
    provider_events: Sequence[Any], run: Mapping[str, object]
) -> list[Any]:
    snapshot = run.get("configuration_snapshot")
    configuration = snapshot if isinstance(snapshot, Mapping) else {}
    baseline_raw = configuration.get("provider_event_baseline_ids")
    baseline_values = baseline_raw if isinstance(baseline_raw, list) else []
    baseline_ids = {str(value) for value in baseline_values if isinstance(value, (str, int))}
    return [event for event in provider_events if event.id not in baseline_ids]


def _provider_confirmed_tool_ids(provider_events: Sequence[Any]) -> set[str]:
    return {
        str(event.payload.get("tool_use_id"))
        for event in provider_events
        if str(event.payload.get("provider_type") or "") == "user.tool_confirmation"
        and event.payload.get("tool_use_id")
    }


def reconcile_work_run(
    *,
    user_id: UUID,
    work_run_id: UUID,
    provider: AgentProvider | None = None,
    config: WorkConfig | None = None,
) -> dict[str, Any]:
    resolved = require_work_enabled(config)
    agent = provider or get_agent_provider()
    lease_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    with persistence_service.db_uow() as db:
        run = repository.get_work_run_for_user(db, work_run_id, user_id)
        if run is None:
            raise work_http_error(
                status.HTTP_404_NOT_FOUND, "work_run_not_found", "Work run not found."
            )
        if run["status"] in repository.TERMINAL_RUN_STATUSES:
            return run
        if not repository.claim_sync_lease(db, work_run_id=work_run_id, lease_owner=lease_owner):
            return run
        work_session_id = UUID(str(run["work_session_id"]))
        work_session = repository.get_work_session(db, work_session_id)
        if work_session is None:
            raise work_http_error(
                status.HTTP_409_CONFLICT,
                "work_session_missing",
                "The Work session is unavailable.",
            )
    provider_session_id = str(
        work_session.get("provider_session_id") or run.get("provider_run_id") or ""
    )
    if not provider_session_id:
        with persistence_service.db_uow() as db:
            repository.release_sync_lease(db, work_run_id=work_run_id, lease_owner=lease_owner)
        raise work_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "work_provider_session_missing",
            "The managed agent session is unavailable.",
        )
    try:
        provider_events = _provider_events_for_run(
            agent.list_events(provider_session_id),
            run,
        )
        provider_session = agent.get_session(provider_session_id)
        identity = _provider_billing_identity(provider_session, provider_name=agent.name)
        existing_provider_model = str(run.get("provider_model_id") or "").strip()
        if existing_provider_model and existing_provider_model != identity["provider_model_id"]:
            raise WorkBillingIdentityError(
                "The Managed Agent session model changed after the Work run started"
            )
        if not existing_provider_model or not run.get("billing_model_id"):
            configuration = dict(run.get("configuration_snapshot") or {})
            configuration.update(identity)
            with persistence_service.db_uow() as db:
                repository.update_work_run(
                    db,
                    work_run_id,
                    configuration_snapshot=configuration,
                    provider_model_id=str(identity["provider_model_id"]),
                    billing_model_id=str(identity["billing_model_id"]),
                    billing_model_source=str(identity["billing_model_source"]),
                    provider_agent_id=(
                        str(identity["provider_agent_id"])
                        if identity.get("provider_agent_id")
                        else None
                    ),
                    provider_agent_version=(
                        int(str(identity["provider_agent_version"]))
                        if identity.get("provider_agent_version")
                        else None
                    ),
                )
            run = {
                **run,
                **identity,
                "configuration_snapshot": configuration,
            }
        usage = _usage_for_run(run, provider_session.usage)
        max_output_tokens = max(1, int(run.get("max_output_tokens") or 40_000))
        finalize_threshold = min(
            resolved.output_finalize_token_threshold,
            max_output_tokens - 1,
        )
        provider_interruptible = _provider_session_is_interruptible(provider_session.status)
        output_guardrail = resolve_output_guardrail(
            output_tokens=usage.output_tokens,
            max_output_tokens=max_output_tokens,
            finalize_threshold=finalize_threshold,
            provider_interruptible=provider_interruptible,
            finalize_already_requested=bool(run.get("output_finalize_requested_at")),
            interrupt_already_requested=bool(run.get("output_limit_interrupt_requested_at")),
        )
        output_limit_reached = output_guardrail.limit_reached
        send_output_interrupt = output_guardrail.interrupt
        send_finalize_instruction = output_guardrail.finalize
        budget_reached = usage.total_credits >= int(run["max_credit_budget"])
        if budget_reached:
            send_output_interrupt = False
        terminal_status: str | None = "budget_exhausted" if budget_reached else None
        stop_reason: str | None = "credit_limit_reached" if budget_reached else None
        expired_confirmations: list[tuple[UUID, str]] = []
        with persistence_service.db_uow() as db:
            if send_finalize_instruction:
                repository.update_work_run(
                    db,
                    work_run_id,
                    output_finalize_requested=True,
                )
                repository.append_work_event(
                    db,
                    work_run_id=work_run_id,
                    event_type="output_finalizing",
                    display_message="Preparing the final deliverable",
                    payload={
                        "output_tokens": usage.output_tokens,
                        "max_output_tokens": max_output_tokens,
                    },
                    provider_event_id=f"output-finalize:{work_run_id}",
                )
            if send_output_interrupt:
                repository.update_work_run(
                    db,
                    work_run_id,
                    stop_reason="output_limit_interrupt_requested",
                    output_limit_interrupt_requested=True,
                )
                repository.append_work_event(
                    db,
                    work_run_id=work_run_id,
                    event_type="output_limit_interrupt_requested",
                    display_message="Output limit reached; stopping Work",
                    payload={
                        "output_tokens": usage.output_tokens,
                        "max_output_tokens": max_output_tokens,
                    },
                    provider_event_id=f"output-interrupt:{work_run_id}",
                )
            provider_by_id = {event.id: event for event in provider_events}
            provider_confirmed_tool_ids = _provider_confirmed_tool_ids(provider_events)
            current_work_session = repository.get_work_session(db, work_session_id) or work_session
            auto_confirm: list[str] = []
            auto_deny: list[str] = []
            approval_cutoff = datetime.now(UTC) - timedelta(
                seconds=resolved.approval_timeout_seconds
            )
            for approval in repository.expire_pending_approvals_for_run(
                db,
                work_run_id=work_run_id,
                user_id=user_id,
                requested_before=approval_cutoff,
            ):
                provider_call_id = str(approval.get("provider_call_id") or "")
                if provider_call_id:
                    expired_confirmations.append((UUID(str(approval["id"])), provider_call_id))
                repository.append_work_event(
                    db,
                    work_run_id=work_run_id,
                    event_type="approval_resolved",
                    display_message="Approval expired",
                    payload={
                        "approval_id": str(approval["id"]),
                        "decision": "expired",
                        "reason": "approval_timeout",
                    },
                )
            for event in provider_events:
                event_payload = dict(event.payload)
                if event.type == "tool_started":
                    tool_name = str(event_payload.get("tool_name") or "tool")
                    connection_id = _connection_id_for_provider_tool(
                        db,
                        work_run_id=work_run_id,
                        event_payload=event_payload,
                    )
                    repository.create_work_tool_call(
                        db,
                        work_run_id=work_run_id,
                        provider_call_id=event.id,
                        connection_id=connection_id,
                        tool_source=(
                            "mcp"
                            if str(event_payload.get("provider_type")) == "agent.mcp_tool_use"
                            else "builtin"
                        ),
                        tool_name=tool_name,
                        action_class=classify_action(tool_name),
                        request_summary={
                            "input_keys": _string_list(event_payload.get("input_keys"))
                        },
                    )
                elif event.type == "tool_completed":
                    completed_call_id = str(event_payload.get("tool_use_id") or "")
                    if completed_call_id:
                        repository.update_work_tool_call_status_by_provider_id(
                            db,
                            work_run_id=work_run_id,
                            provider_call_id=completed_call_id,
                            status="succeeded",
                        )
                if event.type == "approval_required":
                    approval_ids: list[str] = []
                    blocking = event_payload.get("blocking_event_ids")
                    for blocking_id in blocking if isinstance(blocking, list) else []:
                        if str(blocking_id) in provider_confirmed_tool_ids:
                            continue
                        tool_event = provider_by_id.get(str(blocking_id))
                        if tool_event is None:
                            continue
                        tool_name = str(tool_event.payload.get("tool_name") or "tool")
                        action_class = classify_action(tool_name)
                        connection_id = _connection_id_for_provider_tool(
                            db,
                            work_run_id=work_run_id,
                            event_payload=tool_event.payload,
                        )
                        if action_class == "READ":
                            auto_confirm.append(str(blocking_id))
                            continue
                        if action_class == "WRITE" and _has_saved_write_grant(
                            current_work_session,
                            connection_id=connection_id,
                            tool_name=tool_name,
                        ):
                            auto_confirm.append(str(blocking_id))
                            continue
                        existing_call = repository.find_tool_call_by_provider_id(
                            db,
                            work_run_id=work_run_id,
                            provider_call_id=str(blocking_id),
                        )
                        if existing_call is None:
                            existing_call = repository.create_work_tool_call(
                                db,
                                work_run_id=work_run_id,
                                provider_call_id=str(blocking_id),
                                connection_id=connection_id,
                                tool_source=(
                                    "mcp"
                                    if str(tool_event.payload.get("provider_type"))
                                    == "agent.mcp_tool_use"
                                    else "builtin"
                                ),
                                tool_name=tool_name,
                                action_class=action_class,
                                request_summary={
                                    "input_keys": _string_list(tool_event.payload.get("input_keys"))
                                },
                            )
                            approval = repository.create_approval_for_tool_call(
                                db,
                                work_run_id=work_run_id,
                                tool_call_id=existing_call["id"],
                                connection_id=connection_id,
                                action_class=action_class,
                                tool_name=tool_name,
                                description=f"Allow {tool_name} to perform this {action_class.lower().replace('_', ' ')} action?",
                                request_payload=_mapping_or_empty(
                                    tool_event.payload.get("input_summary")
                                ),
                            )
                            if resolved.action_tools_enabled:
                                approval_ids.append(str(approval["id"]))
                            else:
                                repository.decide_approval(
                                    db,
                                    approval_id=approval["id"],
                                    user_id=user_id,
                                    decision="denied",
                                )
                                auto_deny.append(str(blocking_id))
                                repository.append_work_event(
                                    db,
                                    work_run_id=work_run_id,
                                    event_type="approval_resolved",
                                    display_message="Action tools are disabled",
                                    payload={
                                        "approval_id": str(approval["id"]),
                                        "decision": "denied",
                                        "reason": "work_action_tools_disabled",
                                    },
                                )
                        else:
                            pending = repository.list_pending_approvals_for_run(
                                db, work_run_id, user_id
                            )
                            matching = [
                                item
                                for item in pending
                                if item["tool_call_id"] == existing_call["id"]
                            ]
                            if not matching and existing_call.get("status") in {
                                "requested",
                                "running",
                            }:
                                matching = [
                                    repository.create_approval_for_tool_call(
                                        db,
                                        work_run_id=work_run_id,
                                        tool_call_id=existing_call["id"],
                                        connection_id=connection_id,
                                        action_class=action_class,
                                        tool_name=tool_name,
                                        description=f"Allow {tool_name} to perform this {action_class.lower().replace('_', ' ')} action?",
                                        request_payload=_mapping_or_empty(
                                            tool_event.payload.get("input_summary")
                                        ),
                                    )
                                ]
                            if resolved.action_tools_enabled:
                                approval_ids.extend(str(item["id"]) for item in matching)
                            else:
                                for item in matching:
                                    repository.decide_approval(
                                        db,
                                        approval_id=item["id"],
                                        user_id=user_id,
                                        decision="denied",
                                    )
                                    auto_deny.append(str(blocking_id))
                    if approval_ids:
                        event_payload["approval_ids"] = approval_ids
                        terminal_status = None
                        repository.update_work_run(db, work_run_id, status="waiting_for_approval")
                        repository.update_work_session(
                            db, work_session_id, status="waiting_for_approval"
                        )
                repository.append_work_event(
                    db,
                    work_run_id=work_run_id,
                    event_type=event.type,
                    display_message=event.display_message,
                    payload=event_payload,
                    provider_event_id=event.id,
                )
                if event.terminal_status:
                    terminal_status = event.terminal_status
                    stop_reason = event.stop_reason
            provider_terminal_seen = any(event.terminal_status for event in provider_events)
            if output_limit_reached and (not provider_interruptible or provider_terminal_seen):
                terminal_status = "output_limit_reached"
                stop_reason = "output_token_limit_reached"
            elif send_output_interrupt and not budget_reached:
                terminal_status = None
                stop_reason = "output_limit_interrupt_requested"
            current_status = terminal_status or (
                "running" if provider_session.status == "running" else str(run["status"])
            )
            terminal = current_status in repository.TERMINAL_RUN_STATUSES
            actual = usage.total_credits
            if terminal:
                billed = _settle_work_billing(db, run, usage)
                actual = billed
                if not any(event.terminal_status == terminal_status for event in provider_events):
                    repository.append_work_event(
                        db,
                        work_run_id=work_run_id,
                        event_type=(
                            "budget_exhausted"
                            if terminal_status == "budget_exhausted"
                            else (
                                "output_limit_reached"
                                if terminal_status == "output_limit_reached"
                                else f"run_{terminal_status}"
                            )
                        ),
                        display_message=(
                            "Credit budget reached"
                            if terminal_status == "budget_exhausted"
                            else (
                                "Output limit reached"
                                if terminal_status == "output_limit_reached"
                                else None
                            )
                        ),
                        payload={"stop_reason": stop_reason},
                    )
            updated = repository.update_work_run(
                db,
                work_run_id,
                status=current_status,
                actual_credits=actual,
                actual_output_tokens=usage.output_tokens,
                usage_snapshot=dict(provider_session.usage),
                provider_cost_snapshot={
                    "provider": agent.name,
                    "estimated_provider_cost_usd": usage.provider_cost_usd,
                    "reported_provider_cost_usd": usage.reported_provider_cost_usd,
                    "reconstructed_provider_cost_usd": usage.reconstructed_provider_cost_usd,
                    "provider_floor_credits": usage.provider_floor_credits,
                    "provider_model_id": identity["provider_model_id"],
                    "billing_model_id": identity["billing_model_id"],
                    "billing_model_source": identity["billing_model_source"],
                    "provider_agent_id": identity.get("provider_agent_id"),
                    "provider_agent_version": identity.get("provider_agent_version"),
                    "provider_model_effort": identity.get("provider_model_effort"),
                    "provider_model_speed": identity.get("provider_model_speed"),
                },
                stop_reason=stop_reason,
                completed=terminal,
            )
            repository.update_work_session(
                db,
                work_session_id,
                status=(
                    "completed"
                    if current_status in {"completed", "output_limit_reached"}
                    else (
                        current_status
                        if current_status in {"failed", "cancelled", "waiting_for_approval"}
                        else "running"
                    )
                ),
            )
            repository.release_sync_lease(db, work_run_id=work_run_id, lease_owner=lease_owner)
            result = updated or run
        if send_finalize_instruction:
            try:
                agent.send_instruction(
                    provider_session_id,
                    "Cortex output guardrail: stop optional exploration now, finish the "
                    "strongest deliverable possible from verified work, save intended output "
                    "files, and concisely report anything unfinished.",
                )
                logger.info(
                    "Work output finalization requested",
                    extra={
                        "extra_fields": {
                            "event": "work.output_limit.finalize_requested",
                            "work_run_id": str(work_run_id),
                            "output_tokens": usage.output_tokens,
                            "max_output_tokens": max_output_tokens,
                        }
                    },
                )
            except Exception:
                with persistence_service.db_uow() as db:
                    repository.clear_work_output_finalize_request(db, work_run_id)
                logger.exception(
                    "Work output finalization request failed",
                    extra={
                        "extra_fields": {
                            "event": "work.output_limit.finalize_failed",
                            "work_run_id": str(work_run_id),
                        }
                    },
                )
        if send_output_interrupt:
            try:
                agent.interrupt(provider_session_id)
                auto_confirm.clear()
                auto_deny.clear()
                logger.info(
                    "Work output limit interrupt sent",
                    extra={
                        "extra_fields": {
                            "event": "work.output_limit.interrupt_sent",
                            "work_run_id": str(work_run_id),
                            "output_tokens": usage.output_tokens,
                            "max_output_tokens": max_output_tokens,
                        }
                    },
                )
            except Exception:
                with persistence_service.db_uow() as db:
                    repository.clear_work_output_interrupt_request(db, work_run_id)
                raise
        for tool_use_id in auto_confirm:
            try:
                agent.confirm_tool(
                    provider_session_id,
                    tool_use_id,
                    allow=True,
                    deny_message=None,
                )
            except Exception:
                logger.exception(
                    "Automatic read-tool confirmation failed",
                    extra={
                        "extra_fields": {
                            "event": "work.tool.auto_confirm.failed",
                            "work_run_id": str(work_run_id),
                        }
                    },
                )
        for tool_use_id in auto_deny:
            try:
                agent.confirm_tool(
                    provider_session_id,
                    tool_use_id,
                    allow=False,
                    deny_message="Cortex Work action tools are disabled for this environment.",
                )
            except Exception:
                logger.exception(
                    "Automatic action-tool denial failed",
                    extra={
                        "extra_fields": {
                            "event": "work.tool.auto_deny.failed",
                            "work_run_id": str(work_run_id),
                        }
                    },
                )
        for approval_id, tool_use_id in expired_confirmations:
            try:
                agent.confirm_tool(
                    provider_session_id,
                    tool_use_id,
                    allow=False,
                    deny_message="Cortex Work approval expired before a decision was received.",
                )
            except Exception:
                with persistence_service.db_uow() as db:
                    reopened = repository.reopen_expired_approval_after_provider_failure(
                        db,
                        approval_id=approval_id,
                    )
                    if reopened:
                        repository.update_work_run(
                            db,
                            work_run_id,
                            status="waiting_for_approval",
                        )
                        repository.update_work_session(
                            db,
                            work_session_id,
                            status="waiting_for_approval",
                        )
                        repository.append_work_event(
                            db,
                            work_run_id=work_run_id,
                            event_type="approval_required",
                            display_message="Approval required",
                            payload={
                                "approval_ids": [str(approval_id)],
                                "reason": "approval_expiry_confirmation_failed",
                            },
                        )
                logger.exception(
                    "Expired approval provider denial failed",
                    extra={
                        "extra_fields": {
                            "event": "work.approval.expiry_confirm.failed",
                            "work_run_id": str(work_run_id),
                            "approval_id": str(approval_id),
                        }
                    },
                )
        if (
            result["status"] in repository.TERMINAL_RUN_STATUSES
            and resolved.artifact_import_enabled
        ):
            try:
                import_work_artifacts(
                    user_id=user_id,
                    work_run_id=work_run_id,
                    provider=agent,
                )
            except Exception:
                logger.exception(
                    "Work artifact import failed",
                    extra={
                        "extra_fields": {
                            "event": "work.artifact.import.failed",
                            "work_run_id": str(work_run_id),
                        }
                    },
                )
        return result
    except HTTPException:
        with persistence_service.db_uow() as db:
            repository.release_sync_lease(db, work_run_id=work_run_id, lease_owner=lease_owner)
        raise
    except Exception as exc:
        logger.exception(
            "Work reconciliation failed",
            extra={
                "extra_fields": {"event": "work.reconcile.failed", "work_run_id": str(work_run_id)}
            },
        )
        with persistence_service.db_uow() as db:
            repository.release_sync_lease(db, work_run_id=work_run_id, lease_owner=lease_owner)
        raise work_http_error(
            status.HTTP_502_BAD_GATEWAY,
            "work_reconciliation_failed",
            "Work progress could not be refreshed.",
        ) from exc


def cancel_work_run(
    *, user_id: UUID, work_run_id: UUID, provider: AgentProvider | None = None
) -> dict[str, Any]:
    config = require_work_enabled()
    agent = provider or get_agent_provider()
    with persistence_service.db_uow(commit_on_success=False) as db:
        run = repository.get_work_run_for_user(db, work_run_id, user_id)
        if run is None:
            raise work_http_error(
                status.HTTP_404_NOT_FOUND, "work_run_not_found", "Work run not found."
            )
        work_session_id = UUID(str(run["work_session_id"]))
        work_session = repository.get_work_session(db, work_session_id)
        if work_session is None:
            raise work_http_error(
                status.HTTP_409_CONFLICT,
                "work_session_missing",
                "The Work session is unavailable.",
            )
    if run["status"] in repository.TERMINAL_RUN_STATUSES:
        return run
    provider_session_id = str(
        work_session.get("provider_session_id") or run.get("provider_run_id") or ""
    )
    if provider_session_id:
        provider_session = agent.get_session(provider_session_id)
        if _provider_session_is_interruptible(provider_session.status):
            agent.interrupt(provider_session_id)
    # Reconcile first to capture provider usage, then force cancellation if the provider has not emitted it yet.
    try:
        reconcile_work_run(user_id=user_id, work_run_id=work_run_id, provider=agent, config=config)
    except HTTPException:
        pass
    with persistence_service.db_uow() as db:
        current = repository.get_work_run_for_user(db, work_run_id, user_id) or run
        if current["status"] != "cancelled":
            repository.append_work_event(
                db,
                work_run_id=work_run_id,
                event_type="run_cancelled",
                display_message="Work stopped",
                payload={"stop_reason": "user_cancelled"},
            )
            updated = repository.update_work_run(
                db, work_run_id, status="cancelled", stop_reason="user_cancelled", completed=True
            )
            repository.update_work_session(db, work_session_id, status="cancelled")
            reservation_id = current.get("billing_reservation_id")
            # Reconciliation normally settled the reservation from the provider's
            # final usage snapshot. Release only if it is still active.
            if isinstance(reservation_id, UUID):
                try:
                    release_usage(
                        db,
                        reservation_id=reservation_id,
                        reason="work_cancelled_before_billable_usage",
                    )
                except Exception:
                    pass
            return updated or current
        return current


def import_work_artifacts(
    *,
    user_id: UUID,
    work_run_id: UUID,
    provider: AgentProvider | None = None,
) -> list[dict[str, Any]]:
    """Import provider output files into Cortex-owned S3/uploaded_files records."""
    agent = provider or get_agent_provider()
    with persistence_service.db_uow(commit_on_success=False) as db:
        run = repository.get_work_run_for_user(db, work_run_id, user_id)
        if run is None:
            raise work_http_error(404, "work_run_not_found", "Work run not found.")
        work_session = repository.get_work_session(db, UUID(str(run["work_session_id"])))
        if work_session is None:
            raise work_http_error(409, "work_session_missing", "The Work session is unavailable.")
        effective = resolve_effective_subscription(db, user_id)
        input_rows = repository.list_work_session_files(
            db,
            UUID(str(run["work_session_id"])),
            user_id,
            role="input",
        )
    provider_session_id = str(
        run.get("provider_run_id") or work_session.get("provider_session_id") or ""
    )
    if not provider_session_id:
        return []
    input_provider_ids = {
        str(item.get("provider_file_id")) for item in input_rows if item.get("provider_file_id")
    }
    max_size = int(effective.plan.limits.max_file_bytes)
    storage = get_object_storage()
    imported: list[dict[str, Any]] = []
    for artifact in agent.list_artifacts(provider_session_id):
        if artifact.id in input_provider_ids or not artifact.downloadable:
            continue
        try:
            with persistence_service.db_uow(commit_on_success=False) as db:
                if (
                    repository.find_work_artifact_by_provider_file(
                        db, user_id=user_id, provider_file_id=artifact.id
                    )
                    is not None
                ):
                    continue
            filename = PurePosixPath(str(artifact.filename or "artifact").replace("\\", "/")).name
            filename = _SAFE_FILENAME.sub("_", filename).strip("._")[:180]
            if not filename or filename in {".", ".."}:
                logger.warning("Rejected unsafe Work artifact filename")
                continue
            if artifact.size_bytes is not None and (
                artifact.size_bytes <= 0 or artifact.size_bytes > max_size
            ):
                logger.warning(
                    "Rejected oversized Work artifact",
                    extra={"extra_fields": {"size_bytes": artifact.size_bytes}},
                )
                continue
            payload = agent.download_artifact(artifact.id)
            if not payload or len(payload) > max_size:
                logger.warning(
                    "Rejected invalid Work artifact payload",
                    extra={"extra_fields": {"size_bytes": len(payload)}},
                )
                continue
            mime_type = str(
                artifact.mime_type
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            ).lower()
            file_id = uuid4()
            today = datetime.now(UTC)
            key = "/".join(
                part
                for part in (
                    storage.key_prefix,
                    "users",
                    str(user_id),
                    "work",
                    f"{today:%Y/%m/%d}",
                    str(file_id),
                    filename,
                )
                if part
            )
            storage.put_bytes(
                key=key,
                payload=payload,
                content_type=mime_type,
                metadata={"cortex-file-id": str(file_id), "work-run-id": str(work_run_id)},
            )
            try:
                with persistence_service.db_uow() as db:
                    expires = today + timedelta(
                        hours=max(1, int(os.getenv("ATTACHMENTS_FILE_TTL_HOURS", "168")))
                    )
                    create_uploaded_file(
                        db,
                        file_id=file_id,
                        user_id=user_id,
                        original_filename=filename,
                        mime_type=mime_type,
                        size_bytes=len(payload),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        storage_bucket=storage.bucket,
                        storage_key=key,
                        status="ready",
                        ingestion_meta={
                            "source": "work_artifact",
                            "work_run_id": str(work_run_id),
                        },
                        expires_at=expires,
                    )
                    linked, _ = repository.attach_work_file(
                        db,
                        work_run_id=work_run_id,
                        user_id=user_id,
                        file_id=file_id,
                        role="artifact",
                        source="agent",
                        provider_file_id=artifact.id,
                        artifact_type=mime_type,
                        metadata={"imported_from_managed_session": True},
                    )
                    repository.append_work_event(
                        db,
                        work_run_id=work_run_id,
                        event_type="artifact_created",
                        display_message=f"Created {filename}",
                        payload={
                            "file_id": str(file_id),
                            "filename": filename,
                            "mime_type": mime_type,
                            "size_bytes": len(payload),
                        },
                        provider_event_id=f"artifact:{artifact.id}",
                    )
                    imported.append(linked)
            except Exception:
                try:
                    storage.delete_object(key=key)
                except Exception:
                    logger.exception("Failed to remove orphaned Work artifact object")
                raise
        except Exception:
            logger.exception(
                "Work artifact import item failed",
                extra={
                    "extra_fields": {
                        "event": "work.artifact.import.item_failed",
                        "work_run_id": str(work_run_id),
                        "filename": str(artifact.filename or "artifact")[:180],
                    }
                },
            )
            continue
    return imported
