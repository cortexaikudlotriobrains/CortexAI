import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "../components/layout/Sidebar";
import { useChatStore } from "../store/chatStore";
import type { HistoryEntry, HistoryThread, WorkSession } from "../types";

describe("Sidebar", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    resetStore();
  });

  it("preserves mode navigation, active session, history search, and thread selection", async () => {
    const user = userEvent.setup();
    const onSelectThread = vi.fn<(thread: HistoryThread) => void>();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
      mode: "single",
    });

    render(<Sidebar onSelectThread={onSelectThread} />);

    expect(screen.getByRole("heading", { name: "CortexAI" })).toBeInTheDocument();
    expect(screen.getByText("Recent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Usage" })).not.toHaveAttribute("aria-current");
    const activeThread = screen.getByRole("button", { name: /Quarterly planning\. Ask,/ });
    expect(activeThread).toHaveAttribute("aria-current", "page");
    expect(activeThread).toHaveAccessibleName(/Quarterly planning\. Ask,/);
    expect(activeThread.querySelector("[data-history-title]")).toHaveTextContent(
      "Quarterly planning",
    );
    expect(activeThread).toHaveTextContent(/ASK ·/);
    expect(activeThread).not.toHaveTextContent("2 turns");
    expect(activeThread).not.toHaveTextContent("gpt-5.1");
    const timestamps = [...document.querySelectorAll("time")];
    expect(timestamps).toHaveLength(2);
    expect(timestamps.some((timestamp) => timestamp.dateTime === "2026-06-10T11:00:00Z")).toBe(
      true,
    );

    await user.click(screen.getByRole("button", { name: "Compare" }));
    expect(useChatStore.getState().mode).toBe("compare");

    await user.click(screen.getByRole("button", { name: "Filter chats" }));
    expect(screen.getByRole("textbox", { name: "Search chats" })).toHaveFocus();
    await user.type(screen.getByRole("textbox", { name: "Search chats" }), "vendors");
    expect(screen.queryByText("Quarterly planning")).not.toBeInTheDocument();
    const compareThread = screen.getByRole("button", { name: /Compare vendors\. Compare,/ });
    expect(compareThread).toBeInTheDocument();

    await user.click(compareThread);
    expect(onSelectThread).toHaveBeenCalledTimes(1);
    expect(onSelectThread.mock.calls[0]?.[0].sessionId).toBe("compare-session");
  });

  it("starts a new chat without changing sidebar navigation behavior", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
      mode: "compare",
    });

    render(<Sidebar onSelectThread={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "New chat" }));

    expect(useChatStore.getState().sessionId).toBeNull();
    expect(useChatStore.getState().pendingNewSession).toBe(true);
    expect(useChatStore.getState().mode).toBe("compare");
    expect(screen.getByText("Quarterly planning")).toBeInTheDocument();
  });

  it("collapses and expands the desktop sidebar while keeping icon actions usable", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
      mode: "single",
    });

    render(<Sidebar onSelectThread={vi.fn()} />);

    const sidebar = screen.getByLabelText("Primary navigation");
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByRole("textbox", { name: "Search chats" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(sidebar).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("textbox", { name: "Search chats" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Compare" }));
    expect(useChatStore.getState().mode).toBe("compare");

    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(useChatStore.getState().sessionId).toBeNull();

    await user.click(screen.getByRole("button", { name: "Expand sidebar" }));

    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByRole("textbox", { name: "Search chats" })).toBeInTheDocument();
  });

  it("marks Usage active and routes Ask or Compare back to chat", async () => {
    const user = userEvent.setup();
    const onNavigateChat = vi.fn<(mode: "single" | "compare") => void>();
    const onNavigateUsage = vi.fn();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
      mode: "single",
    });

    render(
      <Sidebar
        onSelectThread={vi.fn()}
        activeView="usage"
        onNavigateChat={onNavigateChat}
        onNavigateUsage={onNavigateUsage}
      />,
    );

    expect(screen.getByRole("button", { name: "Usage" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Ask" })).not.toHaveAttribute("aria-current");

    await user.click(screen.getByRole("button", { name: "Compare" }));

    expect(useChatStore.getState().mode).toBe("compare");
    expect(onNavigateChat).toHaveBeenCalledWith("compare");

    await user.click(screen.getByRole("button", { name: "Usage" }));
    expect(onNavigateUsage).toHaveBeenCalledTimes(1);
  });

  it("marks Models active and routes the sidebar Models item", async () => {
    const user = userEvent.setup();
    const onNavigateModels = vi.fn();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
      mode: "single",
    });

    render(
      <Sidebar onSelectThread={vi.fn()} activeView="models" onNavigateModels={onNavigateModels} />,
    );

    expect(screen.getByRole("button", { name: "Models" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Ask" })).not.toHaveAttribute("aria-current");

    await user.click(screen.getByRole("button", { name: "Models" }));
    expect(onNavigateModels).toHaveBeenCalledTimes(1);
  });

  it("marks AI credits active and routes the sidebar AI credits item", async () => {
    const user = userEvent.setup();
    const onNavigateCredits = vi.fn();

    render(
      <Sidebar
        onSelectThread={vi.fn()}
        activeView="credits"
        onNavigateCredits={onNavigateCredits}
      />,
    );

    expect(screen.getByRole("button", { name: "AI credits" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "Usage" })).not.toHaveAttribute("aria-current");

    await user.click(screen.getByRole("button", { name: "AI credits" }));
    expect(onNavigateCredits).toHaveBeenCalledTimes(1);
  });

  it("keeps the signed-in session footer as status instead of a sign-out action", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
    });

    render(<Sidebar onSelectThread={vi.fn()} loggedIn whoAmI={whoAmI()} />);

    expect(screen.queryByRole("button", { name: /Sign out/i })).not.toBeInTheDocument();
    expect(screen.getByText("Session active").closest("button")).toBeNull();
    await user.click(screen.getByText("Session active"));
  });

  it("keeps the sidebar footer sign-in action for signed-out users", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();

    render(<Sidebar onSelectThread={vi.fn()} loggedIn={false} onLogin={onLogin} />);

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it("shows a signed-out sidebar state without interactive workspace history", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
    });

    render(<Sidebar onSelectThread={vi.fn()} loggedIn={false} onLogin={onLogin} signedOut />);

    expect(screen.getByText("Sign in")).toBeInTheDocument();
    expect(screen.getByText("Access your workspace")).toBeInTheDocument();
    expect(screen.getByText("Sign in to view history.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Search chats" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New chat" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Compare" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it("deletes every persisted entry in a history thread", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      statusText: "No Content",
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
    });

    const onSelectThread = vi.fn();

    render(<Sidebar onSelectThread={onSelectThread} />);
    const activeThread = screen.getByRole("button", { name: /Quarterly planning\. Ask,/ });
    const activeRow = activeThread.closest("li");
    expect(activeRow).not.toBeNull();

    await user.click(
      within(activeRow!).getByRole("button", { name: /Chat options for Quarterly planning/ }),
    );
    const menu = within(activeRow!).getByRole("menu", { name: /Options for Quarterly planning/ });
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(2);
    await user.click(within(menu).getByRole("menuitem", { name: /Delete/ }));

    expect(onSelectThread).not.toHaveBeenCalled();
    expect(within(activeRow!).getByText("Delete?")).toBeInTheDocument();
    await user.click(within(activeRow!).getByRole("button", { name: "Confirm delete chat" }));

    await waitFor(() =>
      expect(useChatStore.getState().history.map((entry) => entry.id)).toEqual([3, 4]),
    );
    const deletedPaths = fetchMock.mock.calls.map((call) => call[0]);
    expect(deletedPaths).toContain("/v1/history/1");
    expect(deletedPaths).toContain("/v1/history/2");
    expect(deletedPaths).not.toContain("/v1/history/3");
    expect(deletedPaths).not.toContain("/v1/history/4");
    expect(useChatStore.getState().sessionId).toBeNull();
    expect(screen.queryByText("Quarterly planning")).toBeNull();
    expect(screen.getByText("Compare vendors")).toBeInTheDocument();
  });

  it("renames a persisted thread from the two-item context menu", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ session_id: "ask-session", title: "Launch plan" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    useChatStore.setState({
      history: historyEntries(),
      sessionId: "ask-session",
      pendingNewSession: false,
    });

    render(<Sidebar onSelectThread={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Chat options for Quarterly planning/ }));
    await user.click(screen.getByRole("menuitem", { name: /Rename/ }));
    const renameInput = screen.getByRole("textbox", { name: "Rename Quarterly planning" });
    await user.clear(renameInput);
    await user.type(renameInput, "Launch plan{Enter}");

    await waitFor(() => expect(screen.getByText("Launch plan")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/history/session/ask-session",
      expect.objectContaining({
        method: "PATCH",
        credentials: "include",
        body: JSON.stringify({ title: "Launch plan" }),
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      useChatStore.getState().history.filter((entry) => entry.session_id === "ask-session"),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ session_title: "Launch plan" }),
        expect.objectContaining({ session_title: "Launch plan" }),
      ]),
    );
  });

  it("supports row arrow navigation and R, D, and Escape shortcuts", async () => {
    const user = userEvent.setup();
    const onSelectThread = vi.fn();
    useChatStore.setState({ history: historyEntries(), sessionId: "ask-session" });

    render(<Sidebar onSelectThread={onSelectThread} />);
    const rows = [...document.querySelectorAll<HTMLButtonElement>("button[data-history-thread]")];
    expect(rows).toHaveLength(2);
    rows[0]!.focus();
    await user.keyboard("{ArrowDown}");
    expect(rows[1]).toHaveFocus();
    const focusedThreadKey = rows[1]!.dataset.historyThread;
    expect(focusedThreadKey).toBeTruthy();
    await user.keyboard("{Enter}");
    expect(onSelectThread).toHaveBeenCalledTimes(1);

    await user.keyboard("r");
    expect(screen.getByRole("textbox", { name: /Rename/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("textbox", { name: /Rename/ })).toBeNull();
    await waitFor(() => {
      expect(
        document.querySelector<HTMLButtonElement>(
          `button[data-history-thread="${focusedThreadKey}"]`,
        ),
      ).toHaveFocus();
    });

    await user.keyboard("d");
    expect(screen.getByText("Delete?")).toBeInTheDocument();
  });

  it("groups recent chats under Today, Yesterday, and month-day labels", () => {
    const now = new Date();
    const yesterday = new Date(now);
    yesterday.setDate(now.getDate() - 1);
    const older = new Date(now);
    older.setDate(now.getDate() - 8);
    useChatStore.setState({
      history: [
        historyEntry({
          id: 10,
          sessionId: "today-session",
          prompt: "Today chat",
          response: "Today response",
          mode: "single",
          provider: "openai",
          model: "gpt-5.1",
          timestamp: now.toISOString(),
        }),
        historyEntry({
          id: 11,
          sessionId: "yesterday-session",
          prompt: "Yesterday chat",
          response: "Yesterday response",
          mode: "single",
          provider: "openai",
          model: "gpt-5.1",
          timestamp: yesterday.toISOString(),
        }),
        historyEntry({
          id: 12,
          sessionId: "older-session",
          prompt: "Older chat",
          response: "Older response",
          mode: "compare",
          provider: "openai",
          model: "gpt-5.1",
          timestamp: older.toISOString(),
        }),
      ],
    });

    render(<Sidebar onSelectThread={vi.fn()} />);

    expect(screen.getByText("Today")).toBeInTheDocument();
    expect(screen.getByText("Yesterday")).toBeInTheDocument();
    expect(
      screen.getByText(older.toLocaleDateString(undefined, { month: "short", day: "numeric" })),
    ).toBeInTheDocument();
  });

  it("shows up to 100 recent chat threads", () => {
    useChatStore.setState({
      history: Array.from({ length: 101 }, (_, index) =>
        historyEntry({
          id: index + 1,
          sessionId: `history-session-${index + 1}`,
          prompt: `History chat ${index + 1}`,
          response: `History response ${index + 1}`,
          mode: "single",
          provider: "openai",
          model: "gpt-5.1",
          timestamp: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(),
        }),
      ),
    });

    render(<Sidebar onSelectThread={vi.fn()} />);

    expect(document.querySelectorAll("button[data-history-thread]")).toHaveLength(100);
    expect(screen.getByText("History chat 101")).toBeInTheDocument();
    expect(screen.queryByText("History chat 1", { exact: true })).not.toBeInTheDocument();
  });

  it("shows completed Work history once and hides zero-run session shells", () => {
    const onSelectWorkSession = vi.fn();
    const title = "Prepare the regression strategy";
    const workSessions: WorkSession[] = [
      workSession({ id: "empty-1", session_id: "history-empty-1", title }),
      workSession({ id: "empty-2", session_id: "history-empty-2", title }),
      workSession({
        id: "completed-1",
        session_id: "history-completed-1",
        title,
        status: "completed",
        latest_run_status: "completed",
      }),
    ];

    render(
      <Sidebar
        onSelectThread={vi.fn()}
        workSessions={workSessions}
        onSelectWorkSession={onSelectWorkSession}
      />,
    );

    const entries = screen.getAllByRole("button", { name: `${title}. Work, completed` });
    expect(entries).toHaveLength(1);
  });
});

