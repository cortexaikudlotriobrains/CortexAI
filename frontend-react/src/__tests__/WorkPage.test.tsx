import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkPage } from "../pages/WorkPage";
import { useWorkStore } from "../store/workStore";
import type { WorkArtifact, WorkEvent, WorkRun, WorkSession } from "../types";

const apiMocks = vi.hoisted(() => ({
  beginToolOAuth: vi.fn(),
  cancelWorkRun: vi.fn(),
  createToolConnection: vi.fn(),
  createWorkSession: vi.fn(),
  decideWorkApproval: vi.fn(),
  getLatestWorkRun: vi.fn(),
  getWorkApproval: vi.fn(),
  getWorkEvents: vi.fn(),
  getWorkRun: vi.fn(),
  getWorkSession: vi.fn(),
  listToolCatalog: vi.fn(),
  listToolConnections: vi.fn(),
  listWorkArtifacts: vi.fn(),
  listWorkRuns: vi.fn(),
  listWorkSessions: vi.fn(),
  sendWorkInstruction: vi.fn(),
  startWorkRun: vi.fn(),
  streamWorkEvents: vi.fn(),
  testToolConnection: vi.fn(),
}));

vi.mock("../api/work", () => apiMocks);
vi.mock("../config/runtimeConfig", () => ({
  getRuntimeConfig: () => ({ workEnabled: true }),
}));
vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    whoAmI: null,
    cognitoConfig: { enabled: false },
    loading: false,
    loggedIn: true,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));
vi.mock("../hooks/useSubscription", () => ({
  useSubscription: () => ({
    entitlements: {
      features: { work_enabled: true },
      limits: { max_work_credit_budget: 1_000_000 },
    },
  }),
}));
vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "light", toggleTheme: vi.fn() }),
}));
vi.mock("../subscription/accountMenuPresentation", () => ({
  getAccountMenuSubscriptionPresentation: () => ({
    planLabel: "Pro",
    billingActionLabel: "Manage plan",
    billingPastDue: false,
    billingDestination: null,
  }),
}));
vi.mock("../components/layout/Sidebar", () => ({ Sidebar: () => null }));
vi.mock("../components/layout/AccountMenu", () => ({ AccountMenu: () => null }));
vi.mock("../components/subscription/SubscriptionBanner", () => ({
  SubscriptionBanner: () => null,
}));
vi.mock("../components/work/WorkComposer", () => ({
  WorkComposer: ({
    value,
    onChange,
    onSubmit,
    busy,
  }: {
    value: string;
    onChange: (value: string) => void;
    onSubmit: () => void;
    busy: boolean;
  }) => (
    <div>
      <textarea
        aria-label="Work goal"
        value={value}
        onChange={(event) => onChange(event.currentTarget.value)}
      />
      <button type="button" onClick={onSubmit} disabled={busy || !value.trim()}>
        Start work
      </button>
    </div>
  ),
}));
vi.mock("../components/work/WorkRail", () => ({ WorkRail: () => null }));
vi.mock("../components/work/WorkArtifacts", () => ({
  WorkArtifacts: ({ artifacts }: { artifacts: WorkArtifact[] }) => (
    <div>{artifacts.map((artifact) => <span key={artifact.id}>{artifact.filename}</span>)}</div>
  ),
}));
vi.mock("../components/work/WorkApproval", () => ({ WorkApproval: () => null }));

