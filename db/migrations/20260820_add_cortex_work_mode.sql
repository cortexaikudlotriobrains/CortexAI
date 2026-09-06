-- CortexAI Work: durable agent sessions, runs, events, tools, approvals, and recovery.
-- Long-running execution remains provider-owned; these tables are the Cortex
-- authorization, billing, audit, and reconnect source of truth.

BEGIN;

ALTER TABLE public.sessions
    DROP CONSTRAINT IF EXISTS sessions_mode_check;

ALTER TABLE public.sessions
    ADD CONSTRAINT sessions_mode_check
    CHECK (mode = ANY (ARRAY['ask', 'compare', 'eval', 'research', 'work']::text[]));

CREATE TABLE IF NOT EXISTS public.work_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'idle',
    agent_provider text NOT NULL,
    provider_session_id text NULL,
    provider_agent_id text NULL,
    provider_environment_id text NULL,
    default_tool_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_work_sessions_session UNIQUE (session_id),
    CONSTRAINT ck_work_sessions_status CHECK (
        status IN ('idle', 'running', 'waiting_for_approval', 'completed', 'failed', 'cancelled')
    )
);

CREATE INDEX IF NOT EXISTS ix_work_sessions_user_updated
    ON public.work_sessions (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_work_sessions_provider_session
    ON public.work_sessions (provider_session_id)
    WHERE provider_session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_work_sessions_status_updated
    ON public.work_sessions (status, updated_at);

CREATE TABLE IF NOT EXISTS public.work_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_session_id uuid NOT NULL REFERENCES public.work_sessions(id) ON DELETE CASCADE,
    request_id text NOT NULL,
    instruction text NOT NULL,
    status text NOT NULL DEFAULT 'created',
    provider text NOT NULL,
    provider_run_id text NULL,
    max_credit_budget bigint NOT NULL CHECK (max_credit_budget > 0),
    reserved_credits bigint NOT NULL DEFAULT 0 CHECK (reserved_credits >= 0),
    actual_credits bigint NOT NULL DEFAULT 0 CHECK (actual_credits >= 0),
    billing_reservation_id uuid NULL REFERENCES public.usage_reservations(id) ON DELETE SET NULL,
    configuration_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    usage_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_cost_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_cursor text NULL,
    next_event_sequence bigint NOT NULL DEFAULT 1 CHECK (next_event_sequence > 0),
    started_at timestamptz NULL,
    completed_at timestamptz NULL,
    stop_reason text NULL,
    error_code text NULL,
    error_message text NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_work_runs_session_request UNIQUE (work_session_id, request_id),
    CONSTRAINT ck_work_runs_status CHECK (
        status IN (
            'created', 'planning', 'running', 'waiting_for_approval',
            'completed', 'failed', 'cancelled', 'budget_exhausted'
        )
    )
);

CREATE INDEX IF NOT EXISTS ix_work_runs_session_created
    ON public.work_runs (work_session_id, created_at);
CREATE INDEX IF NOT EXISTS ix_work_runs_status_updated
    ON public.work_runs (status, updated_at);

CREATE TABLE IF NOT EXISTS public.work_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_run_id uuid NOT NULL REFERENCES public.work_runs(id) ON DELETE CASCADE,
    sequence_number bigint NOT NULL,
    event_type text NOT NULL,
    display_message text NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_event_id text NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_work_events_run_sequence UNIQUE (work_run_id, sequence_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_events_run_provider_event
    ON public.work_events (work_run_id, provider_event_id)
    WHERE provider_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_work_events_run_sequence
    ON public.work_events (work_run_id, sequence_number);

CREATE TABLE IF NOT EXISTS public.work_run_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_run_id uuid NOT NULL REFERENCES public.work_runs(id) ON DELETE CASCADE,
    file_id uuid NOT NULL REFERENCES public.uploaded_files(id) ON DELETE CASCADE,
    role text NOT NULL,
    source text NOT NULL,
    provider_file_id text NULL,
    artifact_type text NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_work_run_files_role CHECK (role IN ('input', 'artifact')),
    CONSTRAINT ck_work_run_files_source CHECK (source IN ('user', 'agent', 'connector')),
    CONSTRAINT uq_work_run_files_link UNIQUE (work_run_id, file_id, role)
);

