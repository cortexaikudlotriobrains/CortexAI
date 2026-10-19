from __future__ import annotations

import socket
import sys

import pytest

import run_app


def test_runner_defaults_match_documented_local_urls(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_app.py"])

    args = run_app._parse_args()

    assert args.api_host == "127.0.0.1"
    assert args.api_port == 8000
    assert args.frontend_host == "127.0.0.1"
    assert args.frontend_port == 5174
    assert args.subscription_plan is None


def test_require_port_available_accepts_unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    run_app._require_port_available("Test", "127.0.0.1", port)


def test_require_port_available_rejects_listening_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        with pytest.raises(RuntimeError, match=rf"Test port 127\.0\.0\.1:{port} is unavailable"):
            run_app._require_port_available("Test", "127.0.0.1", port)


def test_windows_taskkill_command_targets_descendants():
    assert run_app._windows_taskkill_command(1234, force=False) == [
        "taskkill",
        "/PID",
        "1234",
        "/T",
    ]
    assert run_app._windows_taskkill_command(1234, force=True) == [
        "taskkill",
        "/PID",
        "1234",
        "/T",
        "/F",
    ]


@pytest.mark.parametrize("plan", run_app.LOCAL_SUBSCRIPTION_PLANS)
def test_configure_local_subscription_sets_safe_runner_environment(plan):
    env = {
        "APP_ENV": "production",
        "BILLING_ENABLED": "true",
        "DEV_SUBSCRIPTION_PLAN": "pro",
    }

    run_app._configure_local_subscription(
        env,
        plan,
        api_host="127.0.0.1",
        frontend_host="localhost",
    )

    assert env["APP_ENV"] == "local"
    assert env["BILLING_ENABLED"] == "false"
    assert env["ENABLE_DEV_SESSION_LOGIN"] == "true"
    assert env["DEV_SUBSCRIPTION_PLAN"] == ("" if plan == "free" else plan)
    assert env["DEV_SUBSCRIPTION_BYPASS_ENABLED"] == ("true" if plan == "unrestricted" else "false")


@pytest.mark.parametrize(
    ("api_host", "frontend_host"),
    [("0.0.0.0", "127.0.0.1"), ("127.0.0.1", "0.0.0.0")],
)
def test_configure_local_subscription_rejects_non_loopback_hosts(
    api_host,
    frontend_host,
):
    with pytest.raises(RuntimeError, match="local-only"):
        run_app._configure_local_subscription(
            {},
            "unrestricted",
            api_host=api_host,
            frontend_host=frontend_host,
        )


def test_configure_local_subscription_leaves_normal_environment_unchanged():
    env = {"BILLING_ENABLED": "true"}

    run_app._configure_local_subscription(
        env,
        None,
        api_host="0.0.0.0",
        frontend_host="0.0.0.0",
    )

    assert env == {"BILLING_ENABLED": "true"}
