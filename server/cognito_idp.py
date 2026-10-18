"""Server-side Amazon Cognito user-pool calls for the in-app login page.

These are the public (unsigned) Cognito user-pool APIs, so the API process needs
no AWS credentials for them. The app client must allow ``USER_PASSWORD_AUTH``
(see ``docs/runbooks/auth-login.md``). Passwords and one-time codes are passed
straight through and are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from utils.logger import get_logger

logger = get_logger(__name__)


class CognitoFlowError(Exception):
    """A user-pool call failed in a way the API maps to a stable public code."""

    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message


_INVALID_CREDENTIALS = (
    "invalid_credentials",
    401,
    "Incorrect email or password.",
)
_INVALID_CODE = ("invalid_code", 400, "That code is not correct.")

# Cognito error code -> (public code, HTTP status, safe message)
_COMMON_ERRORS: dict[str, tuple[str, int, str]] = {
    "NotAuthorizedException": _INVALID_CREDENTIALS,
    "UserNotFoundException": _INVALID_CREDENTIALS,
    "UserNotConfirmedException": (
        "email_not_verified",
        403,
        "Verify your email address to continue.",
    ),
    "PasswordResetRequiredException": (
        "password_reset_required",
        403,
        "Reset your password to continue.",
    ),
    "InvalidPasswordException": (
        "weak_password",
        400,
        "Choose a stronger password.",
    ),
    "CodeMismatchException": _INVALID_CODE,
    "ExpiredCodeException": ("expired_code", 400, "That code has expired. Request a new one."),
    "LimitExceededException": (
        "rate_limited",
        429,
        "Too many attempts. Please wait a few minutes and try again.",
    ),
    "TooManyRequestsException": (
        "rate_limited",
        429,
        "Too many attempts. Please wait a few minutes and try again.",
    ),
    "TooManyFailedAttemptsException": (
        "rate_limited",
        429,
        "Too many attempts. Please wait a few minutes and try again.",
    ),
}


@dataclass(frozen=True)
class PasswordLoginResult:
    id_token: str
    refresh_token: str | None


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def password_auth_configured() -> bool:
    return bool(
        _env("COGNITO_USER_POOL_ID") and _env("COGNITO_CLIENT_ID") and _env("COGNITO_REGION")
    )


@lru_cache(maxsize=4)
def _client_for_region(region: str):
    return boto3.client(
        "cognito-idp",
        region_name=region,
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _client():
    return _client_for_region(_env("COGNITO_REGION"))


def _secret_hash(username: str) -> str | None:
    client_secret = _env("COGNITO_CLIENT_SECRET")
    if not client_secret:
        return None
    digest = hmac.new(
        client_secret.encode("utf-8"),
        (username + _env("COGNITO_CLIENT_ID")).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _with_secret_hash(params: dict[str, Any], username: str) -> dict[str, Any]:
    secret_hash = _secret_hash(username)
    if secret_hash:
        params["SecretHash"] = secret_hash
    return params


def _map_error(exc: Exception, *, operation: str) -> CognitoFlowError:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "") or "")
        if code == "NotAuthorizedException":
            refined = _refine_not_authorized(exc, operation=operation)
            if refined is not None:
                return refined
        mapped = _COMMON_ERRORS.get(code)
        if mapped:
            return CognitoFlowError(*mapped)
        # Only the AWS error code is logged: messages can echo user input.
        logger.warning(
            "Cognito user-pool call failed",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.error",
                    "operation": operation,
                    "aws_error_code": code or "unknown",
                }
            },
        )
    else:
        logger.warning(
            "Cognito user-pool call could not complete",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.unavailable",
                    "operation": operation,
                    "error_type": type(exc).__name__,
                }
            },
        )
    return CognitoFlowError(
        "auth_unavailable",
        502,
        "Sign-in is temporarily unavailable. Please try again shortly.",
    )


def _refine_not_authorized(exc: ClientError, *, operation: str) -> CognitoFlowError | None:
    """Split NotAuthorizedException cases that are not a wrong password.

    Cognito uses this one code for wrong credentials, a missing/incorrect app-client
    secret, a locked-out user and a disabled user. Only fixed keywords of Cognito's
    own message are inspected; the message itself is never logged or returned.
    """
    message = str(exc.response.get("Error", {}).get("Message", "") or "").lower()
    if "secret" in message:
        logger.error(
            "Cognito app client secret is missing or wrong (check COGNITO_CLIENT_SECRET)",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.misconfigured",
                    "operation": operation,
                    "reason": "client_secret",
                }
            },
        )
        return CognitoFlowError(
            "auth_unavailable",
            502,
            "Sign-in is temporarily unavailable. Please try again shortly.",
        )
    if "attempts exceeded" in message:
        return CognitoFlowError(*_COMMON_ERRORS["TooManyFailedAttemptsException"])
    if "disabled" in message:
        return CognitoFlowError("account_disabled", 403, "This account is not available.")
    return None


def _aws_error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "") or "")
    return ""


def sign_up(email: str, password: str) -> None:
    """Create an unconfirmed user. An existing user is treated as success."""
    params = _with_secret_hash(
        {
            "ClientId": _env("COGNITO_CLIENT_ID"),
            "Username": email,
            "Password": password,
            "UserAttributes": [{"Name": "email", "Value": email}],
        },
        email,
    )
    try:
        _client().sign_up(**params)
    except (ClientError, BotoCoreError) as exc:
        # Answering identically for existing accounts avoids account enumeration.
        if _aws_error_code(exc) == "UsernameExistsException":
            return
        raise _map_error(exc, operation="sign_up") from exc


def confirm_sign_up(email: str, code: str) -> None:
    params = _with_secret_hash(
        {
            "ClientId": _env("COGNITO_CLIENT_ID"),
            "Username": email,
            "ConfirmationCode": code,
        },
        email,
    )
    try:
        _client().confirm_sign_up(**params)
    except (ClientError, BotoCoreError) as exc:
        # Already-confirmed or unknown users must look like a bad code.
        if _aws_error_code(exc) in {"UserNotFoundException", "NotAuthorizedException"}:
            raise CognitoFlowError(*_INVALID_CODE) from exc
        raise _map_error(exc, operation="confirm_sign_up") from exc


def resend_confirmation_code(email: str) -> None:
    params = _with_secret_hash(
        {"ClientId": _env("COGNITO_CLIENT_ID"), "Username": email},
        email,
    )
    try:
        _client().resend_confirmation_code(**params)
    except (ClientError, BotoCoreError) as exc:
        # Unknown or already-confirmed users are indistinguishable to the caller.
        if _aws_error_code(exc) in {
            "UserNotFoundException",
            "InvalidParameterException",
            "NotAuthorizedException",
        }:
            return
        raise _map_error(exc, operation="resend_confirmation_code") from exc


def password_login(email: str, password: str) -> PasswordLoginResult:
    """Authenticate with USER_PASSWORD_AUTH and return the ID token."""
    params: dict[str, Any] = {
        "ClientId": _env("COGNITO_CLIENT_ID"),
        "AuthFlow": "USER_PASSWORD_AUTH",
        "AuthParameters": {"USERNAME": email, "PASSWORD": password},
    }
    secret_hash = _secret_hash(email)
    if secret_hash:
        params["AuthParameters"]["SECRET_HASH"] = secret_hash
    try:
        response = _client().initiate_auth(**params)
    except (ClientError, BotoCoreError) as exc:
        raise _map_error(exc, operation="initiate_auth") from exc

    result = response.get("AuthenticationResult") or {}
    id_token = result.get("IdToken")
    if not id_token:
        # Challenges (forced password change, MFA) are not offered by this page.
        logger.warning(
            "Cognito returned an unsupported sign-in challenge",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.unsupported_challenge",
                    "challenge": str(response.get("ChallengeName") or "none"),
                }
            },
        )
        raise CognitoFlowError(
            "challenge_unsupported",
            403,
            "This account needs extra steps. Reset your password to continue.",
        )
    return PasswordLoginResult(id_token=id_token, refresh_token=result.get("RefreshToken"))


def revoke_refresh_token(refresh_token: str | None) -> None:
    """Best-effort: the API only needs the ID token once, so drop the refresh token."""
    if not refresh_token:
        return
    params: dict[str, Any] = {"ClientId": _env("COGNITO_CLIENT_ID"), "Token": refresh_token}
    client_secret = _env("COGNITO_CLIENT_SECRET")
    if client_secret:
        params["ClientSecret"] = client_secret
    try:
        _client().revoke_token(**params)
    except Exception as exc:  # best effort, never block sign-in
        logger.warning(
            "Cognito refresh token revocation failed",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.revoke_failed",
                    "aws_error_code": _aws_error_code(exc) or type(exc).__name__,
                }
            },
        )


def forgot_password(email: str) -> None:
    """Start a reset. Unknown or unverified users are indistinguishable to the caller."""
    params = _with_secret_hash(
        {"ClientId": _env("COGNITO_CLIENT_ID"), "Username": email},
        email,
    )
    try:
        _client().forgot_password(**params)
    except (ClientError, BotoCoreError) as exc:
        if _aws_error_code(exc) in {
            "UserNotFoundException",
            "InvalidParameterException",
            "NotAuthorizedException",
            "UserNotConfirmedException",
        }:
            return
        raise _map_error(exc, operation="forgot_password") from exc


def confirm_forgot_password(email: str, code: str, new_password: str) -> None:
    params = _with_secret_hash(
        {
            "ClientId": _env("COGNITO_CLIENT_ID"),
            "Username": email,
            "ConfirmationCode": code,
            "Password": new_password,
        },
        email,
    )
    try:
        _client().confirm_forgot_password(**params)
    except (ClientError, BotoCoreError) as exc:
        if _aws_error_code(exc) in {"UserNotFoundException", "NotAuthorizedException"}:
            raise CognitoFlowError(*_INVALID_CODE) from exc
        raise _map_error(exc, operation="confirm_forgot_password") from exc
