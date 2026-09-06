#!/usr/bin/env python3
"""Release gate checks run for launch builds."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "db_mode_smoke.py"
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "build",
    "dist",
}


def _run(cmd: list[str], label: str) -> None:
    print(f"[release-gate] {label}")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def _iter_python_files() -> list[str]:
    files: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & EXCLUDED_DIRS:
            continue
        files.append(str(path.relative_to(REPO_ROOT)))
    files.sort()
    return files


def _run_py_compile(files: list[str], chunk_size: int = 100) -> None:
    if not files:
        return

    for index in range(0, len(files), chunk_size):
        chunk = files[index : index + chunk_size]
        _run(
            [sys.executable, "-m", "py_compile", *chunk],
            f"python -m py_compile ({index + 1}-{index + len(chunk)} of {len(files)})",
        )


def main() -> int:
    python_files = _iter_python_files()
    _run_py_compile(python_files)
    with TemporaryDirectory(prefix=".release-gate-", dir=REPO_ROOT) as temporary:
        pytest_basetemp = Path(temporary) / "pytest"
        _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(pytest_basetemp),
            ],
            "pytest -q",
        )
    _run([sys.executable, str(SMOKE_SCRIPT)], "DB mode smoke")
    print("[release-gate] All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
