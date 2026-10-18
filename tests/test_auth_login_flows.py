"""In-app login: server-side sessions, email/password, Google (state + PKCE), hardening."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from server import auth_security, cognito_idp
from server import session_auth as sa
from server.app import create_app
from server.cognito import CognitoClaims
from tests.auth_db_helpers import bootstrap_auth_sqlite_schema, reset_db_runtime_state

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/pool"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    monkeypatch.setenv("DB_SCHEMA", "main")
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "false")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-secret-unit-test-secret-1234")
    for name in ("APP_ENV", "ENVIRONMENT", "ENV", "COGNITO_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "client-id")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setenv("COGNITO_DOMAIN", "https://auth.example.com")
    reset_db_runtime_state(monkeypatch)
    bootstrap_auth_sqlite_schema()
    auth_security.reset_auth_rate_limits()
    yield create_app()
    reset_db_runtime_state(monkeypatch)


@pytest.fixture()
def client(app):
    return TestClient(app)


def _claims(email="ada@example.com", *, sub="sub-1", verified=True, nonce=None) -> CognitoClaims:
    return CognitoClaims(
        sub=sub,
        email=email,
        email_verified=verified,
        iss=ISSUER,
        aud="client-id",
        token_use="id",
        nonce=nonce,
    )


def _create_user(email="ada@example.com", *, active=True, subject="sub-1") -> UUID:
    from db.tables import get_table
    from server.persistence import db_uow

    with db_uow() as session:
        users = get_table("users")
        return session.execute(
            users.insert()
            .values(
                email=email,
                display_name="Ada",
                is_active=active,
                auth_provider="cognito",
                auth_subject=subject,
                auth_issuer=ISSUER,
            )
            .returning(users.c.id)
        ).scalar_one()


def _issue(user_id: UUID, method: str = "password") -> str:
    from server.persistence import db_uow

    with db_uow() as session:
        return sa.issue_session(session, user_id=user_id, auth_method=method)


# --------------------------------------------------------------------------- #
# Server-side sessions
# --------------------------------------------------------------------------- #


def test_session_token_is_opaque_and_only_its_hash_is_stored(app):
    from db.tables import get_table
    from server.persistence import db_uow

    user_id = _create_user()
    token = _issue(user_id)

    assert str(user_id) not in token
    assert sa.parse_session(token) == user_id
    with db_uow(commit_on_success=False) as session:
        stored = session.execute(select(get_table("auth_sessions").c.token_hash)).scalar_one()
    assert stored == sa.hash_value(token)
    assert stored != token


def test_session_rejects_garbage_forged_expired_revoked_and_disabled(app):
    from db.tables import get_table
    from server.persistence import db_uow

    user_id = _create_user()
    token = _issue(user_id)
    assert sa.parse_session(None) is None
    assert sa.parse_session("short") is None
    assert sa.parse_session("a" * 43) is None  # well-formed but unknown
    # The legacy cookie format (base64 of "<uuid>.<sig>") is no longer accepted.
    assert sa.parse_session("MTExMTExMTEtMTExMS0xMTExLTExMTEtMTExMTExMTExMTEx.AAAA") is None

    sessions = get_table("auth_sessions")
    with db_uow() as session:
        session.execute(
            update(sessions).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    assert sa.parse_session(token) is None

    fresh = _issue(user_id)
    assert sa.parse_session(fresh) == user_id
    assert sa.revoke_session(fresh) == "password"
    assert sa.parse_session(fresh) is None
    assert sa.revoke_session(fresh) is None  # already revoked

    other = _issue(user_id)
    users = get_table("users")
    with db_uow() as session:
        session.execute(update(users).where(users.c.id == user_id).values(is_active=False))
    assert sa.parse_session(other) is None


def test_revoke_user_sessions_revokes_every_live_session(app):
    from server.persistence import db_uow

    user_id = _create_user()
    first, second = _issue(user_id), _issue(user_id, "google")
    with db_uow() as session:
        assert sa.revoke_user_sessions(session, user_id, reason="password_reset") == 2
    assert sa.parse_session(first) is None
    assert sa.parse_session(second) is None


def test_production_requires_a_strong_session_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        sa.validate_session_secret_config()
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret")
    with pytest.raises(RuntimeError):
        sa.validate_session_secret_config()
    monkeypatch.setenv("SESSION_SECRET", "too-short")
    with pytest.raises(RuntimeError):
        sa.validate_session_secret_config()
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    sa.validate_session_secret_config()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.delenv("SESSION_SECRET")
    sa.validate_session_secret_config()  # dev default tolerated outside production


# --------------------------------------------------------------------------- #
# Security helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://evil.example/x",
        "//evil.example/x",
        "/\\evil.example",
        "javascript:alert(1)",
        "relative/path",
        "/ok\r\nSet-Cookie: x=1",
        "/login",
        "/login?next=/",
        "/auth?code=1",
        "/v1/auth/me",
        "/" + "a" * 600,
    ],
)
def test_next_path_rejects_open_redirects_and_loops(value):
    assert auth_security.sanitize_next_path(value) == "/"


def test_next_path_keeps_safe_in_app_targets():
    assert auth_security.sanitize_next_path("/work/abc?x=1") == "/work/abc?x=1"
    assert auth_security.sanitize_next_path("/account/billing") == "/account/billing"


def test_oauth_transaction_round_trip_tamper_and_expiry(monkeypatch):
    tx = auth_security.new_oauth_transaction("/usage")
    cookie = auth_security.encode_oauth_transaction(tx)
    assert auth_security.decode_oauth_transaction(cookie) == tx

    encoded, signature = cookie.rsplit(".", 1)
    assert auth_security.decode_oauth_transaction(f"{encoded}.{'0' * len(signature)}") is None
    assert auth_security.decode_oauth_transaction(f"{encoded}x.{signature}") is None
    assert auth_security.decode_oauth_transaction("garbage") is None

    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 10**10)
    assert auth_security.decode_oauth_transaction(cookie) is None


def test_pkce_challenge_matches_rfc7636_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert auth_security.pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


@pytest.mark.parametrize(
    "password,expected",
    [
        ("short1!", "weak_password"),
        ("a" * 129, "weak_password"),
        ("aaaaaaaaaaaaaaaa", "weak_password"),
        ("password1234", "weak_password"),
        ("my-ada@example.com-pw", "weak_password"),
    ],
)
def test_new_password_policy_rejections(password, expected):
    with pytest.raises(HTTPException) as exc:
        auth_security.validate_new_password(password, email="ada@example.com")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == expected


def test_new_password_policy_rejects_the_email_local_part():
    with pytest.raises(HTTPException) as exc:
        auth_security.validate_new_password("xx-adalovelace-rocks", email="adalovelace@example.com")
    assert exc.value.detail["code"] == "weak_password"


def test_new_password_policy_accepts_long_passphrase():
    auth_security.validate_new_password("correct horse battery staple", email="ada@example.com")


# --------------------------------------------------------------------------- #
# Config + gating
# --------------------------------------------------------------------------- #


def test_config_advertises_enabled_methods(client, monkeypatch):
    body = client.get("/v1/auth/cognito-config").json()
    assert body["passwordLoginEnabled"] is True
    assert body["signupEnabled"] is True
    assert body["googleLoginEnabled"] is True
    assert client.get("/v1/auth/cognito-config").headers["cache-control"] == "no-store"

    monkeypatch.setenv("AUTH_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("AUTH_GOOGLE_LOGIN_ENABLED", "false")
    body = client.get("/v1/auth/cognito-config").json()
    assert body["signupEnabled"] is False
    assert body["googleLoginEnabled"] is False
    assert (
        client.post("/v1/auth/signup", json={"email": "a@b.co", "password": "x" * 14}).status_code
        == 404
    )


def test_disabled_cognito_returns_empty_config_and_404_flows(client, monkeypatch):
    monkeypatch.delenv("COGNITO_USER_POOL_ID")
    assert client.get("/v1/auth/cognito-config").json() == {}
    r = client.post("/v1/auth/login/password", json={"email": "a@b.co", "password": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "auth_method_disabled"


# --------------------------------------------------------------------------- #
# Email + password
# --------------------------------------------------------------------------- #


def _stub_login(monkeypatch, *, email="ada@example.com", verified=True, refresh="refresh-token"):
    revoked: list[str | None] = []
    monkeypatch.setattr(
        cognito_idp,
        "password_login",
        lambda e, p: cognito_idp.PasswordLoginResult(id_token="id-token", refresh_token=refresh),
    )
    monkeypatch.setattr(cognito_idp, "revoke_refresh_token", revoked.append)
    import server.routes.auth as auth_route

    monkeypatch.setattr(
        auth_route, "verify_cognito_id_token", lambda t: _claims(email, verified=verified)
    )
    return revoked


def test_password_login_sets_hardened_cookie_and_session_resolves(client, monkeypatch):
    revoked = _stub_login(monkeypatch)
    r = client.post(
        "/v1/auth/login/password",
        json={"email": " Ada@Example.com ", "password": "correct horse battery"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    assert revoked == ["refresh-token"]
    cookie = r.headers["set-cookie"].lower()
    assert "cortex_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie

    me = client.get("/v1/auth/me")  # real parse_session, no patching
    assert me.status_code == 200
    assert me.json()["email"] == "ada@example.com"


def test_password_login_failures_are_generic_and_do_not_set_cookies(client, monkeypatch):
    def reject(email, password):
        raise cognito_idp.CognitoFlowError(
            "invalid_credentials", 401, "Incorrect email or password."
        )

    monkeypatch.setattr(cognito_idp, "password_login", reject)
    r = client.post(
        "/v1/auth/login/password", json={"email": "ada@example.com", "password": "x" * 14}
    )
    assert r.status_code == 401
    assert r.json()["detail"] == {
        "code": "invalid_credentials",
        "message": "Incorrect email or password.",
    }
    assert "set-cookie" not in r.headers
    assert client.get("/v1/auth/me").status_code == 401


def test_password_login_requires_verified_email_claim(client, monkeypatch):
    _stub_login(monkeypatch, verified=False)
    r = client.post(
        "/v1/auth/login/password", json={"email": "ada@example.com", "password": "x" * 14}
    )
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


def test_password_login_refuses_email_owned_by_another_sign_in_method(client, monkeypatch):
    _create_user("ada@example.com", subject="google-sub")
    _stub_login(monkeypatch)
    r = client.post(
        "/v1/auth/login/password", json={"email": "ada@example.com", "password": "x" * 14}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "account_exists_other_method"
    assert "set-cookie" not in r.headers


def test_password_login_refuses_disabled_accounts(client, monkeypatch):
    _create_user("ada@example.com", active=False, subject="sub-1")
    _stub_login(monkeypatch)
    r = client.post(
        "/v1/auth/login/password", json={"email": "ada@example.com", "password": "x" * 14}
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "account_disabled"
    assert "set-cookie" not in r.headers


def test_login_is_rate_limited_per_identity(client, monkeypatch):
    monkeypatch.setenv("AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN", "3")
    monkeypatch.setenv("AUTH_RATE_LIMIT_IP_PER_MINUTE", "0")

    def reject(email, password):
        raise cognito_idp.CognitoFlowError(
            "invalid_credentials", 401, "Incorrect email or password."
        )

    monkeypatch.setattr(cognito_idp, "password_login", reject)
    body = {"email": "ada@example.com", "password": "x" * 14}
    assert [client.post("/v1/auth/login/password", json=body).status_code for _ in range(3)] == [
        401
    ] * 3
    blocked = client.post("/v1/auth/login/password", json=body)
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1
    other = client.post("/v1/auth/login/password", json={**body, "email": "grace@example.com"})
    assert other.status_code == 401


def test_cross_site_origin_is_rejected_on_state_changing_auth_routes(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    body = {"email": "ada@example.com", "password": "x" * 14}
    evil = client.post(
        "/v1/auth/login/password", json=body, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    assert evil.json()["detail"]["code"] == "origin_not_allowed"
    cross_site = client.post("/v1/auth/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert cross_site.status_code == 403
    # Same origin (and a non-browser client without Origin) pass the origin check.
    assert (
        client.post("/v1/auth/logout", headers={"Origin": "http://testserver"}).status_code == 200
    )
    assert client.post("/v1/auth/logout").status_code == 200


def test_configured_extra_origin_is_allowed(client, monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "https://app.example.com, https://other.example.com")
    r = client.post("/v1/auth/logout", headers={"Origin": "https://app.example.com"})
    assert r.status_code == 200


def test_signup_validates_then_calls_cognito_and_never_reveals_existing_accounts(
    client, monkeypatch
):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cognito_idp, "sign_up", lambda e, p: calls.append((e, p)))

    weak = client.post("/v1/auth/signup", json={"email": "ada@example.com", "password": "short"})
    assert weak.status_code == 422
    assert weak.json()["detail"]["code"] == "weak_password"
    bad_email = client.post("/v1/auth/signup", json={"email": "nope", "password": "x" * 14})
    assert bad_email.status_code == 422
    assert calls == []

    ok = client.post(
        "/v1/auth/signup", json={"email": "Ada@Example.com", "password": "correct horse battery"}
    )
    assert ok.status_code == 202
    assert ok.json() == {"status": "verification_required"}
    assert calls == [("ada@example.com", "correct horse battery")]
    assert "set-cookie" not in ok.headers


def test_cognito_sign_up_treats_existing_user_as_success(monkeypatch):
    from botocore.exceptions import ClientError

    class Stub:
        def sign_up(self, **kwargs):
            raise ClientError({"Error": {"Code": "UsernameExistsException"}}, "SignUp")

    monkeypatch.setattr(cognito_idp, "_client", lambda: Stub())
    cognito_idp.sign_up("ada@example.com", "correct horse battery")  # no exception


def test_cognito_login_maps_errors_without_leaking_account_state(monkeypatch):
    from botocore.exceptions import ClientError

    def raising(code):
        class Stub:
            def initiate_auth(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": code, "Message": "ada@example.com is bad"}}, "x"
                )

        return Stub()

    for code in ("UserNotFoundException", "NotAuthorizedException"):
        monkeypatch.setattr(cognito_idp, "_client", lambda code=code: raising(code))
        with pytest.raises(cognito_idp.CognitoFlowError) as exc:
            cognito_idp.password_login("ada@example.com", "pw")
        assert (exc.value.code, exc.value.status_code) == ("invalid_credentials", 401)
        assert "ada@example.com" not in exc.value.message

    monkeypatch.setattr(cognito_idp, "_client", lambda: raising("UserNotConfirmedException"))
    with pytest.raises(cognito_idp.CognitoFlowError) as exc:
        cognito_idp.password_login("ada@example.com", "pw")
    assert exc.value.code == "email_not_verified"

    monkeypatch.setattr(cognito_idp, "_client", lambda: raising("SomethingNew"))
    with pytest.raises(cognito_idp.CognitoFlowError) as exc:
        cognito_idp.password_login("ada@example.com", "pw")
    assert (exc.value.code, exc.value.status_code) == ("auth_unavailable", 502)


def test_cognito_login_rejects_unsupported_challenges(monkeypatch):
    class Stub:
        def initiate_auth(self, **kwargs):
            return {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "s"}

    monkeypatch.setattr(cognito_idp, "_client", lambda: Stub())
    with pytest.raises(cognito_idp.CognitoFlowError) as exc:
        cognito_idp.password_login("ada@example.com", "pw")
    assert exc.value.code == "challenge_unsupported"


def test_cognito_client_secret_adds_secret_hash(monkeypatch):
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "shh")
    seen: dict = {}

    class Stub:
        def initiate_auth(self, **kwargs):
            seen.update(kwargs)
            return {"AuthenticationResult": {"IdToken": "t", "RefreshToken": "r"}}

    monkeypatch.setattr(cognito_idp, "_client", lambda: Stub())
    result = cognito_idp.password_login("ada@example.com", "pw")
    assert result.id_token == "t"
    assert seen["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert seen["AuthParameters"]["SECRET_HASH"]


def test_confirm_resend_and_forgot_are_uniform(client, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cognito_idp, "confirm_sign_up", lambda e, c: seen.append(f"confirm:{c}"))
    monkeypatch.setattr(cognito_idp, "resend_confirmation_code", lambda e: seen.append("resend"))
    monkeypatch.setattr(cognito_idp, "forgot_password", lambda e: seen.append("forgot"))

    assert (
        client.post(
            "/v1/auth/signup/confirm", json={"email": "a@b.co", "code": "12ab56"}
        ).status_code
        == 422
    )
    ok = client.post("/v1/auth/signup/confirm", json={"email": "a@b.co", "code": " 123456 "})
    assert (ok.status_code, ok.json()) == (200, {"status": "verified"})
    assert client.post("/v1/auth/signup/resend", json={"email": "a@b.co"}).status_code == 202
    forgot = client.post("/v1/auth/password/forgot", json={"email": "nobody@b.co"})
    assert (forgot.status_code, forgot.json()) == (202, {"status": "code_sent"})
    assert seen == ["confirm:123456", "resend", "forgot"]


def test_password_reset_revokes_every_existing_session(client, monkeypatch):
    user_id = _create_user()
    token = _issue(user_id)
    assert sa.parse_session(token) == user_id
    monkeypatch.setattr(cognito_idp, "confirm_forgot_password", lambda e, c, p: None)

    weak = client.post(
        "/v1/auth/password/reset",
        json={"email": "ada@example.com", "code": "123456", "new_password": "short"},
    )
    assert weak.status_code == 422
    assert sa.parse_session(token) == user_id  # nothing changed on a rejected request

    r = client.post(
        "/v1/auth/password/reset",
        json={
            "email": "ada@example.com",
            "code": "123456",
            "new_password": "correct horse battery",
        },
    )
    assert (r.status_code, r.json()) == (200, {"status": "password_reset"})
    assert sa.parse_session(token) is None


def test_unknown_fields_are_rejected(client):
    r = client.post(
        "/v1/auth/login/password",
        json={"email": "ada@example.com", "password": "x" * 14, "is_admin": True},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #


def test_logout_revokes_the_server_session_even_if_the_cookie_is_replayed(client):
    user_id = _create_user()
    token = _issue(user_id)
    client.cookies.set("cortex_session", token)
    assert client.get("/v1/auth/me").status_code == 200

    r = client.post("/v1/auth/logout")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cognito_logout_url": None}
    assert "cortex_session=" in r.headers["set-cookie"]

    client.cookies.set("cortex_session", token)  # attacker replays the stolen cookie
    assert client.get("/v1/auth/me").status_code == 401


def test_google_logout_returns_cognito_logout_url(client, monkeypatch):
    monkeypatch.setenv("COGNITO_LOGOUT_REDIRECT_URI", "https://app.example.com/login")
    token = _issue(_create_user(), "google")
    client.cookies.set("cortex_session", token)
    body = client.post("/v1/auth/logout").json()
    url = urlsplit(body["cognito_logout_url"])
    query = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.example.com/logout"
    assert query["client_id"] == ["client-id"]
    assert query["logout_uri"] == ["https://app.example.com/login"]


# --------------------------------------------------------------------------- #
# Google (Cognito federated) with state + PKCE + nonce
# --------------------------------------------------------------------------- #


def _start_google(client, next_path="/usage"):
    r = client.get(f"/v1/auth/google/start?next={next_path}", follow_redirects=False)
    assert r.status_code == 302
    return r, parse_qs(urlsplit(r.headers["location"]).query)


def test_google_start_skips_hosted_ui_and_binds_state_pkce_and_nonce(client):
    r, query = _start_google(client)
    location = urlsplit(r.headers["location"])
    assert (location.netloc, location.path) == ("auth.example.com", "/oauth2/authorize")
    assert query["identity_provider"] == ["Google"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://testserver/v1/auth/callback"]
    assert query["state"][0] and query["nonce"][0] and query["code_challenge"][0]

    cookie = r.headers["set-cookie"].lower()
    assert "cortex_oauth_tx=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    assert tx.state == query["state"][0]
    assert auth_security.pkce_challenge(tx.code_verifier) == query["code_challenge"][0]
    assert tx.nonce == query["nonce"][0]
    assert tx.next_path == "/usage"
    assert "code_verifier" not in r.headers["location"]


def test_google_start_never_stores_an_external_next_target(client):
    _start_google(client, next_path="https://evil.example")
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    assert tx.next_path == "/"


def _stub_exchange(monkeypatch, *, nonce=None, verified=True, email="ada@example.com"):
    import server.routes.auth as auth_route

    exchanged: dict = {}

    def exchange(code, redirect_uri, code_verifier=None):
        exchanged.update(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)
        return {"id_token": "id-token"}

    monkeypatch.setattr(auth_route, "exchange_code_for_tokens", exchange)
    monkeypatch.setattr(
        auth_route,
        "verify_cognito_id_token",
        lambda t: _claims(email, sub="google-sub", verified=verified, nonce=nonce),
    )
    return exchanged


def test_google_callback_success_sends_verifier_and_issues_session(client, monkeypatch):
    _, query = _start_google(client)
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    exchanged = _stub_exchange(monkeypatch, nonce=tx.nonce)

    r = client.get(f"/v1/auth/callback?code=abc&state={query['state'][0]}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/usage?fresh_login=1"
    assert exchanged["code_verifier"] == tx.code_verifier
    cookies = " ".join(r.headers.get_list("set-cookie")).lower()
    assert "cortex_session=" in cookies and "httponly" in cookies
    assert 'cortex_oauth_tx=""' in cookies or "cortex_oauth_tx=;" in cookies  # one-shot

    client.cookies.delete("cortex_oauth_tx")
    assert client.get("/v1/auth/me").status_code == 200


def test_google_callback_rejects_missing_or_wrong_state(client, monkeypatch):
    _start_google(client)
    exchanged = _stub_exchange(monkeypatch)

    wrong = client.get("/v1/auth/callback?code=abc&state=forged", follow_redirects=False)
    assert wrong.headers["location"] == "/login?error=session_expired"
    assert exchanged == {}  # the code was never exchanged
    assert "cortex_session=" not in " ".join(wrong.headers.get_list("set-cookie"))

    client.cookies.delete("cortex_oauth_tx")
    no_cookie = client.get("/v1/auth/callback?code=abc&state=anything", follow_redirects=False)
    assert no_cookie.headers["location"] == "/login?error=session_expired"


def test_google_callback_rejects_nonce_mismatch_and_unverified_email(client, monkeypatch):
    _, query = _start_google(client)
    _stub_exchange(monkeypatch, nonce="someone-elses-nonce")
    replay = client.get(
        f"/v1/auth/callback?code=abc&state={query['state'][0]}", follow_redirects=False
    )
    assert replay.headers["location"] == "/login?error=google_failed"

    _, query = _start_google(client)
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    _stub_exchange(monkeypatch, nonce=tx.nonce, verified=False)
    unverified = client.get(
        f"/v1/auth/callback?code=abc&state={query['state'][0]}", follow_redirects=False
    )
    assert unverified.headers["location"] == "/login?error=email_not_verified"


def test_google_callback_conflict_cancel_and_direct_visit(client, monkeypatch):
    _create_user("ada@example.com", subject="password-sub")
    _, query = _start_google(client)
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    _stub_exchange(monkeypatch, nonce=tx.nonce)
    conflict = client.get(
        f"/v1/auth/callback?code=abc&state={query['state'][0]}", follow_redirects=False
    )
    assert conflict.headers["location"] == "/login?error=account_exists"
    assert "cortex_session=" not in " ".join(conflict.headers.get_list("set-cookie"))

    cancelled = client.get("/v1/auth/callback?error=access_denied", follow_redirects=False)
    assert cancelled.headers["location"] == "/login?error=google_cancelled"
    direct = client.get("/auth", follow_redirects=False)
    assert direct.headers["location"] == "/login"


def test_google_callback_succeeds_at_the_legacy_auth_path(client, monkeypatch):
    monkeypatch.setenv("COGNITO_REDIRECT_URI", "http://testserver/auth")
    _, query = _start_google(client, next_path="/")
    assert query["redirect_uri"] == ["http://testserver/auth"]
    tx = auth_security.decode_oauth_transaction(client.cookies.get("cortex_oauth_tx"))
    exchanged = _stub_exchange(monkeypatch, nonce=tx.nonce)
    r = client.get(f"/auth?code=abc&state={query['state'][0]}", follow_redirects=False)
    assert r.headers["location"] == "/?fresh_login=1"
    assert exchanged["redirect_uri"] == "http://testserver/auth"


# --------------------------------------------------------------------------- #
# Dev login keeps working through server-side sessions
# --------------------------------------------------------------------------- #


def test_dev_login_issues_a_server_side_session(client, monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "true")
    r = client.post("/v1/auth/dev-login")
    assert r.status_code == 200
    assert client.get("/v1/auth/me").status_code == 200
    client.post("/v1/auth/logout")
    assert client.get("/v1/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Browser hardening headers
# --------------------------------------------------------------------------- #


def test_security_headers_are_added_to_api_and_auth_responses(client):
    for path in ("/health", "/v1/auth/cognito-config"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "SAMEORIGIN"
        assert headers["content-security-policy"] == "frame-ancestors 'self'"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "strict-transport-security" not in headers  # plain HTTP

    secure = TestClient(client.app, base_url="https://testserver")
    assert "max-age=" in secure.get("/health").headers["strict-transport-security"]
