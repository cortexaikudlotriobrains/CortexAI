#!/usr/bin/env python3
"""Run CortexAI FastAPI and the React/Vite frontend together."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

ROOT = Path(__file__).resolve().parent
FRONTEND_REACT_DIR = ROOT / "frontend-react"
LOCAL_SUBSCRIPTION_PLANS = ("free", "plus", "pro", "unrestricted")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _resolve_npm(executable: str | None) -> str:
    candidate = executable or ("npm.cmd" if os.name == "nt" else "npm")
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    raise RuntimeError(
        f"Could not find {candidate!r}. Install Node.js 20.x and run "
        "`npm ci --prefix frontend-react` first."
    )


def _check_frontend_ready() -> None:
    package_json = FRONTEND_REACT_DIR / "package.json"
    node_modules = FRONTEND_REACT_DIR / "node_modules"
    if not package_json.exists():
        raise RuntimeError(f"React frontend package not found: {package_json}")
    if not node_modules.exists():
        raise RuntimeError(
            "React frontend dependencies are not installed. "
            "Run `npm ci --prefix frontend-react` first."
        )


def _configure_local_subscription(
    env: dict[str, str],
    plan: str | None,
    *,
    api_host: str,
    frontend_host: str,
) -> None:
    if plan is None:
        return
    if api_host not in LOOPBACK_HOSTS or frontend_host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            "--subscription-plan is local-only and requires loopback API and frontend hosts"
        )

    env["APP_ENV"] = "local"
    env["BILLING_ENABLED"] = "false"
    env["ENABLE_DEV_SESSION_LOGIN"] = "true"
    env["DEV_SUBSCRIPTION_PLAN"] = "" if plan == "free" else plan
    env["DEV_SUBSCRIPTION_BYPASS_ENABLED"] = "true" if plan == "unrestricted" else "false"


def _require_port_available(name: str, host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = host
    if host == "localhost":
        bind_host = "127.0.0.1"
        family = socket.AF_INET

    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((bind_host, port))
    except OSError as exc:
        raise RuntimeError(
            f"{name} port {host}:{port} is unavailable ({exc}). "
            "Stop the process using that port or choose a different port."
        ) from exc
    finally:
        probe.close()


def _start_process(
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> ManagedProcess:
    print(f"[cortexai] starting {name}: {' '.join(command)}")
    process = subprocess.Popen(command, cwd=str(cwd), env=env)
    return ManagedProcess(name=name, process=process)


def _windows_taskkill_command(pid: int, *, force: bool) -> list[str]:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    return command


def _signal_process_tree(managed: ManagedProcess, *, force: bool) -> None:
    if managed.process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            _windows_taskkill_command(managed.process.pid, force=force),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    if force:
        managed.process.kill()
    else:
        managed.process.terminate()


def _terminate(processes: Sequence[ManagedProcess]) -> None:
    for managed in processes:
        if managed.process.poll() is None:
            print(f"[cortexai] stopping {managed.name}...")
            _signal_process_tree(managed, force=False)

    deadline = time.monotonic() + 8
    for managed in processes:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            managed.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            print(f"[cortexai] killing {managed.name}...")
            _signal_process_tree(managed, force=True)
            managed.process.wait(timeout=5)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the CortexAI API and React frontend in one local dev process.",
    )
    parser.add_argument("--api-host", default="127.0.0.1", help="FastAPI host")
    parser.add_argument("--api-port", type=int, default=8000, help="FastAPI port")
    parser.add_argument("--frontend-host", default="127.0.0.1", help="Vite host")
    parser.add_argument("--frontend-port", type=int, default=5173, help="Vite port")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="Disable FastAPI auto-reload",
    )
    parser.add_argument(
        "--enable-dev-login",
        action="store_true",
        help="Enable local dev-session login for the API process",
    )
    parser.add_argument(
        "--subscription-plan",
        choices=LOCAL_SUBSCRIPTION_PLANS,
        default=None,
        help=(
            "Local-only subscription profile. free/plus/pro exercise that plan; "
            "unrestricted keeps subscription enforcement active with very high local allowances."
        ),
    )
    parser.add_argument(
        "--serve-api-frontend",
        action="store_true",
        help="Keep FastAPI static frontend serving enabled instead of forcing SERVE_FRONTEND=false",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run run_server.py",
    )
    parser.add_argument(
        "--npm",
        default=None,
        help="npm executable used to run the React dev server",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    env = os.environ.copy()

    try:
        _configure_local_subscription(
            env,
            args.subscription_plan,
            api_host=args.api_host,
            frontend_host=args.frontend_host,
        )
        npm = _resolve_npm(args.npm)
        _check_frontend_ready()
        _require_port_available("API", args.api_host, args.api_port)
        _require_port_available("React frontend", args.frontend_host, args.frontend_port)
    except RuntimeError as exc:
        print(f"[cortexai] {exc}", file=sys.stderr)
        return 1

    api_host_for_browser = _display_host(args.api_host)
    api_base = f"http://{api_host_for_browser}:{args.api_port}"
    frontend_url = f"http://{_display_host(args.frontend_host)}:{args.frontend_port}/"

    if not args.serve_api_frontend:
        env["SERVE_FRONTEND"] = "false"
    if args.enable_dev_login:
        env["ENABLE_DEV_SESSION_LOGIN"] = "true"
        env.setdefault("APP_ENV", "local")

    env["CORTEX_API_PROXY_TARGET"] = api_base
    env["FRONTEND_RUNTIME_API_BASE"] = api_base

    api_command = [
        args.python,
        "run_server.py",
        "--host",
        args.api_host,
        "--port",
        str(args.api_port),
    ]
    if not args.no_reload:
        api_command.append("--reload")

    frontend_command = [
        npm,
        "run",
        "--prefix",
        "frontend-react",
        "dev",
        "--",
        "--host",
        args.frontend_host,
        "--port",
        str(args.frontend_port),
        "--strictPort",
    ]

    print(f"[cortexai] API: {api_base}")
    print(f"[cortexai] React frontend: {frontend_url}")
    if args.subscription_plan:
        print(f"[cortexai] local subscription profile: {args.subscription_plan}")
    print("[cortexai] press Ctrl+C to stop both processes")

    processes: list[ManagedProcess] = []
    try:
        processes.append(
            _start_process("api", api_command, cwd=ROOT, env=env),
        )
        processes.append(
            _start_process("react", frontend_command, cwd=ROOT, env=env),
        )

        while True:
            for managed in processes:
                exit_code = managed.process.poll()
                if exit_code is not None:
                    print(
                        f"[cortexai] {managed.name} exited with code {exit_code}; "
                        "stopping remaining processes"
                    )
                    _terminate(processes)
                    return int(exit_code)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[cortexai] shutdown requested")
        _terminate(processes)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
