# Runbook: In-App Login (Email/Password + Google)

CortexAI signs users in on its own `/login` page. Amazon Cognito stays the identity
store, but the browser never visits a Cognito-hosted page: the FastAPI backend
calls the Cognito user-pool APIs server-side (email/password) or sends the browser
straight to Google through Cognito (Google), then issues its own server-side
session.

## Flow overview

| Method | Browser → API | API → Cognito |
| --- | --- | --- |
| Create account | `POST /v1/auth/signup` | `SignUp` (email is the username) |
| Verify email | `POST /v1/auth/signup/confirm`, `/signup/resend` | `ConfirmSignUp`, `ResendConfirmationCode` |
| Email + password | `POST /v1/auth/login/password` | `InitiateAuth` (`USER_PASSWORD_AUTH`), then `RevokeToken` on the refresh token |
| Forgot / reset | `POST /v1/auth/password/forgot`, `/password/reset` | `ForgotPassword`, `ConfirmForgotPassword` |
| Google | `GET /v1/auth/google/start` → Cognito `/oauth2/authorize?identity_provider=Google` → Google → `GET /v1/auth/callback` (or `/auth`) | `/oauth2/token` (code + PKCE verifier) |
| Logout | `POST /v1/auth/logout` | none (Google sessions get a Cognito logout URL) |

After any successful sign-in the backend verifies the Cognito ID token (signature,
issuer, audience, expiry, `token_use=id`, `email_verified`), upserts the user, and
creates an `auth_sessions` row. The `cortex_session` cookie holds only a random
256-bit token; the database stores its HMAC-SHA256 hash.

The user-pool calls are the public (unsigned) APIs, so the API process needs **no
AWS credentials** for them.

## Cognito setup (outside this repository)

The repository has no AWS IaC, so these are operator steps:

