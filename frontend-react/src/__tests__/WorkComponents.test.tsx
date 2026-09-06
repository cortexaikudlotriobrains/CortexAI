import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkApproval } from "../components/work/WorkApproval";
import { WorkArtifacts } from "../components/work/WorkArtifacts";
import { WorkComposer } from "../components/work/WorkComposer";
import { WorkRail } from "../components/work/WorkRail";
import { WorkStatusPill } from "../components/work/WorkStatusPill";
import { useWorkStore } from "../store/workStore";
import type { AttachmentUploadTask } from "../store/attachmentUploadStore";
import type {
  WorkApproval as Approval,
  WorkArtifact,
  WorkEvent,
  WorkRun,
  WorkSession,
} from "../types";

afterEach(cleanup);

beforeEach(() => {
  useWorkStore.getState().resetWorkspace();
  useWorkStore.getState().setSessions([]);
  useWorkStore.getState().setConnections([]);
  useWorkStore.getState().setToolCatalog([]);
});

describe("Cortex Work components", () => {
  it("starts substantive Work tasks with a one-dollar Pro budget ceiling", () => {
    expect(useWorkStore.getState().maxCreditBudget).toBe(1_000_000);
  });

  it("retains a newly created session so a rejected run can retry without another history row", async () => {
    const session = workSession();
    const createSession = vi.fn().mockResolvedValue(session);

    const first = await useWorkStore.getState().ensureSession(createSession);
    const retry = await useWorkStore.getState().ensureSession(createSession);

    expect(first).toBe(session);
    expect(retry).toBe(session);
    expect(useWorkStore.getState().session).toBe(session);
    expect(createSession).toHaveBeenCalledTimes(1);
  });

  it("maps durable states to user-facing status labels", () => {
    const { rerender } = render(<WorkStatusPill status="waiting_for_approval" />);
    expect(screen.getByText("Needs approval")).toBeInTheDocument();
    rerender(<WorkStatusPill status="budget_exhausted" />);
    expect(screen.getByText("Budget reached")).toBeInTheDocument();
  });

  it("passes an explicit remembered WRITE grant to approval handling", async () => {
    const user = userEvent.setup();
    const onApprove = vi.fn();
    render(
      <WorkApproval
        approval={approval({ action_type: "WRITE", connection_id: "connection-1" })}
        onApprove={onApprove}
        onDeny={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onApprove).toHaveBeenCalledWith(true);
  });

  it("does not offer persistent grants for sensitive approval classes", () => {
    render(
      <WorkApproval
        approval={approval({ action_type: "DESTRUCTIVE", connection_id: "connection-1" })}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
      />,
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Cortex wants to use Delete Repository/ }),
    ).toBeInTheDocument();
  });

  it("renders authenticated open and download artifact links", () => {
    const artifact: WorkArtifact = {
      id: "artifact-1",
      file_id: "file-1",
      role: "artifact",
      source: "agent",
      filename: "analysis.pdf",
      mime_type: "application/pdf",
      size_bytes: 2_048,
      artifact_type: "report",
      metadata: {},
      created_at: "2026-08-20T00:00:00Z",
    };
    render(<WorkArtifacts runId="run-1" artifacts={[artifact]} />);
    expect(screen.getByRole("link", { name: "Open" })).toHaveAttribute(
      "href",
      "/v1/work/runs/run-1/artifacts/file-1/download?inline=1",
    );
    expect(screen.getByRole("link", { name: "Download analysis.pdf" })).toHaveAttribute(
      "href",
      "/v1/work/runs/run-1/artifacts/file-1/download",
    );
  });

  it("shows real plan activity and credit progress in the Work rail", async () => {
    const user = userEvent.setup();
    render(
      <WorkRail
        run={run()}
        events={[event(1, "plan_created", "Plan created"), event(2, "progress", "Reading files")]}
        connections={[]}
        enabledConnectionIds={[]}
      />,
    );
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
    expect(screen.getByText("Reading files")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Work credit usage" })).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
    expect(screen.getByText(/Provider model · Claude Haiku 4.5/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Tools/ }));
    expect(screen.getByText("No connected tools")).toBeInTheDocument();
  });

  it("settles every activity marker and plan step when Work is completed", () => {
    const { container } = render(
      <WorkRail
        run={run({ status: "completed", completed_at: "2026-08-20T00:01:00Z" })}
        events={[
          event(1, "planning", "Creating a plan"),
          event(2, "progress", null),
          event(3, "progress", "Usage updated"),
          event(4, "run_completed", "Work completed"),
        ]}
        connections={[]}
        enabledConnectionIds={[]}
      />,
    );

    expect(screen.getByText("3 of 3")).toBeInTheDocument();
    expect(container.querySelector('[data-activity-state="active"]')).toBeNull();
    expect(container.querySelectorAll('[data-activity-state="done"]')).toHaveLength(3);
    expect(screen.queryByText("progress")).not.toBeInTheDocument();
  });

  it("spins only the newest visible activity while Work is running", () => {
    const { container } = render(
      <WorkRail
        run={run()}
        events={[
          event(1, "planning", "Creating a plan"),
          event(2, "progress", "Reading files"),
          event(3, "progress", null),
          event(4, "progress", "Writing the report"),
        ]}
        connections={[]}
        enabledConnectionIds={[]}
      />,
    );

    const activeMarkers = container.querySelectorAll('[data-activity-state="active"]');
    expect(activeMarkers).toHaveLength(1);
    expect(activeMarkers[0]?.closest("li")).toHaveTextContent("Writing the report");
    expect(screen.queryByText(/^progress$/)).not.toBeInTheDocument();
  });

  it("submits a goal with Enter and toggles web access", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onWebModeChange = vi.fn();
    render(
      <WorkComposer {...composerProps({ value: "Build the report", onSubmit, onWebModeChange })} />,
    );
    await user.type(screen.getByRole("textbox", { name: "Work goal" }), "{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: /Web access: Auto/ }));
    expect(onWebModeChange).toHaveBeenCalledWith("on");
  });

  it("presents scaled Work budgets while preserving raw budget selections", async () => {
    const user = userEvent.setup();
    const onBudgetChange = vi.fn();
    render(
      <WorkComposer
        {...composerProps({ maxPlanBudget: 1_000_000, onBudgetChange })}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Work settings" }));
    expect(screen.getByRole("button", { name: "25 credits" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "100 credits" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "250 credits" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1,000 credits" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "250 credits" }));
    expect(onBudgetChange).toHaveBeenCalledWith(250_000);
  });

  it("warns when Web is explicitly off for a current-information request", () => {
    render(
      <WorkComposer
        {...composerProps({
          value: "Build an itinerary with current opening hours and ticket prices",
          webMode: "off",
        })}
      />,
    );

    expect(
      screen.getByText("This request appears to need current information, but Web is explicitly off."),
    ).toBeInTheDocument();
  });

  it("blocks submission until every attachment is ready", () => {
    const uploading: AttachmentUploadTask = {
      clientId: "upload-1",
      file: new File(["x"], "source.csv", { type: "text/csv" }),
      filename: "source.csv",
      mimeType: "text/csv",
      sizeBytes: 1,
      state: "uploading",
      progress: 50,
      retryCount: 0,
      uploadMode: "legacy",
    };
    render(<WorkComposer {...composerProps({ value: "Build the report", tasks: [uploading] })} />);
    expect(screen.getByRole("button", { name: /Start work/ })).toBeDisabled();
    expect(screen.getByText("50%")).toBeInTheDocument();
  });

  it("deduplicates replayed events and clears only Work workspace state", () => {
    const state = useWorkStore.getState();
    state.setSessions([{ id: "session-1" } as never]);
    state.appendEvent(event(2, "progress", "newer"));
    state.appendEvent(event(1, "planning", "older"));
    state.appendEvent(event(2, "progress", "replacement"));
    expect(useWorkStore.getState().events.map((item) => item.display_message)).toEqual([
      "older",
      "replacement",
    ]);
    useWorkStore.getState().resetWorkspace();
    expect(useWorkStore.getState().events).toEqual([]);
    expect(useWorkStore.getState().sessions).toHaveLength(1);
  });
});

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "approval-1",
    work_run_id: "run-1",
    tool_call_id: "call-1",
    connection_id: null,
    action_type: "WRITE",
    tool_name: "delete_repository",
    description: "Cortex needs permission to continue.",
    request_payload: { repository: "example/repo" },
    status: "pending",
    requested_at: "2026-08-20T00:00:00Z",
    decided_at: null,
    ...overrides,
  };
}

function run(overrides: Partial<WorkRun> = {}): WorkRun {
  return {
    id: "run-1",
    work_session_id: "session-1",
    request_id: "request-1",
    instruction: "Build the report",
    status: "running",
    provider: "fake",
    max_credit_budget: 100_000,
    max_output_tokens: 40_000,
    actual_output_tokens: 12_000,
    reserved_credits: 100_000,
    actual_credits: 25_000,
    provider_model_id: "claude-haiku-4-5",
    billing_model_id: "claude-haiku-4-5",
    billing_model_source: "fake_session_agent_snapshot",
    provider_agent_id: "fake-agent",
    provider_agent_version: 1,
    output_finalize_requested_at: null,
    output_limit_interrupt_requested_at: null,
    configuration_snapshot: { web_enabled: false },
    usage_snapshot: {},
    stop_reason: null,
    error_code: null,
    error_message: null,
    started_at: "2026-08-20T00:00:00Z",
    completed_at: null,
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
    ...overrides,
  };
}

function workSession(): WorkSession {
  return {
    id: "session-1",
    session_id: "history-session-1",
    title: "Build the report",
    status: "idle",
    agent_provider: "fake",
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
    latest_run_status: null,
  };
}

function event(sequence: number, type: string, displayMessage: string | null): WorkEvent {
  return {
    id: `event-${sequence}`,
    sequence,
    type,
    display_message: displayMessage,
    payload: {},
    created_at: "2026-08-20T00:00:00Z",
  };
}

function composerProps(overrides: Record<string, unknown> = {}) {
  return {
    value: "",
    onChange: vi.fn(),
    onSubmit: vi.fn(),
    onFiles: vi.fn(),
    onRemoveFile: vi.fn(),
    onRetryFile: vi.fn(),
    tasks: [],
    connections: [],
    catalog: [],
    enabledConnectionIds: [],
    onToggleConnection: vi.fn(),
    onConnect: vi.fn(),
    onAddMcp: vi.fn().mockResolvedValue(undefined),
    webMode: "auto" as const,
    onWebModeChange: vi.fn(),
    maxCreditBudget: 1_000_000,
    maxPlanBudget: 250_000,
    onBudgetChange: vi.fn(),
    ...overrides,
  };
}
