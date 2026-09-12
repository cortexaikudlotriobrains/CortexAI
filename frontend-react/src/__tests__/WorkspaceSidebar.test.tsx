import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceSidebar } from "../components/layout/WorkspaceSidebar";
import { useChatStore } from "../store/chatStore";
import { useSidebarStore } from "../store/sidebarStore";
import { useWorkStore } from "../store/workStore";
import type { HistoryEntry, WorkSession } from "../types";

const mocks = vi.hoisted(() => ({
  cancel: vi.fn(),
  fetchAnalysisRuns: vi.fn(),
  fetchHistory: vi.fn(),
  listWorkSessions: vi.fn(),
  loadHistory: vi.fn(),
  removeThread: vi.fn(),
  renameThread: vi.fn(),
}));

vi.mock("../api/cortexAnalysis", () => ({
  fetchCortexAnalysisRuns: mocks.fetchAnalysisRuns,
}));

vi.mock("../api/history", () => ({
  fetchHistory: mocks.fetchHistory,
}));

vi.mock("../api/work", () => ({
  listWorkSessions: mocks.listWorkSessions,
}));

vi.mock("../config/runtimeConfig", () => ({
  getRuntimeConfig: () => ({ workEnabled: true }),
}));

vi.mock("../hooks/useChat", () => ({
  useChat: () => ({ cancel: mocks.cancel }),
}));

vi.mock("../hooks/useHistory", () => ({
  useHistory: () => ({
    load: mocks.loadHistory,
    removeThread: mocks.removeThread,
    renameThread: mocks.renameThread,
  }),
}));

describe("WorkspaceSidebar", () => {
  beforeEach(() => {
    mocks.loadHistory.mockResolvedValue([]);
    mocks.listWorkSessions.mockResolvedValue([workSession()]);
    mocks.fetchHistory.mockResolvedValue(historyEntries());
    mocks.fetchAnalysisRuns.mockResolvedValue([]);
    resetStores();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetStores();
  });

  it("returns secondary routes to the remembered Work session", async () => {
    const user = userEvent.setup();
    useWorkStore.getState().setSession({
      ...workSession(),
      status: "running",
      latest_run_status: "running",
    });
    renderSidebar("credits", "/credits");

    expect(
      await screen.findByRole("button", { name: "Prepare launch report. Work, completed" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Work$/ })).toBeInTheDocument();
    expect(mocks.loadHistory).toHaveBeenCalledWith({ restoreActiveTranscript: false });

    await user.click(screen.getByRole("button", { name: /^Work$/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/work/work-session-1");
  });

  it("turns New chat on a secondary route into a visible Ask navigation", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      mode: "compare",
      sessionId: "compare-session",
      pendingNewSession: false,
    });
    renderSidebar("credits", "/credits");

    await user.click(screen.getByRole("button", { name: "New chat" }));

    expect(mocks.cancel).toHaveBeenCalledTimes(1);
    expect(useChatStore.getState().mode).toBe("single");
    expect(useChatStore.getState().sessionId).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/");
  });

  it("restores chat history from Work and navigates to the hydrated transcript", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ history: historyEntries(), sessionId: null });
    renderSidebar("work", "/work", []);

    await user.click(screen.getByRole("button", { name: /Quarterly planning\. Ask,/ }));

    await waitFor(() => {
      expect(mocks.fetchHistory).toHaveBeenCalledWith(500, "ask-session");
      expect(mocks.fetchAnalysisRuns).toHaveBeenCalledWith({ sessionId: "ask-session" });
      expect(useChatStore.getState().sessionId).toBe("ask-session");
      expect(screen.getByTestId("location")).toHaveTextContent("/");
    });
  });

  it("applies the signed-out history gate consistently outside Chat", () => {
    render(
      <MemoryRouter initialEntries={["/credits"]}>
        <WorkspaceSidebar activeView="credits" authLoading={false} authEnabled loggedIn={false} />
      </MemoryRouter>,
    );

    expect(screen.getByText("Sign in to view history.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(mocks.loadHistory).not.toHaveBeenCalled();
    expect(mocks.listWorkSessions).not.toHaveBeenCalled();
  });
});

function renderSidebar(
  activeView: "chat" | "work" | "usage" | "credits" | "models" | "account",
  initialPath: string,
  workSessions?: WorkSession[],
) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <WorkspaceSidebar
        activeView={activeView}
        authLoading={false}
        authEnabled={false}
        loggedIn={false}
        workSessions={workSessions}
      />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function workSession(): WorkSession {
  return {
    id: "work-session-1",
    session_id: "work-history-session-1",
    title: "Prepare launch report",
    status: "completed",
    agent_provider: "fake",
    created_at: "2026-09-01T12:00:00Z",
    updated_at: "2026-09-01T12:05:00Z",
    latest_run_status: "completed",
  };
}

function historyEntries(): HistoryEntry[] {
  return [
    {
      id: 1,
      session_id: "ask-session",
      timestamp: "2026-09-01T11:00:00Z",
      mode: "single",
      prompt: "Quarterly planning",
      provider: "openai",
      model: "gpt-5.6-luna",
      response: "Planning response",
      latency_ms: 300,
      tokens: 40,
      cost: 0.001,
      web_source_items: [],
    },
  ];
}

function resetStores() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
  useSidebarStore.getState().setCollapsed(false);
  useWorkStore.getState().resetWorkspace();
}
