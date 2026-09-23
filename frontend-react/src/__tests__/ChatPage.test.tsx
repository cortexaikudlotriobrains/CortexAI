import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPage } from "../pages/ChatPage";
import { useChatStore } from "../store/chatStore";
import type { CognitoConfig, WhoAmIResponse } from "../types";

interface MockAuthState {
  whoAmI: WhoAmIResponse | null;
  cognitoConfig: CognitoConfig | null;
  loading: boolean;
  loggedIn: boolean;
}

const mocks = vi.hoisted(() => ({
  authState: {
    whoAmI: null,
    cognitoConfig: { enabled: true, domain: "https://auth.example.com", clientId: "client-123" },
    loading: false,
    loggedIn: false,
  } satisfies MockAuthState,
  login: vi.fn(),
  logout: vi.fn(),
  loadHistory: vi.fn(),
  removeThread: vi.fn(),
  submit: vi.fn(),
  regenerate: vi.fn(),
  cancel: vi.fn(),
  reloadSubscription: vi.fn(),
  useModels: vi.fn(),
  subscriptionEntitlements: {
    current: null as null | {
      plan: {
        code: "free" | "plus" | "pro";
        display_name: string;
        status: "free" | "active" | "past_due";
      };
    },
  },
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    ...mocks.authState,
    login: mocks.login,
    logout: mocks.logout,
    refresh: vi.fn(),
  }),
}));

vi.mock("../hooks/useModels", () => ({
  useModels: mocks.useModels,
}));

vi.mock("../hooks/useSubscription", () => ({
  useSubscription: () => ({
    entitlements: mocks.subscriptionEntitlements.current,
    reload: mocks.reloadSubscription,
  }),
}));

vi.mock("../hooks/useHistory", () => ({
  useHistory: () => ({
    load: mocks.loadHistory,
    removeThread: mocks.removeThread,
    clear: vi.fn(),
  }),
}));

vi.mock("../hooks/useChat", () => ({
  useChat: () => ({
    submit: mocks.submit,
    cancel: mocks.cancel,
    regenerate: mocks.regenerate,
  }),
}));

vi.mock("../components/composer/PromptComposer", () => ({
  PromptComposer: () => <div data-testid="prompt-composer" />,
}));

vi.mock("../components/results/ResultsSection", () => ({
  ResultsSection: () => <div data-testid="results-section" />,
}));

describe("ChatPage authentication gate", () => {
  beforeEach(() => {
    Object.assign(mocks.authState, {
      whoAmI: null,
      cognitoConfig: {
        enabled: true,
        domain: "https://auth.example.com",
        clientId: "client-123",
      },
      loading: false,
      loggedIn: false,
    });
    mocks.useModels.mockReturnValue({ models: [], loading: false, error: null });
    mocks.subscriptionEntitlements.current = null;
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetStore();
    delete (
      window as unknown as { CORTEX_RUNTIME_CONFIG?: Record<string, unknown> }
    ).CORTEX_RUNTIME_CONFIG;
  });

  it("shows a sign-in gate instead of the workspace for signed-out Cognito users", async () => {
    const user = userEvent.setup();

    renderChatPage();

    const signInRegion = screen.getByRole("region", { name: "Sign in to use CortexAI" });
    expect(signInRegion).toBeInTheDocument();
    expect(signInRegion).toHaveTextContent(
      "Access your AI workspace, saved chats, model comparison, and file analysis.",
    );
    expect(screen.queryByText(/Backend not connected/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/port 8000/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId("prompt-composer")).not.toBeInTheDocument();
    expect(screen.getByText("Sign in to view history.")).toBeInTheDocument();
    expect(mocks.useModels).toHaveBeenCalledWith(false);
    expect(mocks.loadHistory).not.toHaveBeenCalled();

    await user.click(within(signInRegion).getByRole("button", { name: "Sign in" }));

    expect(mocks.login).toHaveBeenCalledTimes(1);
  });

  it("renders the workspace and startup fetches once the session is authenticated", async () => {
    const user = userEvent.setup();
    Object.assign(mocks.authState, {
      whoAmI: whoAmI(),
      cognitoConfig: {
        enabled: true,
        domain: "https://auth.example.com",
        clientId: "client-123",
      },
      loading: false,
      loggedIn: true,
    });
    mocks.subscriptionEntitlements.current = {
      plan: { code: "plus", display_name: "Plus", status: "active" },
    };
    renderChatPage();

    expect(screen.queryByRole("region", { name: "Sign in to use CortexAI" })).not.toBeInTheDocument();
    const composer = screen.getByTestId("prompt-composer");
    expect(composer).toBeInTheDocument();
    expect(composer.parentElement).toHaveAttribute("data-composer-placement", "center");
    expect(screen.getByText("Hi, how can I help?")).toBeInTheDocument();
    expect(mocks.useModels).toHaveBeenCalledWith(true);
    await waitFor(() => {
      expect(mocks.loadHistory).toHaveBeenCalledWith({ restoreActiveTranscript: true });
    });

    await user.click(screen.getAllByRole("button", { name: "Account" })[0]);
    expect(
      screen.getByRole("menuitem", { name: "Plus plan, Manage subscription" }),
    ).toBeInTheDocument();

    act(() => useChatStore.getState().setMode("compare"));
    expect(screen.getByText("Hi, what would you like to compare?")).toBeInTheDocument();
    expect(screen.queryByText("Hi, how can I help?")).not.toBeInTheDocument();

    act(() => {
      useChatStore.getState().beginTurn({
        mode: "compare",
        prompt: "Explain this",
        submittedPrompt: "Explain this",
        attachments: [],
        responses: [],
        status: "streaming",
      });
    });
    expect(composer.parentElement).toHaveAttribute("data-composer-placement", "bottom");
    expect(screen.queryByText("Hi, what would you like to compare?")).not.toBeInTheDocument();
  });

  it("retries the latest failed turn through regeneration", async () => {
    Object.assign(mocks.authState, {
      whoAmI: whoAmI(),
      cognitoConfig: { enabled: false },
      loading: false,
      loggedIn: true,
    });
    const turnId = useChatStore.getState().beginTurn({
      mode: "single",
      prompt: "Retry this prompt",
      submittedPrompt: "Retry this prompt",
      attachments: [],
      responses: [],
      status: "error",
    });
    useChatStore.getState().setError("Temporary upstream outage");
    const user = userEvent.setup();

    renderChatPage();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(mocks.regenerate).toHaveBeenCalledWith(turnId);
    expect(mocks.submit).not.toHaveBeenCalled();
  });

  it.each([
    ["Ask", "single"],
    ["Compare", "compare"],
  ] as const)("reloads the AI-credit balance when a %s stream settles", async (_label, mode) => {
    Object.assign(mocks.authState, {
      whoAmI: whoAmI(),
      cognitoConfig: { enabled: false },
      loading: false,
      loggedIn: true,
    });
    useChatStore.getState().setMode(mode);

    renderChatPage();

    expect(mocks.reloadSubscription).not.toHaveBeenCalled();
    act(() => useChatStore.getState().setStreaming(true));
    expect(mocks.reloadSubscription).not.toHaveBeenCalled();

    act(() => useChatStore.getState().setStreaming(false));

    await waitFor(() => {
      expect(mocks.reloadSubscription).toHaveBeenCalledTimes(1);
    });
  });
});

function renderChatPage() {
  render(
    <BrowserRouter>
      <ChatPage />
    </BrowserRouter>,
  );
}

function resetStore() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
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
