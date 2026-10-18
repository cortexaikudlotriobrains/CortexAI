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
| `SESSION_IDLE_TIMEOUT_SECONDS` | `259200` (3 days) | Sessions unused this long end (`0` disables) |
| `AUTH_ALLOWED_ORIGINS` | – | Trusted browser origins of a separately hosted frontend (comma-separated): allowed on auth POSTs and for credentialed CORS |
| `AUTH_RATE_LIMIT_IP_PER_MINUTE` | `20` | Per-client-IP throttle on auth routes (`0` disables) |
| `AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN` | `8` | Per-email throttle, per action (`0` disables) |
| `AUTH_BREACHED_PASSWORD_CHECK` | `true` | Reject sign-up/reset passwords found in known breaches (Have I Been Pwned range API; fails open) |
| `AUTH_MAINTENANCE_INTERVAL_SECONDS` | `3600` | Background purge of long-ended sessions and old rate-limit windows (`0` disables) |
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
Errors: `422 invalid_email | weak_password | breached_password`, `429 rate_limited`,
`502 auth_unavailable`.

### `POST /v1/auth/signup/confirm` → `200 {"status": "verified"}`

Body `{"email", "code"}` (6 digits). Errors: `400 invalid_code | expired_code`, `422`, `429`.

### `POST /v1/auth/signup/resend` → `202 {"status": "code_sent"}`

Body `{"email"}`. Identical for unknown or already-verified emails.

### `POST /v1/auth/login/password` → `200 {"user_id", "email"}` + session cookie

Body `{"email", "password"}`. Errors: `401 invalid_credentials`, `403 email_not_verified |
password_reset_required | challenge_unsupported | account_disabled`, `409
account_exists_other_method`, `429 rate_limited`, `502 auth_unavailable`.

### `POST /v1/auth/password/forgot` → `202 {"status": "code_sent"}`

Body `{"email"}`. Identical for unknown accounts.

### `POST /v1/auth/password/reset` → `200 {"status": "password_reset"}`

Body `{"email", "code", "new_password"}`. **Revokes every existing session of the user.**
Errors include `422 weak_password | breached_password`.

### `GET /v1/auth/google/start?next=/path`

`302` to Cognito with `identity_provider=Google`, `state`, PKCE `code_challenge` (S256), and
`nonce`; sets the short-lived (10 min), signed, `HttpOnly` transaction cookie
(`__Host-cortex_oauth_tx` on HTTPS, `cortex_oauth_tx` on plain HTTP) holding the
state, PKCE verifier, nonce and validated `next` path.

### `GET /v1/auth/callback` and `GET /auth`

Both run the same handler. It requires the transaction cookie, a matching `state`, sends
the PKCE verifier with the code exchange, requires the ID token `nonce` to match and
`email_verified=true`, then sets the session cookie and redirects to the validated `next` path
with `fresh_login=1`. Every failure redirects to `/login?error=<reason>` with one of
`google_cancelled`, `google_failed`, `session_expired`, `email_not_verified`,
`account_exists`, `account_disabled`. Only Cognito's `error=access_denied` (the user backed
out) maps to `google_cancelled`; any other provider error (for example `invalid_scope`,
`unauthorized_client`, `invalid_request`) is a configuration problem, shown as `google_failed`
and logged as `auth.oauth.provider_error` with Cognito's `error` and `error_description`.

### `POST /v1/auth/logout` → `200 {"ok": true, "cognito_logout_url": string | null}`

Revokes the server-side session (replaying a copied cookie fails afterwards) and clears the
cookie. `cognito_logout_url` is set only for Google sessions.

### `POST /v1/auth/login`

Token clients: body `{"id_token"}`; same session issuance and account checks.

### `GET /v1/auth/sessions` → `200 {"sessions": [...]}`

The signed-in user's live sessions, most recently active first: `id`, `auth_method`,
`created_at`, `last_seen_at`, `expires_at`, `user_agent`, `current`. Token hashes and IP
hashes are never returned. Shown in React at `/account/security` ("Signed-in devices").

### `DELETE /v1/auth/sessions/{session_id}` → `200 {"ok": true}`

Signs out one of the user's own sessions. Another user's session id returns the same
`404 session_not_found` as a missing one.

### `POST /v1/auth/sessions/revoke-others` → `200 {"ok": true, "revoked": n}`

