"""Amazon Cognito JWT validation and token exchange for server-side auth.

Validates ID tokens issued by Cognito (e.g. from Google IdP). Uses JWKS from
Cognito user pool. Configure via COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID,
COGNITO_REGION, COGNITO_DOMAIN. Optional COGNITO_CLIENT_SECRET for token exchange.
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import jwt
from dataclasses import dataclass
from typing import Any
from utils.logger import get_logger

logger = get_logger(__name__)

# Lazy import to avoid requiring PyJWT when Cognito is not configured
_jwks_client = None


def _cognito_enabled() -> bool:
    pool_id = (os.getenv("COGNITO_USER_POOL_ID") or "").strip()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    region = (os.getenv("COGNITO_REGION") or "").strip()
    return bool(pool_id and client_id and region)


def _jwks_url() -> str:
    region = (os.getenv("COGNITO_REGION") or "").strip()
    pool_id = (os.getenv("COGNITO_USER_POOL_ID") or "").strip()
    return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"


def _get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        url = _jwks_url()
        _jwks_client = jwt.PyJWKClient(url, cache_jwk_set=True, lifespan=3600)
    return _jwks_client


def _ssl_context() -> ssl.SSLContext:
    """Return the default SSL context (verifies certificates).

    In environments where the Cognito JWKS / token endpoints are not reachable
    with the system CA bundle, set ``COGNITO_SSL_VERIFY=false`` to disable
    certificate verification.  This should only be used for local testing or
    environments with a corporate SSL inspection proxy.
    """
    raw = str(os.getenv("COGNITO_SSL_VERIFY", "true") or "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        logger.warning(
            "COGNITO_SSL_VERIFY=false: TLS certificate verification disabled for Cognito endpoints",
            extra={"extra_fields": {"event": "cognito.ssl_verify.disabled"}},
        )
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def get_cognito_public_keys() -> dict:
    """Fetch public keys from Cognito JWKS endpoint."""
    url = _jwks_url()
    ctx = _ssl_context()
    with urllib.request.urlopen(url, context=ctx, timeout=15) as response:
        return json.loads(response.read().decode())


@dataclass(frozen=True)
class CognitoClaims:
    """Verified claims from Cognito ID token."""

    sub: str
    email: str | None
    email_verified: bool
    iss: str
    aud: str
    token_use: str
    nonce: str | None = None

    @property
    def display_email(self) -> str:
        return self.email or self.sub


def verify_cognito_id_token(token: str) -> CognitoClaims | None:
    """
    Verify a Cognito ID token (e.g. from Google sign-in via Cognito).

    Returns CognitoClaims if valid, None otherwise.
    """
    if not _cognito_enabled():
        return None

    pool_id = (os.getenv("COGNITO_USER_POOL_ID") or "").strip()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    region = (os.getenv("COGNITO_REGION") or "").strip()
    expected_issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"

    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header["kid"]

        jwks = get_cognito_public_keys()
        signing_key = None
        for jwk in jwks["keys"]:
            if jwk["kid"] == kid:
                signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
                break

        if signing_key is None:
            logger.warning(
                "Cognito token verification failed: no matching key id in JWKS",
                extra={
                    "extra_fields": {"event": "auth.cognito.token_invalid", "reason": "unknown_kid"}
                },
            )
            return None

        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=expected_issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except Exception as e:
        # PyJWT messages ("Invalid audience", "Signature has expired") carry no token data.
        logger.warning(
            "Cognito token verification failed",
            extra={
                "extra_fields": {
                    "event": "auth.cognito.token_invalid",
                    "error_type": type(e).__name__,
                    "reason": str(e)[:200],
                }
            },
        )
        return None

    token_use = payload.get("token_use")
    if token_use != "id":
        logger.warning(
            "Cognito token is not an ID token",
            extra={"extra_fields": {"event": "auth.cognito.token_invalid", "reason": "token_use"}},
        )
        return None

    return CognitoClaims(
        sub=str(payload.get("sub", "")),
        email=payload.get("email"),
        email_verified=bool(payload.get("email_verified", False)),
        iss=str(payload.get("iss", "")),
        aud=str(payload.get("aud", "")),
        token_use=str(token_use),
        nonce=payload.get("nonce"),
    )


def cognito_enabled() -> bool:
    """Return True if Cognito auth is configured."""
    return _cognito_enabled()


def get_cognito_config_for_frontend() -> dict[str, Any]:
    """
    Return public Cognito config for frontend (no secrets).
    Used by /v1/auth/cognito-config; the in-app login page reads the enabled-method flags.
    """
    if not _cognito_enabled():
        return {}
    region = (os.getenv("COGNITO_REGION") or "").strip()
    pool_id = (os.getenv("COGNITO_USER_POOL_ID") or "").strip()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    domain = (os.getenv("COGNITO_DOMAIN") or "").strip()
    redirect_uri = (os.getenv("COGNITO_REDIRECT_URI") or "").strip()
    domain_normalized = domain.rstrip("/") if domain else ""
    if not domain_normalized.startswith("http"):
        domain_normalized = "https://" + domain_normalized if domain_normalized else ""
    logout_path = (
        f"{domain_normalized}/logout?client_id={urllib.parse.quote(client_id)}"
        if (domain_normalized and client_id)
        else ""
    )
    return {
        "enabled": True,
        "region": region,
        "userPoolId": pool_id,
        "clientId": client_id,
        "domain": domain,
        "redirectUri": redirect_uri,
        "logoutUrl": logout_path,
    }


def get_cognito_domain() -> str:
    """Return Cognito domain base URL (no trailing slash), or empty if not configured."""
    domain = (os.getenv("COGNITO_DOMAIN") or "").strip().rstrip("/")
    return domain


def get_cognito_authorize_url(
    redirect_uri: str,
    state: str | None = None,
    *,
    code_challenge: str | None = None,
    nonce: str | None = None,
    identity_provider: str | None = None,
) -> str:
    """
    Build the Cognito ``/oauth2/authorize`` URL (authorization code flow).

    ``identity_provider`` (e.g. ``Google``) sends the browser straight to that
    provider so the Cognito-hosted login page is never shown. ``code_challenge``
    enables PKCE (S256); ``state`` and ``nonce`` bind the response to the browser
    that started the flow. redirect_uri must match the Cognito app client.
    """
    domain = get_cognito_domain()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    if not domain or not client_id:
        return ""
    params = [
        ("client_id", client_id),
        ("response_type", "code"),
        ("scope", "openid email profile"),
        ("redirect_uri", redirect_uri),
    ]
    if identity_provider:
        params.append(("identity_provider", identity_provider))
    if state:
        params.append(("state", state))
    if nonce:
        params.append(("nonce", nonce))
    if code_challenge:
        params.append(("code_challenge", code_challenge))
        params.append(("code_challenge_method", "S256"))
    qs = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params)
    return f"{domain}/oauth2/authorize?{qs}"


def exchange_code_for_tokens(
    code: str, redirect_uri: str, code_verifier: str | None = None
) -> dict[str, Any] | None:
    """
    Exchange authorization code for tokens via Cognito /oauth2/token.
    Returns token response dict (id_token, access_token, etc.) or None on failure.
    """
    domain = get_cognito_domain()
    client_id = (os.getenv("COGNITO_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("COGNITO_CLIENT_SECRET") or "").strip()
    if not domain or not client_id:
        return None
    ctx = _ssl_context()
    url = f"{domain}/oauth2/token"
    body_parts = [
        "grant_type=authorization_code",
        f"code={urllib.parse.quote(code)}",
        f"client_id={urllib.parse.quote(client_id)}",
        f"redirect_uri={urllib.parse.quote(redirect_uri)}",
    ]
    if code_verifier:
        body_parts.append(f"code_verifier={urllib.parse.quote(code_verifier, safe='')}")
    if client_secret:
        body_parts.append(f"client_secret={urllib.parse.quote(client_secret)}")
    body = "&".join(body_parts).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Cognito answers {"error": "invalid_grant" | "invalid_client" | ...}; no tokens.
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = ""
        logger.warning(
            "Cognito token exchange rejected",
            extra={
                "extra_fields": {
                    "event": "auth.oauth.token_exchange_failed",
                    "status": e.code,
                    "detail": detail,
                }
            },
        )
        return None
    except Exception as e:
        logger.warning(
            "Cognito token exchange failed",
            extra={
                "extra_fields": {
                    "event": "auth.oauth.token_exchange_failed",
                    "error_type": type(e).__name__,
                }
            },
        )
        return None