function workSession(overrides: Partial<WorkSession> = {}): WorkSession {
  return {
    id: "work-session",
    session_id: "history-session",
    title: "Work task",
    status: "idle",
    agent_provider: "fake",
    created_at: "2026-08-20T00:00:00Z",
    updated_at: "2026-08-20T00:00:00Z",
    latest_run_status: null,
    ...overrides,
  };
}

function historyEntries(): HistoryEntry[] {
  return [
    historyEntry({
      id: 1,
      sessionId: "ask-session",
      prompt: "Quarterly planning",
      response: "Planning response",
      mode: "single",
      provider: "openai",
      model: "gpt-5.1",
      timestamp: "2026-06-10T10:00:00Z",
    }),
    historyEntry({
      id: 2,
      sessionId: "ask-session",
      prompt: "Add milestones",
      response: "Milestone response",
      mode: "single",
      provider: "openai",
      model: "gpt-5.1",
      timestamp: "2026-06-10T10:01:00Z",
    }),
    historyEntry({
      id: 3,
      sessionId: "compare-session",
      prompt: "Compare vendors",
      response: "OpenAI comparison",
      mode: "compare",
      provider: "openai",
      model: "gpt-5.1",
      requestGroupId: "compare-group",
      timestamp: "2026-06-10T11:00:00Z",
    }),
    historyEntry({
      id: 4,
      sessionId: "compare-session",
      prompt: "Compare vendors",
      response: "Claude comparison",
      mode: "compare",
      provider: "claude",
      model: "claude-sonnet-4-5",
      requestGroupId: "compare-group",
      timestamp: "2026-06-10T11:00:00Z",
    }),
  ];
}

function historyEntry({
  id,
  sessionId,
  prompt,
  response,
  mode,
  provider,
  model,
  timestamp,
  requestGroupId,
}: {
  id: number;
  sessionId: string;
  prompt: string;
  response: string;
  mode: string;
  provider: string;
  model: string;
  timestamp: string;
  requestGroupId?: string;
}): HistoryEntry {
  return {
    id,
    session_id: sessionId,
    request_group_id: requestGroupId,
    timestamp,
    mode,
    prompt,
    provider,
    model,
    response,
    latency_ms: 300,
    tokens: 40,
    cost: 0.001,
    web_source_items: [],
  };
}

function whoAmI() {
  return {
    api_key_id: "test-key",
    user_id: "signed-in-user",
    plan_tier: "Pro",
    storage_policy: "full",
    redact_pii: false,
    baseline: {
      provider: "openai",
      model: "gpt-5.1",
      source: "test",
    },
    rate_limits: {
      requests_per_minute: 60,
      daily_cap_scope: "user",
    },
    breakers: {
      failure_threshold: 5,
      window_seconds: 60,
      cooldown_seconds: 120,
      scope: "provider_model",
    },
  };
}

function resetStore() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
}
