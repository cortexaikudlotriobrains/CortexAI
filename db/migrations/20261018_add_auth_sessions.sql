BEGIN;

-- Server-side browser sessions. The cookie carries only a random opaque token;
-- this table stores its keyed hash, so a database read cannot be replayed as a
-- login and any session can be revoked (logout, password reset, user disable).
-- Existing signed-user-id cookies are not accepted after this change ships:
-- users sign in once.
CREATE TABLE IF NOT EXISTS public.auth_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL,
    auth_method varchar(32) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    last_seen_at timestamptz NOT NULL DEFAULT NOW(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz NULL,
    revoked_reason varchar(64) NULL,
    user_agent varchar(256) NULL,
    ip_hash char(64) NULL,
    CONSTRAINT ck_auth_sessions_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_auth_sessions_method CHECK (
        auth_method IN ('password', 'google', 'cognito_token', 'dev')
    ),
    CONSTRAINT ck_auth_sessions_revocation CHECK (
        (revoked_at IS NULL AND revoked_reason IS NULL) OR revoked_at IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_sessions_token_hash
    ON public.auth_sessions (token_hash);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_active
    ON public.auth_sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_auth_sessions_expires_at
    ON public.auth_sessions (expires_at);

-- Fixed-window brute-force counters shared by every API instance. bucket_key is a
-- keyed hash of "<action>:<ip|email>"; old windows are purged by the API.
CREATE TABLE IF NOT EXISTS public.auth_rate_limits (
    bucket_key char(64) NOT NULL,
    window_start timestamptz NOT NULL,
    hits integer NOT NULL DEFAULT 0,
    CONSTRAINT pk_auth_rate_limits PRIMARY KEY (bucket_key, window_start),
    CONSTRAINT ck_auth_rate_limits_hits CHECK (hits >= 0)
);

CREATE INDEX IF NOT EXISTS ix_auth_rate_limits_window_start
    ON public.auth_rate_limits (window_start);

COMMIT;