Signs out every other device and keeps the current session.

## Security model

- **Sessions**: opaque random token, hashed at rest, server-side absolute expiry
  (`SESSION_MAX_AGE_SECONDS`) and idle timeout (`SESSION_IDLE_TIMEOUT_SECONDS`), revocation,
  disabled users rejected on every request (`users.is_active`), `last_seen_at` refreshed at most
  every 5 minutes. Users can see and end their sessions at `/account/security`. Cookie:
  `HttpOnly`, `SameSite=Lax`, `Path=/`; on HTTPS it is `Secure` and named
  `__Host-cortex_session`, so no sibling subdomain can set or overwrite it, and the unprefixed
  name is ignored there. Plain-HTTP development keeps `cortex_session`. A new token is issued at
  every login (no session fixation).
- **Brute force**: per-IP and per-email fixed-window throttles on every auth route, stored in
  the shared `auth_rate_limits` table so they hold across every API instance (bounded in-memory
  fallback if the table is unreachable), plus Cognito's own lockout.
- **Breached passwords**: new passwords (sign-up, reset) are checked against Have I Been Pwned
  with k-anonymity: only the first 5 hex characters of the SHA-1 leave the server and responses
  are padded. If the service is unreachable the check is skipped (logged), never blocking sign-up.
- **CORS**: only trusted origins (`AUTH_ALLOWED_ORIGINS`, the origin of
  `COGNITO_SUCCESS_REDIRECT_URI`, loopback outside production) get credentialed CORS. Every
  other origin gets `Access-Control-Allow-Origin: *` without credentials, so API-key/Bearer
  calls still work but no other site, including a sibling subdomain, can read a
  cookie-authenticated response. The React app calls the API on its own origin and needs no CORS.
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
  `Content-Security-Policy: frame-ancestors 'self'` on every response; HSTS on HTTPS.
- **Content Security Policy** (`frontend-react/index.html`): production builds allow scripts only
  from the app's own origin (no inline scripts; `strictProductionCsp` in `vite.config.ts` drops
  `'unsafe-inline'`, which only the dev server's React Refresh preamble needs), plus
  `object-src 'none'`, `base-uri 'self'` and `form-action 'self'`.
- **Audit log**: `auth.login.succeeded`, `auth.login.failed` (with `code`), `auth.signup.requested`,
  `auth.password.reset`, `auth.rate_limited`, `auth.logout` and `auth.session.revoked` are
  structured log events with `email_hash` / `ip_hash` (keyed hashes, never raw values). Alert on
  spikes of `auth.login.failed` or `auth.rate_limited` per hash.
- **Failed sign-out**: if the logout request fails, React does not pretend the user is signed out;
  `/login?error=logout_failed` shows "You're still signed in" with a retry.

## Rollout

1. Configure the Cognito app client (above) and set `SESSION_SECRET` (and `COGNITO_REDIRECT_URI`) in the
   production environment.
2. Apply `db/migrations/20261018_add_auth_sessions.sql` (additive; creates `auth_sessions` and
   `auth_rate_limits`). New API builds refuse to start on PostgreSQL without them, so migrate first.
3. Deploy API + frontend together. **All existing sessions end once**: the old cookie format is no longer
   accepted, so users sign in again.
4. Verify: sign up → verify → sign in → refresh → log off; Google sign-in; wrong password; reset password
   (other devices are signed out).

Rollback: deploy the previous build; the extra table is harmless. Users sign in again.

## Known limitations / follow-ups

- No MFA (TOTP) yet. Add the challenge step to `server/cognito_idp.py` and the login page.
- No step-up re-authentication before sensitive actions (billing).
- The IP throttle keys on the client address resolved through `TRUSTED_PROXY_IPS` (default `*`,
  which trusts forwarded headers from any upstream); narrow that list to your load balancer so
  clients cannot rotate the header to dodge the IP limit (the per-email limit and Cognito's lockout
  still apply). There is no CAPTCHA; AWS WAF Bot Control or a CAPTCHA on sign-up needs operator setup.
- The breached-password check needs outbound HTTPS to `api.pwnedpasswords.com`; without it the
  check is skipped (fail open).
- Operator-side hardening not covered by code: Cognito threat protection, a custom Cognito domain,
  and SES-sent verification emails from your own domain.