describe("WorkPage terminal event synchronization", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const state = useWorkStore.getState();
    state.resetWorkspace();
    state.setSessions([]);
    state.setConnections([]);
    state.setToolCatalog([]);

    apiMocks.getWorkSession.mockResolvedValue(workSession());
    apiMocks.getLatestWorkRun.mockResolvedValue(workRun());
    apiMocks.listWorkRuns.mockResolvedValue([workRun()]);
    apiMocks.createWorkSession.mockResolvedValue(
      workSession({ status: "idle", latest_run_status: null }),
    );
    apiMocks.startWorkRun.mockResolvedValue(workRun());
    apiMocks.sendWorkInstruction.mockResolvedValue(workRun());
    apiMocks.getWorkRun.mockResolvedValue(
      workRun({
        status: "completed",
        completed_at: "2026-08-24T23:00:13Z",
        stop_reason: "end_turn",
      }),
    );
    apiMocks.listWorkSessions.mockResolvedValue([workSession()]);
    apiMocks.listToolCatalog.mockResolvedValue([]);
    apiMocks.listToolConnections.mockResolvedValue([]);
    apiMocks.listWorkArtifacts.mockResolvedValue([]);
    apiMocks.getWorkEvents.mockImplementation(async (_runId: string, afterSequence = 0) => {
      if (afterSequence === 12) {
        return {
          items: [
            workEvent(19, "agent_message", "The complete response arrived before refresh."),
            workEvent(23, "run_completed", "Work completed"),
          ],
          latest_sequence: 23,
        };
      }
      return {
        items: [workEvent(11, "progress", "Work is running")],
        latest_sequence: 11,
      };
    });
    apiMocks.streamWorkEvents.mockImplementation(
      async (
        _runId: string,
        _afterSequence: number,
        onEvent: (event: WorkEvent) => Promise<boolean | void>,
      ) => {
        await onEvent(workEvent(12, "progress", "Usage updated"));
      },
    );
  });

  afterEach(() => {
    cleanup();
    useWorkStore.getState().resetWorkspace();
  });

  it("loads remaining events before stopping when the refreshed run is already complete", async () => {
    render(
      <MemoryRouter initialEntries={["/work/work-session-1"]}>
        <Routes>
          <Route path="/work/:workSessionId" element={<WorkPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("The complete response arrived before refresh."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Cortex completed the requested work.")).not.toBeInTheDocument();
    expect(apiMocks.getWorkEvents).toHaveBeenCalledWith("run-1", 12);
    await waitFor(() => {
      expect(useWorkStore.getState().events.map((event) => event.sequence)).toEqual([
        11, 12, 19, 23,
      ]);
    });
  });

  it("shows a starting workspace while the run-start request is pending", async () => {
    const pendingStart = deferred<WorkRun>();
    apiMocks.startWorkRun.mockReturnValue(pendingStart.promise);

    render(
      <MemoryRouter initialEntries={["/work"]}>
        <Routes>
          <Route path="/work" element={<WorkPage />} />
          <Route path="/work/:workSessionId" element={<WorkPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const goal = await screen.findByRole("textbox", { name: "Work goal" });
    fireEvent.change(goal, { target: { value: "Prepare a launch report" } });
    useWorkStore.getState().setWebMode("on");
    fireEvent.click(screen.getByRole("button", { name: "Start work" }));

    const starting = await screen.findByRole("status", { name: "Starting work" });
    expect(starting).toHaveTextContent("Starting work...");
    expect(screen.getByRole("heading", { name: "Prepare a launch report" })).toBeInTheDocument();
    expect(screen.getByText("Starting", { exact: true })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "What should I work on?" })).not.toBeInTheDocument();

    pendingStart.resolve(workRun({ instruction: "Prepare a launch report" }));
    await waitFor(() => {
      expect(screen.queryByRole("status", { name: "Starting work" })).not.toBeInTheDocument();
    });
    expect(apiMocks.startWorkRun).toHaveBeenCalledWith(
      "work-session-1",
      expect.objectContaining({
        instruction: "Prepare a launch report",
        max_credit_budget: 1_000_000,
        web_mode: "on",
      }),
      expect.stringMatching(/^work-ui-/),
    );
  });

  it("keeps earlier prompts, results, and deliverables visible after a follow-up", async () => {
    const original = workRun({
      id: "run-security",
      instruction: "Analyze the application security concerns",
      status: "completed",
      completed_at: "2026-08-24T22:04:00Z",
      created_at: "2026-08-24T22:00:04Z",
      updated_at: "2026-08-24T22:04:00Z",
    });
    const followup = workRun({
      id: "run-deliverables",
      instruction: "Where are the deliverables? I do not see them.",
      status: "completed",
      completed_at: "2026-08-24T23:04:00Z",
      created_at: "2026-08-24T23:00:04Z",
      updated_at: "2026-08-24T23:04:00Z",
    });
    apiMocks.getWorkSession.mockResolvedValue(
      workSession({ status: "idle", latest_run_status: "completed" }),
    );
    apiMocks.listWorkRuns.mockResolvedValue([original, followup]);
    apiMocks.getWorkEvents.mockImplementation(async (runId: string) => ({
      items: runId === original.id
        ? [workEvent(10, "agent_message", "Security review complete with six findings.")]
        : [workEvent(20, "agent_message", "The deliverables remain attached above.")],
      latest_sequence: runId === original.id ? 10 : 20,
    }));
    apiMocks.listWorkArtifacts.mockImplementation(async (runId: string) => (
      runId === original.id
        ? [workArtifact("security-report", "SECURITY_ANALYSIS_REPORT.md")]
        : []
    ));

    render(
      <MemoryRouter initialEntries={["/work/work-session-1"]}>
        <Routes>
          <Route path="/work/:workSessionId" element={<WorkPage />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText("Analyze the application security concerns")).toBeInTheDocument();
    expect(screen.getByText("Security review complete with six findings.")).toBeInTheDocument();
    expect(screen.getByText("SECURITY_ANALYSIS_REPORT.md")).toBeInTheDocument();
    expect(screen.getByText("Where are the deliverables? I do not see them.")).toBeInTheDocument();
    expect(screen.getByText("The deliverables remain attached above.")).toBeInTheDocument();
  });
});

function workSession(overrides: Partial<WorkSession> = {}): WorkSession {
  return {
    id: "work-session-1",
    session_id: "history-session-1",
    title: "Can you try again",
    status: "running",
    agent_provider: "fake",
    created_at: "2026-08-24T23:00:00Z",
    updated_at: "2026-08-24T23:00:00Z",
    latest_run_status: "running",
    ...overrides,
  };
}

function workRun(overrides: Partial<WorkRun> = {}): WorkRun {
  return {
    id: "run-1",
    work_session_id: "work-session-1",
    request_id: "request-1",
    instruction: "Can you try again",
    status: "running",
    provider: "fake",
    max_credit_budget: 1_000_000,
    max_output_tokens: 40_000,
    actual_output_tokens: 12_000,
    reserved_credits: 1_000_000,
    actual_credits: 39_651,
    provider_model_id: "claude-haiku-4-5",
    billing_model_id: "claude-haiku-4-5",
    billing_model_source: "fake_session_agent_snapshot",
    provider_agent_id: "fake-agent",
    provider_agent_version: 1,
    output_finalize_requested_at: null,
    output_limit_interrupt_requested_at: null,
    configuration_snapshot: { requested_web_mode: "auto", effective_web_enabled: false },
    usage_snapshot: {},
    stop_reason: null,
    error_code: null,
    error_message: null,
    started_at: "2026-08-24T23:00:04Z",
    completed_at: null,
    created_at: "2026-08-24T23:00:04Z",
    updated_at: "2026-08-24T23:00:12Z",
    ...overrides,
  };
}

function workEvent(sequence: number, type: string, displayMessage: string): WorkEvent {
  return {
    id: `event-${sequence}`,
    sequence,
    type,
    display_message: displayMessage,
    payload: {},
    created_at: "2026-08-24T23:00:13Z",
  };
}

function workArtifact(id: string, filename: string): WorkArtifact {
  return {
    id,
    file_id: `${id}-file`,
    role: "artifact",
    source: "agent",
    filename,
    mime_type: "text/markdown",
    size_bytes: 4096,
    artifact_type: "report",
    metadata: {},
    created_at: "2026-08-24T22:04:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}
