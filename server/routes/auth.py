"""Auth endpoints: in-app email/password + Google sign-in, sessions, dev login."""

import asyncio
import hmac
import os
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from db import get_or_create_cli_user, get_or_create_user_by_cognito
from db.repository import UserEmailConflictError, is_user_active
from db.tables import get_table
from server import cognito_idp
from server.auth_security import (
    OAUTH_TX_COOKIE_NAME,
    OAUTH_TX_TTL_SECONDS,
    client_ip,
    decode_oauth_transaction,
    encode_oauth_transaction,
    enforce_auth_rate_limit,
    error_detail,
    new_oauth_transaction,
    normalize_email,
    pkce_challenge,
    require_same_origin,
    states_match,
    validate_code,
    validate_login_password,
    validate_new_password,
)
from server.cognito import (
    CognitoClaims,
    cognito_enabled,
    exchange_code_for_tokens,
    get_cognito_authorize_url,
    get_cognito_config_for_frontend,
    get_cognito_domain,
    verify_cognito_id_token,
)
from server.dependencies import get_session_user_id
from server.persistence import db_uow
from server.session_auth import (
    SESSION_COOKIE_NAME,
    issue_session,
    revoke_session,
    revoke_user_sessions,
    runtime_environment,
    session_max_age_seconds,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _no_store(response: Response) -> None:
    """Auth responses carry credentials-adjacent data and must never be cached."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(prefix="/v1/auth", tags=["Auth"], dependencies=[Depends(_no_store)])
PRODUCTION_ENV_VALUES = {"prod", "production"}

_ACCOUNT_EXISTS = error_detail(
    "account_exists_other_method",
    "An account with this email already exists. Sign in with the method you used before.",
)
_ACCOUNT_DISABLED = error_detail("account_disabled", "This account is not available.")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _runtime_environment() -> str:
    return runtime_environment()


def _session_max_age() -> int | None:
    return session_max_age_seconds()


def _session_cookie_secure(request: Request) -> bool:
    """Return True if the session cookie should have the Secure flag set.

    In production HTTPS deployments (CloudFront/ALB), the ``Secure`` flag must
    be set so the browser sends the cookie on HTTPS requests.  We detect HTTPS
    from the actual request scheme (which is correct when ProxyHeadersMiddleware
    is active) and fall back to an explicit SESSION_COOKIE_SECURE env override.
    """
    explicit = os.getenv("SESSION_COOKIE_SECURE")
    if explicit is not None:
        return str(explicit).strip().lower() in {"1", "true", "yes", "on"}
    # With ProxyHeadersMiddleware in place request.url.scheme already reflects
    # X-Forwarded-Proto, so this correctly returns True for HTTPS deployments.
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_session_cookie_secure(request),
        samesite="lax",
        path="/",
        max_age=_session_max_age(),
    )


def _dev_login_enabled() -> bool:
    return _env_bool("ENABLE_DEV_SESSION_LOGIN", default=False)


def _ensure_dev_login_allowed(request: Request) -> None:
    if not _dev_login_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    runtime_env = _runtime_environment()
    if runtime_env in PRODUCTION_ENV_VALUES:
        logger.warning(
            "Dev session login blocked in production runtime",
            extra={
                "extra_fields": {
                    "event": "auth.dev_login.blocked.production",
                    "runtime_env": runtime_env,
                    "path": str(request.url.path),
                }
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dev session login is disabled in production environments",
        )

    expected_token = str(os.getenv("DEV_SESSION_LOGIN_TOKEN", "") or "").strip()
    if not expected_token:
        return
    provided = str(request.headers.get("X-Dev-Login-Token") or "")
    if not hmac.compare_digest(provided, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid dev login token",
        )


# --------------------------------------------------------------------------- #
# Public configuration
# --------------------------------------------------------------------------- #


def _password_login_enabled() -> bool:
    return cognito_enabled() and _env_bool("AUTH_PASSWORD_LOGIN_ENABLED", default=True)


def _signup_enabled() -> bool:
    return _password_login_enabled() and _env_bool("AUTH_SIGNUP_ENABLED", default=True)


def _google_login_enabled() -> bool:
    return (
        cognito_enabled()
        and bool(get_cognito_domain())
        and _env_bool("AUTH_GOOGLE_LOGIN_ENABLED", default=True)
    )


def _require_flow(enabled: bool) -> None:
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail("auth_method_disabled", "This sign-in method is not available."),
        )


@router.get("/cognito-config")
async def cognito_config():
    """
    Return public auth configuration for the frontend (no auth required).
    ``passwordLoginEnabled`` / ``signupEnabled`` / ``googleLoginEnabled`` drive
    which controls the in-app login page shows.
    """
    config = get_cognito_config_for_frontend()
    if not config:
        return config
    return {
        **config,
        "passwordLoginEnabled": _password_login_enabled(),
        "signupEnabled": _signup_enabled(),
        "googleLoginEnabled": _google_login_enabled(),
    }


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _flow_http_error(exc: cognito_idp.CognitoFlowError) -> HTTPException:
    headers = {"Retry-After": "60"} if exc.status_code == 429 else None
    return HTTPException(
        status_code=exc.status_code,
        detail=error_detail(exc.code, exc.message),
        headers=headers,
    )


def _redirect_uri_from_request(request: Request) -> str:
    """Callback URL sent to Cognito (must match the app client's allowed callback).

    ``COGNITO_REDIRECT_URI`` always wins and should be set in production to the
    exact registered HTTPS callback. Otherwise it is this API's
    ``/v1/auth/callback`` on the effective request origin; behind CloudFront/ALB
    that origin is only ``https://`` when ``ProxyHeadersMiddleware`` trusts the
    forwarded scheme, so an unset value can cause a redirect mismatch there.
    """
    configured = (os.getenv("COGNITO_REDIRECT_URI") or "").strip()
    if configured:
        return configured.rstrip("/")
    return f"{str(request.base_url).rstrip('/')}/v1/auth/callback"


def _with_query_param(url: str, key: str, value: str) -> str:
    """Return URL with a single query parameter added or replaced."""
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    pairs.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def _frontend_origin() -> str:
    """Origin of a separately hosted frontend (from COGNITO_SUCCESS_REDIRECT_URI), else ''."""
    configured = (os.getenv("COGNITO_SUCCESS_REDIRECT_URI") or "").strip()
    parts = urlsplit(configured)
    if parts.scheme in {"http", "https"} and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def _login_error_redirect(reason: str) -> RedirectResponse:
    redirect = RedirectResponse(
        url=f"{_frontend_origin()}/login?error={quote(reason)}",
        status_code=status.HTTP_302_FOUND,
    )
    redirect.delete_cookie(OAUTH_TX_COOKIE_NAME, path="/")
    return redirect


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _establish_session(
    request: Request, claims: CognitoClaims, *, auth_method: str
) -> tuple[UUID, str]:
    """Upsert the user, refuse disabled/conflicting accounts, and issue a session."""
    with db_uow() as session:
        user_id: UUID = get_or_create_user_by_cognito(
            session,
            sub=claims.sub,
            email=claims.email,
            issuer=claims.iss,
        )
        if not is_user_active(session, user_id):
            raise PermissionError("account_disabled")
        token = issue_session(
            session,
            user_id=user_id,
            auth_method=auth_method,
            user_agent=_user_agent(request),
            client_ip=client_ip(request),
        )
    return user_id, token


# --------------------------------------------------------------------------- #
# Google sign-in (Cognito federated, authorization code + PKCE + state + nonce)
# --------------------------------------------------------------------------- #


@router.get("/google/start")
async def google_start(
    request: Request, next_path: str | None = Query(None, alias="next")
) -> RedirectResponse:
    """Send the browser straight to Google through Cognito, skipping the hosted page."""
    _require_flow(_google_login_enabled())
    enforce_auth_rate_limit(request, action="google_start")

    tx = new_oauth_transaction(next_path or "/")
    authorize_url = get_cognito_authorize_url(
        _redirect_uri_from_request(request),
        tx.state,
        code_challenge=pkce_challenge(tx.code_verifier),
        nonce=tx.nonce,
        identity_provider="Google",
    )
    if not authorize_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail("auth_unavailable", "Sign-in is temporarily unavailable."),
        )
    redirect = RedirectResponse(url=authorize_url, status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        key=OAUTH_TX_COOKIE_NAME,
        value=encode_oauth_transaction(tx),
        httponly=True,
        secure=_session_cookie_secure(request),
        samesite="lax",  # the Cognito redirect back is a top-level GET
        path="/",
        max_age=OAUTH_TX_TTL_SECONDS,
    )
    return redirect


async def handle_oauth_callback(
    request: Request,
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """
    Shared OAuth2 code callback for GET /auth and GET /v1/auth/callback.

    Requires the one-shot transaction cookie set by ``/v1/auth/google/start``:
    ``state`` must match (login CSRF), the PKCE verifier is sent with the code
    exchange, and the ID token ``nonce`` must match (token replay). Every failure
    ends on the in-app login page, never on a Cognito-hosted page.
    """
    if not cognito_enabled():
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    tx = decode_oauth_transaction(request.cookies.get(OAUTH_TX_COOKIE_NAME))
    if error:
        logger.info(
            "OAuth provider returned an error",
            extra={"extra_fields": {"event": "auth.oauth.provider_error", "error": error[:64]}},
        )
        return _login_error_redirect("google_cancelled")
    if not code or not code.strip():
        # Someone opened the callback URL directly.
        return RedirectResponse(
            url=f"{_frontend_origin()}/login", status_code=status.HTTP_302_FOUND
        )
    if tx is None or not states_match(tx.state, state):
        logger.warning(
            "OAuth callback rejected: missing or mismatched state",
            extra={"extra_fields": {"event": "auth.oauth.state_mismatch"}},
        )
        return _login_error_redirect("session_expired")

    redirect_uri = _redirect_uri_from_request(request)
    tokens = await asyncio.to_thread(
        exchange_code_for_tokens, code.strip(), redirect_uri, tx.code_verifier
    )
    if not tokens or not tokens.get("id_token"):
        return _login_error_redirect("google_failed")
    claims = await asyncio.to_thread(verify_cognito_id_token, tokens["id_token"])
    if not claims or not claims.nonce or not states_match(tx.nonce, claims.nonce):
        logger.warning(
            "OAuth callback rejected: ID token failed verification or nonce check",
            extra={"extra_fields": {"event": "auth.oauth.token_rejected"}},
        )
        return _login_error_redirect("google_failed")
    if not claims.email_verified:
        return _login_error_redirect("email_not_verified")

    try:
        user_id, token = await asyncio.to_thread(
            _establish_session, request, claims, auth_method="google"
        )
    except UserEmailConflictError:
        return _login_error_redirect("account_exists")
    except PermissionError:
        return _login_error_redirect("account_disabled")

    base = (os.getenv("COGNITO_SUCCESS_REDIRECT_URI") or "").strip()
    success_url = _with_query_param(base or tx.next_path, "fresh_login", "1")
    redirect = RedirectResponse(url=success_url, status_code=status.HTTP_302_FOUND)
    _set_session_cookie(redirect, request, token)
    redirect.delete_cookie(OAUTH_TX_COOKIE_NAME, path="/")
    logger.info(
        "User logged in via Google",
        extra={"extra_fields": {"event": "auth.login.google", "user_id": str(user_id)}},
    )
    return redirect


@router.get("/callback")
async def auth_callback(
    request: Request,
    response: Response,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """OAuth2 authorization code callback at /v1/auth/callback."""
    return await handle_oauth_callback(request, response, code, state, error)


# --------------------------------------------------------------------------- #
# Email + password
# --------------------------------------------------------------------------- #


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignUpRequest(_StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class ConfirmSignUpRequest(_StrictModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)


class EmailRequest(_StrictModel):
    email: str = Field(max_length=254)


class PasswordLoginRequest(_StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class PasswordResetRequest(_StrictModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=16)
    new_password: str = Field(max_length=256)


@router.post("/signup", status_code=status.HTTP_202_ACCEPTED)
async def signup(body: SignUpRequest, request: Request) -> dict:
    """Create an account and email a verification code. Identical for existing emails."""
    require_same_origin(request)
    _require_flow(_signup_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="signup", identity=email)
    validate_new_password(body.password, email=email)
    try:
        await asyncio.to_thread(cognito_idp.sign_up, email, body.password)
    except cognito_idp.CognitoFlowError as exc:
        raise _flow_http_error(exc) from exc
    return {"status": "verification_required"}


@router.post("/signup/confirm")
async def signup_confirm(body: ConfirmSignUpRequest, request: Request) -> dict:
    """Confirm an email address with the emailed 6-digit code."""
    require_same_origin(request)
    _require_flow(_signup_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="confirm", identity=email)
    code = validate_code(body.code)
    try:
        await asyncio.to_thread(cognito_idp.confirm_sign_up, email, code)
    except cognito_idp.CognitoFlowError as exc:
        raise _flow_http_error(exc) from exc
    return {"status": "verified"}


@router.post("/signup/resend", status_code=status.HTTP_202_ACCEPTED)
async def signup_resend(body: EmailRequest, request: Request) -> dict:
    """Re-send the verification code. Identical for unknown emails."""
    require_same_origin(request)
    _require_flow(_signup_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="resend", identity=email)
    try:
        await asyncio.to_thread(cognito_idp.resend_confirmation_code, email)
    except cognito_idp.CognitoFlowError as exc:
        raise _flow_http_error(exc) from exc
    return {"status": "code_sent"}


@router.post("/login/password")
async def login_with_password(
    body: PasswordLoginRequest, request: Request, response: Response
) -> dict:
    """Authenticate with email + password and set the session cookie."""
    require_same_origin(request)
    _require_flow(_password_login_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="login", identity=email)
    validate_login_password(body.password)

    try:
        result = await asyncio.to_thread(cognito_idp.password_login, email, body.password)
    except cognito_idp.CognitoFlowError as exc:
        logger.info(
            "Password login rejected",
            extra={"extra_fields": {"event": "auth.login.password.rejected", "code": exc.code}},
        )
        raise _flow_http_error(exc) from exc

    claims = await asyncio.to_thread(verify_cognito_id_token, result.id_token)
    # Only the ID token is needed; drop the refresh token so it cannot outlive us.
    await asyncio.to_thread(cognito_idp.revoke_refresh_token, result.refresh_token)
    if not claims or not claims.email_verified or (claims.email or "").lower() != email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail("invalid_credentials", "Incorrect email or password."),
        )

    try:
        user_id, token = await asyncio.to_thread(
            _establish_session, request, claims, auth_method="password"
        )
    except UserEmailConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ACCOUNT_EXISTS) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_ACCOUNT_DISABLED
        ) from exc

    _set_session_cookie(response, request, token)
    logger.info(
        "User logged in with password",
        extra={"extra_fields": {"event": "auth.login.password", "user_id": str(user_id)}},
    )
    return {"user_id": str(user_id), "email": claims.email}


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def password_forgot(body: EmailRequest, request: Request) -> dict:
    """Email a reset code. The response is identical whether or not the account exists."""
    require_same_origin(request)
    _require_flow(_password_login_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="forgot", identity=email)
    try:
        await asyncio.to_thread(cognito_idp.forgot_password, email)
    except cognito_idp.CognitoFlowError as exc:
        raise _flow_http_error(exc) from exc
    return {"status": "code_sent"}


def _revoke_sessions_for_email(email: str, *, reason: str) -> int:
    with db_uow() as session:
        users = get_table("users")
        user_id = session.execute(
            select(users.c.id).where(func.lower(users.c.email) == email)
        ).scalar_one_or_none()
        if user_id is None:
            return 0
        return revoke_user_sessions(session, user_id, reason=reason)


@router.post("/password/reset")
async def password_reset(body: PasswordResetRequest, request: Request) -> dict:
    """Set a new password with the emailed code and sign out every existing session."""
    require_same_origin(request)
    _require_flow(_password_login_enabled())
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, action="reset", identity=email)
    code = validate_code(body.code)
    validate_new_password(body.new_password, email=email)
    try:
        await asyncio.to_thread(cognito_idp.confirm_forgot_password, email, code, body.new_password)
    except cognito_idp.CognitoFlowError as exc:
        raise _flow_http_error(exc) from exc
    revoked = await asyncio.to_thread(_revoke_sessions_for_email, email, reason="password_reset")
    logger.info(
        "Password reset completed",
        extra={"extra_fields": {"event": "auth.password.reset", "sessions_revoked": revoked}},
    )
    return {"status": "password_reset"}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


@router.post("/dev-login")
async def dev_login(request: Request, response: Response) -> dict:
    """
    Local-development helper: create a deterministic dev user and set session cookie.
    Disabled by default and blocked in production-like runtimes.
    """
    _ensure_dev_login_allowed(request)

    with db_uow() as session:
        user_id: UUID = get_or_create_cli_user(session)
        users = get_table("users")
        row = session.execute(
            select(users.c.email, users.c.display_name).where(users.c.id == user_id)
        ).first()
        email = row[0] if row else "cli@cortexai.local"
        display_name = row[1] if row else "CLI User"
        token = issue_session(
            session,
            user_id=user_id,
            auth_method="dev",
            user_agent=_user_agent(request),
            client_ip=client_ip(request),
        )

    _set_session_cookie(response, request, token)
    logger.info(
        "Dev session login completed",
        extra={
            "extra_fields": {
                "event": "auth.dev_login.success",
                "user_id": str(user_id),
                "email": email,
            }
        },
    )
    return {
        "ok": True,
        "user_id": str(user_id),
        "email": email,
        "display_name": display_name,
    }


class LoginRequest(BaseModel):
    id_token: str


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict:
    """
    Verify a Cognito ID token, upsert the user in the DB, and set a session cookie.
    Kept for token-based clients; the login page uses ``/login/password`` and
    ``/google/start``.
    """
    require_same_origin(request)
    enforce_auth_rate_limit(request, action="token_login")
    claims = await asyncio.to_thread(verify_cognito_id_token, body.id_token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired ID token",
        )

    try:
        user_id, token = await asyncio.to_thread(
            _establish_session, request, claims, auth_method="cognito_token"
        )
    except UserEmailConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ACCOUNT_EXISTS) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_ACCOUNT_DISABLED
        ) from exc

    _set_session_cookie(response, request, token)
    logger.info("User logged in via Cognito", extra={"extra_fields": {"user_id": str(user_id)}})

    return {
        "user_id": str(user_id),
        "email": claims.email,
        "email_verified": claims.email_verified,
    }


def _cognito_logout_url(request: Request) -> str | None:
    """Hosted-UI logout URL that also clears Cognito's own cookie (Google users)."""
    domain = get_cognito_domain()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    if not domain or not client_id:
        return None
    if not domain.startswith("http"):
        domain = "https://" + domain
    configured = (os.getenv("COGNITO_LOGOUT_REDIRECT_URI") or "").strip()
    logout_uri = configured or f"{str(request.base_url).rstrip('/')}/login"
    return f"{domain}/logout?client_id={quote(client_id, safe='')}&logout_uri={quote(logout_uri, safe='')}"


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    """Revoke the server-side session and clear the cookie."""
    require_same_origin(request)
    method = await asyncio.to_thread(revoke_session, request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {
        "ok": True,
        # Only federated (Google) sessions have a Cognito cookie worth clearing.
        "cognito_logout_url": _cognito_logout_url(request) if method == "google" else None,
    }


@router.get("/me")
async def me(user_id: UUID = Depends(get_session_user_id)) -> dict:
    """
    Return basic user info for the current session.
    """
    with db_uow(commit_on_success=False) as session:
        users = get_table("users")
        row = session.execute(
            select(users.c.email, users.c.display_name).where(users.c.id == user_id)
        ).first()

    email = row[0] if row else None
    display_name = row[1] if row else None

    return {
        "user_id": str(user_id),
        "email": email,
        "display_name": display_name,
    }