1. **User pool**: sign-in with **email** as the username attribute, email verification
   on (code sent by email), self-registration enabled. Set the password policy to at
   least the application policy (12+ characters; the API enforces 12–128 and rejects
   passwords containing the email address; Cognito's own policy still applies).
2. **App client** (the one in `COGNITO_CLIENT_ID`):
   - Authentication flows: `ALLOW_USER_PASSWORD_AUTH` and `ALLOW_REFRESH_TOKEN_AUTH`
     (the refresh token is revoked immediately after login and never used).
   - **Prevent user existence errors: Enabled** (so unknown users and wrong passwords are
     indistinguishable).
   - Refresh token revocation enabled.
   - OAuth: authorization code grant; scopes `openid`, `email`, `profile`; identity providers
     `Google` (and Cognito user pool if you keep the hosted UI reachable for other tools).
   - Allowed callback URL(s): exactly the value of `COGNITO_REDIRECT_URI`
     (for example `https://app.example.com/v1/auth/callback`); allowed sign-out URL:
     `COGNITO_LOGOUT_REDIRECT_URI` (default `<origin>/login`).
   - With an app-client secret, set `COGNITO_CLIENT_SECRET`; the backend adds `SECRET_HASH`.
3. **Google identity provider**: attribute mapping `email → email`; Google users arrive with a
   verified email.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_REGION`, `COGNITO_DOMAIN` | – | Pool, app client, Google redirect/token exchange |
| `COGNITO_REDIRECT_URI` | `<origin>/v1/auth/callback` | Exact registered callback; required in production |
| `COGNITO_LOGOUT_REDIRECT_URI` | `<origin>/login` | Registered sign-out URL for Google sessions |
| `COGNITO_SUCCESS_REDIRECT_URI` | – | Only for a separately hosted frontend (absolute URL) |
| `SESSION_SECRET` | dev default | **Production must set ≥ 32 random characters** or startup fails |
| `SESSION_MAX_AGE_SECONDS` | `604800` | Cookie and server-side session lifetime (`0` = browser-session cookie, 12 h server cap) |
| `AUTH_PASSWORD_LOGIN_ENABLED`, `AUTH_SIGNUP_ENABLED`, `AUTH_GOOGLE_LOGIN_ENABLED` | `true` | Turn individual methods off (reflected in `GET /v1/auth/cognito-config`) |
| `AUTH_ALLOWED_ORIGINS` | – | Extra browser origins allowed on auth POSTs (comma-separated) |
| `AUTH_RATE_LIMIT_IP_PER_MINUTE` | `20` | Per-client-IP throttle on auth routes (`0` disables) |
| `AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN` | `8` | Per-email throttle, per action (`0` disables) |
| `ENABLE_SECURITY_HEADERS` | `true` | Baseline response headers (see below) |

Generate the secret with, for example, `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Rotating `SESSION_SECRET` invalidates every session (token hashes are keyed with it) and
every in-flight Google sign-in.

## API reference

All bodies are JSON and strict (unknown fields are rejected with `422`). All responses carry
`Cache-Control: no-store`. Errors use `{"detail": {"code", "message"}}`; the React client
maps `code` to its own copy and never renders `message`.

### `GET /v1/auth/cognito-config`

Public config (`{}` when Cognito is not configured) plus `passwordLoginEnabled`,
`signupEnabled`, `googleLoginEnabled`.

### `POST /v1/auth/signup` → `202 {"status": "verification_required"}`

Body `{"email", "password"}`. The response is identical when the email already exists.
Errors: `422 invalid_email | weak_password`, `429 rate_limited`, `502 auth_unavailable`.

### `POST /v1/auth/signup/confirm` → `200 {"status": "verified"}`

Body `{"email", "code"}` (6 digits). Errors: `400 invalid_code | expired_code`, `422`, `429`.

### `POST /v1/auth/signup/resend` → `202 {"status": "code_sent"}`

Body `{"email"}`. Identical for unknown or already-verified emails.

### `POST /v1/auth/login/password` → `200 {"user_id", "email"}` + `Set-Cookie: cortex_session`

Body `{"email", "password"}`. Errors: `401 invalid_credentials`, `403 email_not_verified |
password_reset_required | challenge_unsupported | account_disabled`, `409
account_exists_other_method`, `429 rate_limited`, `502 auth_unavailable`.

### `POST /v1/auth/password/forgot` → `202 {"status": "code_sent"}`

Body `{"email"}`. Identical for unknown accounts.

### `POST /v1/auth/password/reset` → `200 {"status": "password_reset"}`

Body `{"email", "code", "new_password"}`. **Revokes every existing session of the user.**

### `GET /v1/auth/google/start?next=/path`

`302` to Cognito with `identity_provider=Google`, `state`, PKCE `code_challenge` (S256), and
`nonce`; sets the short-lived (10 min), signed, `HttpOnly` `cortex_oauth_tx` cookie holding the
state, PKCE verifier, nonce and validated `next` path.

### `GET /v1/auth/callback` and `GET /auth`

Both run the same handler. It requires the `cortex_oauth_tx` cookie, a matching `state`, sends
the PKCE verifier with the code exchange, requires the ID token `nonce` to match and
`email_verified=true`, then sets `cortex_session` and redirects to the validated `next` path
with `fresh_login=1`. Every failure redirects to `/login?error=<reason>` with one of
`google_cancelled`, `google_failed`, `session_expired`, `email_not_verified`,
`account_exists`, `account_disabled`.

### `POST /v1/auth/logout` → `200 {"ok": true, "cognito_logout_url": string | null}`

Revokes the server-side session (replaying a copied cookie fails afterwards) and clears the
cookie. `cognito_logout_url` is set only for Google sessions.

### `POST /v1/auth/login`

Token clients: body `{"id_token"}`; same session issuance and account checks.

## Security model

- **Sessions**: opaque random token, hashed at rest, server-side expiry and revocation,
  disabled users rejected on every request (`users.is_active`), `last_seen_at` refreshed at most
  every 5 minutes. Cookie: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` when the effective
  request scheme is HTTPS (or `SESSION_COOKIE_SECURE`). A new token is issued at every login
  (no session fixation).
- **Brute force**: per-IP and per-email throttles on every auth route plus Cognito's own
  lockout. Throttles are per API process; run the API behind a WAF rate rule for fleet-wide limits.
- **No account enumeration**: sign-up, resend, forgot-password and login give identical
  responses for unknown and existing accounts.
- **CSRF / login CSRF**: `SameSite=Lax`, a same-origin check on auth POSTs (cross-site `Origin` or
  `Sec-Fetch-Site: cross-site` → `403 origin_not_allowed`), and `state` + PKCE + `nonce` on Google.
- **Open redirects**: `next` must be a same-origin absolute path and may not point at
  `/login`, `/auth` or `/v1/`; checked server-side and client-side.
- **Secrets**: passwords and codes are never logged or put in URLs; the password is held only in
  React component state and cleared on view change and after sign-in; refresh tokens are revoked
  immediately.
- **Account linking**: a Google user and an email/password user with the same address are *not*
  merged. The second sign-in method is refused with `account_exists_other_method` /
  `/login?error=account_exists`. Merging needs a Cognito pre-sign-up trigger and is a separate
  decision.
- **Headers**: `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options: SAMEORIGIN` and
  `Content-Security-Policy: frame-ancestors 'self'` on every response; HSTS on HTTPS. A full
  script CSP is not enabled.

## Rollout

1. Configure the Cognito app client (above) and set `SESSION_SECRET` (and `COGNITO_REDIRECT_URI`) in the
   production environment.
2. Apply `db/migrations/20261018_add_auth_sessions.sql` (additive). New API builds refuse to start on
   PostgreSQL without the table, so migrate first.
3. Deploy API + frontend together. **All existing sessions end once**: the old cookie format is no longer
   accepted, so users sign in again.
4. Verify: sign up → verify → sign in → refresh → log off; Google sign-in; wrong password; reset password
   (other devices are signed out).

Rollback: deploy the previous build; the extra table is harmless. Users sign in again.

## Known limitations / follow-ups

- No MFA (TOTP) yet. Add the challenge step to `server/cognito_idp.py` and the login page.
- CORS is `allow_origins=["*"]` with credentials (reflecting the request origin). `SameSite=Lax`
  cookies and the origin check above limit the impact for auth routes; narrowing it is a separate change.
- Rate limits are per process and the IP throttle keys on the client address resolved through
  `TRUSTED_PROXY_IPS` (default `*`, which trusts forwarded headers from any upstream); narrow that list
  when the API is directly reachable, otherwise rotate the header to dodge the IP limit (the per-email
  limit and Cognito's lockout still apply). There is no CAPTCHA.
- Expired/revoked `auth_sessions` rows are not purged automatically; they are small and indexed
  (`ix_auth_sessions_expires_at`). Delete rows with `expires_at < now() - interval '30 days'` periodically.
