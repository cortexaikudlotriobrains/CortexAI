#!/usr/bin/env python3
"""Run CortexAI's local Git-hook quality and CI-parity gates."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_REF = "origin/develop"
GITLEAKS_VERSION = "8.30.1"
GITLEAKS_RELEASE_BASE = (
    f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}"
)
GITLEAKS_ASSETS = {
    ("Darwin", "arm64"): (
        "gitleaks_8.30.1_darwin_arm64.tar.gz",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Darwin", "x86_64"): (
        "gitleaks_8.30.1_darwin_x64.tar.gz",
        "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    ),
    ("Linux", "aarch64"): (
        "gitleaks_8.30.1_linux_arm64.tar.gz",
        "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
    ),
    ("Linux", "x86_64"): (
        "gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
    ("Windows", "AMD64"): (
        "gitleaks_8.30.1_windows_x64.zip",
        "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
    ),
    ("Windows", "ARM64"): (
        "gitleaks_8.30.1_windows_arm64.zip",
        "b95f5e4f5c425cedca7ee203d9afd29597e692c4924a12ed42f970537c72cc0f",
    ),
}

BACKEND_PREFIXES = (
    "api/",
    "config/",
    "context/",
    "db/",
    "models/",
    "orchestrator/",
    "server/",
    "tests/",
    "tools/",
    "utils/",
)
BACKEND_FILES = {"run_server.py", "main.py", "pyproject.toml"}
REACT_PREFIXES = ("frontend-react/", "e2e/responsive/")
REACT_FILES = {
    "e2e/playwright.mobile.config.mjs",
    "e2e/playwright.desktop-ipad.config.mjs",
    "e2e/package.json",
    "e2e/package-lock.json",
}
SHARED_PREFIXES = (".github/workflows/", "scripts/")
SHARED_FILES = {"README.md", ".env.example", ".pre-commit-config.yaml", "pytest.ini"}
BACKEND_TESTS = (
    "tests/test_baseline_safety_rails.py",
    "tests/test_provider_catalog.py",
    "tests/test_client_registry_and_schema_dynamic.py",
    "tests/test_fastapi_contract_and_guardrails.py",
    "tests/test_dynamic_provider_discovery_e2e.py",
    "tests/test_component_boundaries.py",
    "tests/test_model_rate_cards.py",
    "tests/test_database_pricing_model_registry.py",
    "tests/test_model_catalog_tool.py",
    "tests/test_new_model_credits.py",
    "tests/test_registry_pricing_alignment.py",
    "tests/test_model_registry_capabilities.py",
    "tests/test_credit_calculator.py",
)


class GateFailure(RuntimeError):
    """A required local quality gate failed."""


def _display_command(command: Sequence[str]) -> str:
    return shlex.join(str(part) for part in command)


def _run(
    command: Sequence[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    normalized = [str(part) for part in command]
    print(f"\n$ {_display_command(normalized)}", flush=True)
    result = subprocess.run(
        normalized,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if check and result.returncode != 0:
        raise GateFailure(f"Command failed with exit code {result.returncode}: {_display_command(normalized)}")
    return result


def _git_output(*args: str) -> str:
    result = _run(("git", *args), capture_output=True)
    return result.stdout.strip()


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    return sorted({path.strip().replace("\\", "/") for path in paths if path.strip()})


def _split_git_lines(value: str) -> list[str]:
    return _normalize_paths(value.splitlines())


def staged_paths(*, diff_filter: str = "ACMR") -> list[str]:
    return _split_git_lines(
        _git_output("diff", "--cached", "--name-only", f"--diff-filter={diff_filter}")
    )


def branch_paths(base_ref: str, *, diff_filter: str = "ACMR") -> list[str]:
    return _split_git_lines(
        _git_output("diff", "--name-only", f"--diff-filter={diff_filter}", f"{base_ref}...HEAD")
    )


def python_files(paths: Iterable[str], *, root: Path | None = None) -> list[str]:
    root = root or REPO_ROOT
    return [
        path
        for path in _normalize_paths(paths)
        if path.endswith(".py") and (root / path).is_file()
    ]


def classify_ci_paths(paths: Iterable[str]) -> tuple[bool, bool]:
    """Return whether backend and React CI jobs apply to the paths."""
    backend = False
    react = False
    shared = False
    for path in _normalize_paths(paths):
        backend = backend or path.startswith(BACKEND_PREFIXES) or path in BACKEND_FILES
        backend = backend or (path.startswith("requirements") and path.endswith(".txt"))
        react = react or path.startswith(REACT_PREFIXES) or path in REACT_FILES
        shared = shared or path.startswith(SHARED_PREFIXES) or path in SHARED_FILES
    return backend or shared, react or shared


def _project_python() -> Path:
    configured = os.environ.get("CORTEX_CI_PYTHON", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            REPO_ROOT / "venv" / "Scripts" / "python.exe",
            REPO_ROOT / ".venv" / "Scripts" / "python.exe",
            REPO_ROOT / "venv" / "bin" / "python",
            REPO_ROOT / ".venv" / "bin" / "python",
            Path(sys.executable),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise GateFailure(
        "No project Python interpreter found. Create venv/.venv or set CORTEX_CI_PYTHON."
    )


def _report_python_version(python: Path) -> None:
    result = _run(
        (
            str(python),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ),
        capture_output=True,
    )
    version = result.stdout.strip()
    print(f"Local gate Python: {version}")
    if version != "3.12":
        print("WARNING: ci.yml uses Python 3.12; use a Python 3.12 project venv for exact parity.")


def _run_python_quality(python: Path, files: Sequence[str], *, cwd: Path = REPO_ROOT) -> None:
    if not files:
        print("No applicable Python files to lint or type-check.")
        return
    _run((str(python), "-m", "ruff", "check", *files), cwd=cwd)
    black = _run(
        (str(python), "-m", "black", "--check", *files), cwd=cwd, check=False
    )
    if black.returncode != 0:
        print("WARNING: Black is advisory in ci.yml and did not block this gate.")
    _run(
        (
            str(python),
            "-m",
            "mypy",
            "--explicit-package-bases",
            "--follow-imports=skip",
            *files,
        ),
        cwd=cwd,
    )


def _run_pytest(
    python: Path,
    tests: Sequence[str],
    *,
    cwd: Path,
    basetemp: Path,
) -> None:
    """Run pytest without relying on the user-wide temporary directory."""
    _run(
        (
            str(python),
            "-m",
            "pytest",
            *tests,
            "-q",
            "--basetemp",
            str(basetemp),
        ),
        cwd=cwd,
    )


def _gitleaks_asset() -> tuple[str, str]:
    system = platform.system()
    machine = platform.machine()
    aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}
    if system != "Windows":
        machine = aliases.get(machine.lower(), machine)
    elif machine.lower() in {"amd64", "x86_64", "x64"}:
        machine = "AMD64"
    elif machine.lower() in {"arm64", "aarch64"}:
        machine = "ARM64"
    asset = GITLEAKS_ASSETS.get((system, machine))
    if asset is None:
        raise GateFailure(f"Unsupported Gitleaks platform: {system} {platform.machine()}")
    return asset


def _tool_cache_dir() -> Path:
    git_common_dir = Path(_git_output("rev-parse", "--git-common-dir"))
    if not git_common_dir.is_absolute():
        git_common_dir = REPO_ROOT / git_common_dir
    return git_common_dir.resolve() / "cortex-tools" / "gitleaks" / GITLEAKS_VERSION


def _download_gitleaks() -> Path:
    asset_name, expected_sha256 = _gitleaks_asset()
    cache_dir = _tool_cache_dir()
    executable_name = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    executable = cache_dir / executable_name
    if executable.is_file():
        return executable

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / asset_name
    temporary_archive = archive.with_suffix(f"{archive.suffix}.download")
    url = f"{GITLEAKS_RELEASE_BASE}/{asset_name}"
    print(f"Downloading pinned Gitleaks {GITLEAKS_VERSION} from {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "CortexAI-local-CI-gate"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary_archive.open("wb") as output:
        shutil.copyfileobj(response, output)

    digest = hashlib.sha256(temporary_archive.read_bytes()).hexdigest()
    if digest != expected_sha256:
        temporary_archive.unlink(missing_ok=True)
        raise GateFailure(
            f"Gitleaks archive checksum mismatch: expected {expected_sha256}, got {digest}"
        )
    temporary_archive.replace(archive)

    if asset_name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            zip_member = next(
                name for name in bundle.namelist() if Path(name).name == executable_name
            )
            with bundle.open(zip_member) as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:gz") as bundle:
            tar_member = next(
                item for item in bundle.getmembers() if Path(item.name).name == executable_name
            )
            extracted = bundle.extractfile(tar_member)
            if extracted is None:
                raise GateFailure(f"Gitleaks executable missing from {asset_name}")
            with extracted, executable.open("wb") as output:
                shutil.copyfileobj(extracted, output)
    executable.chmod(executable.stat().st_mode | 0o111)
    archive.unlink(missing_ok=True)
    return executable


def _export_snapshot(destination: Path, source: str) -> None:
    if source == "index":
        prefix = f"{destination.as_posix()}/"
        _run(("git", "checkout-index", "--all", f"--prefix={prefix}"))
        return
    if source == "head":
        archive = destination.parent / "tracked-tree.zip"
        _run(("git", "archive", "--format=zip", f"--output={archive}", "HEAD"))
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
        return
    raise GateFailure(f"Unknown snapshot source: {source}")


def _run_gitleaks(snapshot: Path) -> None:
    gitleaks = _download_gitleaks()
    version = _run((str(gitleaks), "version"), capture_output=True).stdout.strip()
    if version != GITLEAKS_VERSION:
        raise GateFailure(
            f"Expected Gitleaks {GITLEAKS_VERSION}, but {gitleaks} reports {version}"
        )
    _run(
        (
            str(gitleaks),
            "dir",
            str(snapshot),
            "--no-banner",
            "--redact",
            "--exit-code",
            "1",
        )
    )


def _require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise GateFailure(f"Required executable '{name}' was not found on PATH.")
    return executable


def _docker_required() -> bool:
    return os.environ.get("CORTEX_CI_REQUIRE_DOCKER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _run_api_image_build(snapshot: Path) -> bool:
    docker = shutil.which("docker")
    if docker is None:
        message = "Docker CLI was not found on PATH; API image build is deferred to GitHub Actions."
        if _docker_required():
            raise GateFailure(message)
        print(f"WARNING: {message}")
        return False

    daemon = _run(
        (docker, "version", "--format", "{{.Server.Version}}"),
        cwd=snapshot,
        check=False,
        capture_output=True,
    )
    if daemon.returncode != 0:
        message = "Docker daemon is unavailable; API image build is deferred to GitHub Actions."
        if _docker_required():
            raise GateFailure(message)
        print(f"WARNING: {message}")
        return False

    _run(
        (docker, "build", "-f", "Dockerfile.api", "-t", "cortexai-api:ci", "."),
        cwd=snapshot,
    )
    _run((docker, "image", "inspect", "cortexai-api:ci"), cwd=snapshot)
    return True


def _refresh_base_ref(base_ref: str) -> None:
    if os.environ.get("CORTEX_CI_SKIP_BASE_FETCH") == "1":
        return
    if "/" not in base_ref:
        return
    remote, branch = base_ref.split("/", maxsplit=1)
    _run(("git", "fetch", "--quiet", remote, branch))


def run_pre_commit() -> None:
    print("=== CortexAI pre-commit gate ===")
    staged = staged_paths()
    if not staged:
        print("No staged paths detected.")
        return
    python = _project_python()
    _report_python_version(python)
    with tempfile.TemporaryDirectory(prefix="cortex-pre-commit-") as temporary:
        snapshot = Path(temporary) / "tree"
        pytest_basetemp = Path(temporary) / "pytest"
        snapshot.mkdir()
        _export_snapshot(snapshot, "index")
        _run_gitleaks(snapshot)
        staged_python = python_files(staged_paths(diff_filter="AM"), root=snapshot)
        _run_python_quality(python, staged_python, cwd=snapshot)

        staged_tests = [
            path
            for path in staged_python
            if path.startswith("tests/test_") and path.endswith(".py")
        ]
        backend_required, _ = classify_ci_paths(staged)
        if staged_tests:
            _run_pytest(
                python,
                staged_tests,
                cwd=snapshot,
                basetemp=pytest_basetemp,
            )
        elif backend_required:
            _run_pytest(
                python,
                ("tests/test_component_boundaries.py",),
                cwd=snapshot,
                basetemp=pytest_basetemp,
            )
    print("\nPre-commit gate passed. Applicable ci.yml checks run automatically before push.")


def run_pre_push(base_ref: str) -> None:
    print("=== CortexAI pre-push ci.yml parity gate ===")
    _refresh_base_ref(base_ref)
    _git_output("rev-parse", "--verify", base_ref)
    changed = branch_paths(base_ref)
    backend_required, react_required = classify_ci_paths(changed)
    print(f"Base ref: {base_ref}")
    print(f"Changed paths: {len(changed)}")
    print(f"Backend jobs required: {backend_required}")
    print(f"React jobs required: {react_required}")

    python = _project_python()
    _report_python_version(python)
    api_image_verified = True
    with tempfile.TemporaryDirectory(prefix="cortex-pre-push-") as temporary:
        snapshot = Path(temporary) / "tree"
        pytest_basetemp = Path(temporary) / "pytest"
        snapshot.mkdir()
        _export_snapshot(snapshot, "head")
        _run_gitleaks(snapshot)

        if backend_required:
            changed_python = python_files(
                branch_paths(base_ref, diff_filter="AM"), root=snapshot
            )
            _run_python_quality(python, changed_python, cwd=snapshot)
            # Same as ci.yml "Model catalog rules".
            _run((str(python), "scripts/models.py", "check"), cwd=snapshot)
            _run_pytest(
                python,
                BACKEND_TESTS,
                cwd=snapshot,
                basetemp=pytest_basetemp,
            )
            _run(
                (
                    str(python),
                    "-m",
                    "pip_audit",
                    "-r",
                    "requirements.txt",
                    "-r",
                    "requirements-dev.txt",
                ),
                cwd=snapshot,
            )

        if react_required:
            npm = _require_executable("npm")
            npx = _require_executable("npx")
            _run((npm, "ci", "--prefix", "frontend-react"), cwd=snapshot)
            _run((npm, "ci", "--prefix", "e2e"), cwd=snapshot)
            _run((npx, "playwright", "install", "chromium"), cwd=snapshot / "e2e")
            _run((npm, "run", "--prefix", "frontend-react", "test"), cwd=snapshot)
            _run((npm, "run", "--prefix", "frontend-react", "lint"), cwd=snapshot)
            _run((npm, "run", "--prefix", "frontend-react", "build"), cwd=snapshot)
            _run((npm, "run", "--prefix", "e2e", "test:mobile"), cwd=snapshot)
            _run((npm, "run", "--prefix", "e2e", "test:desktop-ipad"), cwd=snapshot)
            _run((str(python), "scripts/build_frontend_artifact.py"), cwd=snapshot)

        if backend_required:
            api_image_verified = _run_api_image_build(snapshot)

    if api_image_verified:
        print("\nAll applicable local ci.yml parity gates passed for HEAD.")
    else:
        print("\nAll locally runnable ci.yml gates passed for HEAD.")
        print("WARNING: API image build remains unverified until GitHub Actions completes it.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("pre-commit", "pre-push"))
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("CORTEX_CI_BASE_REF", DEFAULT_BASE_REF),
        help="Target branch ref used for pre-push path detection (default: origin/develop).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.stage == "pre-commit":
            run_pre_commit()
        else:
            run_pre_push(args.base_ref)
    except (GateFailure, OSError, StopIteration, urllib.error.URLError) as exc:
        print(f"\nLOCAL CI GATE FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
