"""Security controls for remote MCP and OAuth configuration."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import unicodedata
from urllib.parse import urlsplit

_SECRET_KEYS = re.compile(r"token|secret|password|authorization|cookie|api[_-]?key", re.I)


def normalize_work_title(value: object, *, max_length: int | None = None) -> str | None:
    """Return a provider-safe, single-line Work session title."""

    sanitized = "".join(
        " " if character.isspace() or unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in str(value or "")
    )
    normalized = " ".join(sanitized.split())
    if max_length is not None:
        normalized = normalized[:max_length].rstrip()
    return normalized or None


def validate_remote_https_url(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Remote MCP URL must use HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Remote MCP URL cannot include credentials or a fragment")
    if parsed.port not in (None, 443):
        raise ValueError("Remote MCP URL must use the standard HTTPS port")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Remote MCP URL cannot target a local host")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Remote MCP host could not be resolved") from exc
    if not addresses:
        raise ValueError("Remote MCP host resolved to no addresses")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if not address.is_global:
            raise ValueError("Remote MCP URL cannot target a private or reserved address")
    return normalized


def safe_return_path(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("/") or normalized.startswith("//") or "\\" in normalized:
        raise ValueError("OAuth return path must be a same-origin absolute path")
    return normalized[:500]


def redact_mapping(value: object, *, depth: int = 0) -> object:
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)[:100]
            result[key] = (
                "[redacted]" if _SECRET_KEYS.search(key) else redact_mapping(item, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        return [redact_mapping(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def schema_hash(schema: object) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_action(tool_name: str, description: str = "") -> str:
    text = f"{tool_name} {description}".lower()
    if any(word in text for word in ("deploy", "release", "rollback", "infrastructure")):
        return "DEPLOYMENT"
    if any(word in text for word in ("pay", "purchase", "transfer", "trade", "invoice", "refund")):
        return "FINANCIAL"
    if any(
        word in text for word in ("send", "email", "message", "publish", "post_comment", "invite")
    ):
        return "EXTERNAL_COMMUNICATION"
    if any(
        word in text for word in ("delete", "remove", "drop", "destroy", "merge", "force", "revoke")
    ):
        return "DESTRUCTIVE"
    if any(phrase in text for phrase in ("open_pull_request", "open pull request")):
        return "WRITE"
    if any(word in text for word in ("create", "update", "write", "edit", "add", "set", "upload")):
        return "WRITE"
    if any(
        word in text for word in ("get", "list", "read", "search", "find", "fetch", "query", "view")
    ):
        return "READ"
    return "DESTRUCTIVE"