CREATE INDEX IF NOT EXISTS ix_work_run_files_run_role
    ON public.work_run_files (work_run_id, role, created_at);

CREATE TABLE IF NOT EXISTS public.tool_connections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    connector_key text NOT NULL,
    connection_type text NOT NULL,
    display_name text NOT NULL,
    server_url text NULL,
    auth_type text NOT NULL,
    credential_reference text NULL,
    provider_vault_id text NULL,
    status text NOT NULL DEFAULT 'pending',
    granted_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    last_verified_at timestamptz NULL,
    CONSTRAINT ck_tool_connections_type CHECK (connection_type IN ('cortex_builtin', 'mcp_remote')),
    CONSTRAINT ck_tool_connections_status CHECK (
        status IN ('pending', 'connected', 'expired', 'error', 'disabled')
    )
);

CREATE INDEX IF NOT EXISTS ix_tool_connections_user_updated
    ON public.tool_connections (user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_tool_connections_user_status
    ON public.tool_connections (user_id, status);

CREATE TABLE IF NOT EXISTS public.work_run_connections (
    work_run_id uuid NOT NULL REFERENCES public.work_runs(id) ON DELETE CASCADE,
    connection_id uuid NOT NULL REFERENCES public.tool_connections(id) ON DELETE RESTRICT,
    configuration_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (work_run_id, connection_id)
);

CREATE TABLE IF NOT EXISTS public.work_tool_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_run_id uuid NOT NULL REFERENCES public.work_runs(id) ON DELETE CASCADE,
    provider_call_id text NULL,
    connection_id uuid NULL REFERENCES public.tool_connections(id) ON DELETE SET NULL,
    tool_source text NOT NULL,
    tool_name text NOT NULL,
    action_class text NOT NULL,
    status text NOT NULL DEFAULT 'requested',
    request_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NULL,
    completed_at timestamptz NULL,
    cost_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_work_tool_calls_source CHECK (tool_source IN ('builtin', 'cortex', 'mcp')),
    CONSTRAINT ck_work_tool_calls_action CHECK (
        action_class IN ('READ', 'WRITE', 'DESTRUCTIVE', 'EXTERNAL_COMMUNICATION', 'FINANCIAL', 'DEPLOYMENT')
    ),
    CONSTRAINT ck_work_tool_calls_status CHECK (
        status IN ('requested', 'running', 'awaiting_approval', 'succeeded', 'failed', 'denied')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_tool_calls_provider_call
    ON public.work_tool_calls (work_run_id, provider_call_id)
    WHERE provider_call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_work_tool_calls_run_status
    ON public.work_tool_calls (work_run_id, status, started_at);

CREATE TABLE IF NOT EXISTS public.work_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    work_run_id uuid NOT NULL REFERENCES public.work_runs(id) ON DELETE CASCADE,
    tool_call_id uuid NOT NULL REFERENCES public.work_tool_calls(id) ON DELETE CASCADE,
    connection_id uuid NULL REFERENCES public.tool_connections(id) ON DELETE SET NULL,
    action_type text NOT NULL,
    tool_name text NOT NULL,
    description text NOT NULL,
    request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    requested_at timestamptz NOT NULL DEFAULT NOW(),
    decided_at timestamptz NULL,
    decided_by uuid NULL REFERENCES public.users(id) ON DELETE SET NULL,
    CONSTRAINT ck_work_approvals_status CHECK (status IN ('pending', 'approved', 'denied', 'expired'))
);

CREATE INDEX IF NOT EXISTS ix_work_approvals_run
    ON public.work_approvals (work_run_id, requested_at);
CREATE INDEX IF NOT EXISTS ix_work_approvals_pending
    ON public.work_approvals (status, requested_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS public.work_oauth_states (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    state_hash text NOT NULL UNIQUE,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    connector_key text NOT NULL,
    redirect_uri text NOT NULL,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    created_at timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_work_oauth_states_expiry
    ON public.work_oauth_states (expires_at)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS public.work_sync_leases (
    work_run_id uuid PRIMARY KEY REFERENCES public.work_runs(id) ON DELETE CASCADE,
    lease_owner text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT NOW()
);

COMMIT;
