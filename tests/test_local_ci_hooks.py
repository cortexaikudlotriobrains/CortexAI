from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from scripts.run_local_ci import (
    GITLEAKS_VERSION,
    GateFailure,
    _require_executable,
    _run_api_image_build,
    _run_pytest,
    classify_ci_paths,
    python_files,
)
from scripts import release_gate

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ci_path_classification_matches_workflow_boundaries():
    assert classify_ci_paths(["server/routes/chat.py"]) == (True, False)
    assert classify_ci_paths(["frontend-react/src/App.tsx"]) == (False, True)
    assert classify_ci_paths(["README.md"]) == (True, True)
    assert classify_ci_paths([".pre-commit-config.yaml"]) == (True, True)
    assert classify_ci_paths(["docs/FASTAPI_README.md"]) == (False, False)


def test_python_files_keep_only_existing_added_or_modified_candidates(monkeypatch):
    with TemporaryDirectory(prefix="local-ci-hook-", dir=REPO_ROOT) as temporary:
        root = Path(temporary)
        monkeypatch.setattr("scripts.run_local_ci.REPO_ROOT", root)
        (root / "server").mkdir()
        (root / "server" / "app.py").write_text("", encoding="utf-8")

        assert python_files(["server/app.py", "server/deleted.py", "README.md"]) == [
            "server/app.py"
        ]


def test_local_runner_pins_ci_gitleaks_version():
    runner = (REPO_ROOT / "scripts" / "run_local_ci.py").read_text(encoding="utf-8")

    assert f'GITLEAKS_VERSION = "{GITLEAKS_VERSION}"' in runner


def test_executable_lookup_uses_platform_path_resolution(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_local_ci.shutil.which",
        lambda name: "C:/tools/docker.exe" if name == "docker" else None,
    )

    assert _require_executable("docker") == "C:/tools/docker.exe"


def test_pytest_uses_the_gate_private_temp_directory(monkeypatch, tmp_path):
    commands: list[tuple[tuple[str, ...], Path]] = []

    def _fake_run(command, *, cwd, **_kwargs):
        commands.append((tuple(command), cwd))
        return SimpleNamespace(returncode=0, stdout="")

    snapshot = tmp_path / "tree"
    basetemp = tmp_path / "pytest"
    python = tmp_path / "venv" / "python"
    monkeypatch.setattr("scripts.run_local_ci._run", _fake_run)

    _run_pytest(
        python,
        ("tests/test_example.py",),
        cwd=snapshot,
        basetemp=basetemp,
    )

    assert commands == [
        (
            (
                str(python),
                "-m",
                "pytest",
                "tests/test_example.py",
                "-q",
                "--basetemp",
                str(basetemp),
            ),
            snapshot,
        )
    ]


def test_release_gate_uses_a_repo_private_pytest_temp_directory(monkeypatch):
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(release_gate, "_iter_python_files", lambda: [])
    monkeypatch.setattr(
        release_gate,
        "_run",
        lambda command, _label: commands.append(tuple(command)),
    )

    assert release_gate.main() == 0

    pytest_command = next(command for command in commands if "pytest" in command)
    basetemp = Path(pytest_command[pytest_command.index("--basetemp") + 1])
    assert basetemp.name == "pytest"
    assert basetemp.parent.parent == REPO_ROOT


def test_api_image_build_defers_when_docker_is_unavailable(monkeypatch, capsys):
    monkeypatch.delenv("CORTEX_CI_REQUIRE_DOCKER", raising=False)
    monkeypatch.setattr("scripts.run_local_ci.shutil.which", lambda _name: None)

    assert _run_api_image_build(REPO_ROOT) is False
    assert "deferred to GitHub Actions" in capsys.readouterr().out


def test_api_image_build_can_require_docker(monkeypatch):
    monkeypatch.setenv("CORTEX_CI_REQUIRE_DOCKER", "1")
    monkeypatch.setattr("scripts.run_local_ci.shutil.which", lambda _name: None)

    with pytest.raises(GateFailure, match="Docker CLI was not found"):
        _run_api_image_build(REPO_ROOT)


def test_api_image_build_blocks_when_available_build_fails(monkeypatch):
    commands: list[tuple[str, ...]] = []

    def _fake_run(command, **_kwargs):
        normalized = tuple(command)
        commands.append(normalized)
        if "build" in normalized:
            raise GateFailure("Command failed with exit code 1: docker build")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.delenv("CORTEX_CI_REQUIRE_DOCKER", raising=False)
    monkeypatch.setattr("scripts.run_local_ci.shutil.which", lambda _name: "C:/tools/docker.exe")
    monkeypatch.setattr("scripts.run_local_ci._run", _fake_run)

    with pytest.raises(GateFailure, match="Command failed"):
        _run_api_image_build(REPO_ROOT)

    assert any("build" in command for command in commands)
