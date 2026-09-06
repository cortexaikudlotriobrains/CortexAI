from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from server.app import create_app
from server.dependencies import AuthResult, get_auth
from server.routes import tools, work
from server.schemas.work import WorkApprovalDecisionDTO, WorkRunCreateDTO


def test_work_and_tool_routes_are_registered_with_expected_methods():
    app = create_app()
    included = [route.original_router for route in app.routes if hasattr(route, "original_router")]
    assert any(router is work.router for router in included)
    assert any(router is tools.router for router in included)
    methods_by_path: dict[str, set[str]] = {}
    for route in [*work.router.routes, *tools.router.routes]:
        if hasattr(route, "methods"):
            methods_by_path.setdefault(route.path, set()).update(route.methods or set())
    expected = {
        "/v1/work/sessions": {"GET", "POST"},
        "/v1/work/sessions/{work_session_id}/runs": {"GET", "POST"},
        "/v1/work/sessions/{work_session_id}/instructions": {"POST"},
        "/v1/work/runs/{run_id}": {"GET"},
        "/v1/work/runs/{run_id}/events": {"GET"},
        "/v1/work/runs/{run_id}/stream": {"GET"},
        "/v1/work/runs/{run_id}/cancel": {"POST"},
        "/v1/work/runs/{run_id}/artifacts": {"GET"},
        "/v1/work/approvals/{approval_id}/approve": {"POST"},
        "/v1/work/approvals/{approval_id}/deny": {"POST"},
        "/v1/tools/catalog": {"GET"},
        "/v1/tools/connections": {"GET", "POST"},
        "/v1/tools/connections/{connection_id}/test": {"POST"},
        "/v1/tools/{connector_key}/oauth/start": {"POST"},
        "/v1/tools/{connector_key}/oauth/callback": {"GET"},
    }
    for path, methods in expected.items():
        assert methods <= methods_by_path[path]


def test_work_request_contract_normalizes_and_rejects_duplicate_resources():
    request = WorkRunCreateDTO(
        instruction="  Prepare the report  ",
        input_file_ids=[],
        enabled_connection_ids=[],
        max_credit_budget=25_000,
    )
    assert request.instruction == "Prepare the report"
    assert request.web_mode == "auto"
    assert WorkRunCreateDTO(instruction="Current ticket prices", web_enabled=True).web_mode == "on"
    assert WorkRunCreateDTO(instruction="Use only this file", web_enabled=False).web_mode == "off"
    assert WorkApprovalDecisionDTO(remember=True).remember is True
    duplicate = uuid4()
    with pytest.raises(ValidationError, match="duplicate IDs"):
        WorkRunCreateDTO(
            instruction="Prepare the report",
            input_file_ids=[duplicate, duplicate],
        )


def test_terminal_artifact_listing_retries_import_before_returning_files(monkeypatch):
    user_id = uuid4()
    run_id = uuid4()
    file_id = uuid4()
    artifact_id = uuid4()
    retried: list[tuple] = []

    @contextmanager
    def fake_db_uow(*, commit_on_success=True):
        del commit_on_success
        yield object()

    monkeypatch.setattr(work, "_identity", lambda *_: user_id)
    monkeypatch.setattr(work.persistence_service, "db_uow", fake_db_uow)
    monkeypatch.setattr(
        work.repository,
        "get_work_run_for_user",
        lambda *_: {"id": run_id, "status": "completed"},
    )
    monkeypatch.setattr(
        work.service,
        "import_work_artifacts",
        lambda **kwargs: retried.append((kwargs["user_id"], kwargs["work_run_id"])),
    )
    monkeypatch.setattr(
        work.repository,
        "list_work_run_files",
        lambda *_args, **_kwargs: [
            {
                "id": artifact_id,
                "file_id": file_id,
                "role": "artifact",
                "source": "agent",
                "original_filename": "SECURITY_ANALYSIS_REPORT.md",
                "mime_type": "text/markdown",
                "size_bytes": 4096,
                "artifact_type": "report",
                "metadata": {},
                "created_at": datetime(2026, 8, 24, tzinfo=UTC),
            }
        ],
    )
    monkeypatch.setattr(
        work,
        "load_work_config",
        lambda: type("Config", (), {"artifact_import_enabled": True})(),
    )
    app = FastAPI()
    app.include_router(work.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=user_id,
    )

    with TestClient(app) as client:
        response = client.get(f"/v1/work/runs/{run_id}/artifacts")

    assert response.status_code == 200
    assert response.json()[0]["filename"] == "SECURITY_ANALYSIS_REPORT.md"
    assert retried == [(user_id, run_id)]
