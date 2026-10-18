"""FastAPI dependencies for authentication and orchestrator access."""

import os
from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from server.cognito import CognitoClaims, cognito_enabled, verify_cognito_id_token
from server.session_auth import parse_session, session_cookie_value
from server.utils import redact_sensitive_headers
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthResult:
    """Result of authentication: session cookie, API key, or Cognito ID token."""

    api_key: str | None
    cognito_claims: CognitoClaims | None
    user_id: UUID | None = None  # Set when authenticated via cortex_session cookie

    @property
    def is_cognito(self) -> bool:
        return self.cognito_claims is not None

    def api_key_or_none(self) -> str | None:
        """Return API key string when auth was via API key; else None."""
        return self.api_key


async def get_auth(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthResult:
    """
    Authenticate via session cookie, Cognito JWT (Authorization: Bearer), or X-API-Key.
    Cookie is tried first so UI sessions work; then Bearer; then API key.
    Returns AuthResult; raises 401 if none is valid.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    redacted = redact_sensitive_headers(dict(request.headers))
    method = str(request.method or "").upper()
    path = str(request.url.path or "")
    has_api_key_header = bool(x_api_key)
    has_authorization_header = bool(authorization)
    session_cookie = session_cookie_value(request)
    has_session_cookie = bool(session_cookie)

    # 1. Try session cookie (cortex_session)
    user_id = parse_session(session_cookie)
    if user_id is not None:
        return AuthResult(
            api_key=None,
            cognito_claims=None,
            user_id=user_id,
        )

    # 2. Try Cognito Bearer token if configured
    if cognito_enabled() and authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
            claims = verify_cognito_id_token(token)
            if claims:
                return AuthResult(api_key=None, cognito_claims=claims, user_id=None)

    # 3. Fall back to API key
    valid_keys_str = os.getenv("API_KEYS", "")
    valid_keys = [k.strip() for k in valid_keys_str.split(",") if k.strip()]

    if x_api_key and valid_keys and x_api_key in valid_keys:
        return AuthResult(api_key=x_api_key, cognito_claims=None, user_id=None)

    # Require at least one auth method to be configured
    if not cognito_enabled() and not valid_keys:
        logger.error(
            "API authentication not configured",
            extra={
                "extra_fields": {
                    "event": "auth.config.missing",
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "has_x_api_key_header": has_api_key_header,
                    "has_authorization_header": has_authorization_header,
                    "has_session_cookie": has_session_cookie,
                    "headers": redacted,
                }
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API authentication not configured",
        )

    logger.warning(
        "Authentication failed",
        extra={
            "extra_fields": {
                "event": "auth.failed",
                "request_id": request_id,
                "method": method,
                "path": path,
                "has_x_api_key_header": has_api_key_header,
                "has_authorization_header": has_authorization_header,
                "has_session_cookie": has_session_cookie,
                "headers": redacted,
            }
        },
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing credentials (send cortex_session cookie, X-API-Key, or Authorization: Bearer)",
    )


async def get_api_key(request: Request, x_api_key: str | None = Header(None)):
    """Validate API key from X-API-Key header (legacy). Prefer get_auth for new code."""
    valid_keys_str = os.getenv("API_KEYS", "")
    request_id = getattr(request.state, "request_id", "unknown")
    redacted_headers = redact_sensitive_headers(dict(request.headers))

    if not valid_keys_str:
        logger.error(
            "API authentication not configured",
            extra={"extra_fields": {"request_id": request_id, "headers": redacted_headers}},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API authentication not configured",
        )

    valid_keys = [k.strip() for k in valid_keys_str.split(",") if k.strip()]

    if not x_api_key or x_api_key not in valid_keys:
        logger.warning(
            "API authentication failed",
            extra={"extra_fields": {"request_id": request_id, "headers": redacted_headers}},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key"
        )

    return x_api_key


async def get_session_user_id(request: Request) -> UUID:
    """
    Resolve user_id from the server-side session cookie.
    """
    user_id = parse_session(session_cookie_value(request))
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user_id


def get_orchestrator():
    """Dependency to get orchestrator instance (singleton pattern)."""
    from orchestrator.core import CortexOrchestrator

    if not hasattr(get_orchestrator, "_instance"):
        get_orchestrator._instance = CortexOrchestrator()
    return get_orchestrator._instance

