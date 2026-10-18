"""Periodic cleanup of ended browser sessions and old auth rate-limit windows."""

from __future__ import annotations

import os
from datetime import timedelta

from server.auth_security import purge_rate_limit_windows
from server.persistence import db_uow
from server.session_auth import purge_ended_sessions

DEFAULT_INTERVAL_SECONDS = 3600
_SESSION_RETENTION = timedelta(days=30)
_RATE_LIMIT_RETENTION = timedelta(days=1)


def maintenance_interval_seconds() -> int:
    """``AUTH_MAINTENANCE_INTERVAL_SECONDS``; 0 disables the worker."""
    try:
        return max(0, int(os.getenv("AUTH_MAINTENANCE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def run_auth_maintenance_cycle() -> dict[str, int]:
    """Delete sessions ended over 30 days ago and rate-limit windows older than a day."""
    with db_uow() as session:
        sessions = purge_ended_sessions(session, retention=_SESSION_RETENTION)
        windows = purge_rate_limit_windows(session, older_than=_RATE_LIMIT_RETENTION)
    return {"sessions_deleted": sessions, "rate_limit_windows_deleted": windows}
