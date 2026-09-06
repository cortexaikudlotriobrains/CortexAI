import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CreditsPage } from "../pages/CreditsPage";
import { useChatStore } from "../store/chatStore";
import type { CreditTransaction, EntitlementsResponse } from "../types";

const hookMocks = vi.hoisted(() => ({
  fetchCreditTransactions: vi.fn(),
  loadHistory: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  reloadSubscription: vi.fn(),
}));

vi.mock("../api/entitlements", () => ({
  fetchCreditTransactions: hookMocks.fetchCreditTransactions,
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    whoAmI: null,
    cognitoConfig: { enabled: true },
    loading: false,
    loggedIn: true,
    login: hookMocks.login,
    logout: hookMocks.logout,
  }),
}));

vi.mock("../hooks/useHistory", () => ({
  useHistory: () => ({ load: hookMocks.loadHistory }),
}));

vi.mock("../hooks/useSubscription", () => ({
  useSubscription: () => ({
    entitlements: entitlementFixture(),
    loading: false,
    error: null,
    lastLoadedAt: 1,
    reload: hookMocks.reloadSubscription,
  }),
}));

vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "light", toggleTheme: vi.fn() }),
}));

describe("CreditsPage", () => {
  beforeEach(() => {
    hookMocks.fetchCreditTransactions.mockResolvedValue({
      items: transactionFixture(),
      limit: 20,
      offset: 0,
    });
    resetStore();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetStore();
  });

  it("shows the unified balance and itemized charge history", async () => {
    renderPage();

    expect(
      screen.getByRole("heading", { name: "See what each question cost." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "AI credits" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("heading", { name: "AI credit balance" })).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "AI credits: 90 left of 100" }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Final optimized AI answer + Web Search")).toBeInTheDocument();
    });
    const question = screen.getByText("How do atomic credit reservations work?");
    const total = screen.getByText("13 credits");
    expect(
      question.compareDocumentPosition(total) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const activityRegion = screen.getByRole("region", { name: "Recent activity" });
    expect(within(activityRegion).getAllByRole("article")).toHaveLength(1);

    fireEvent.click(screen.getByText("View credit breakdown"));
    const breakdownItems = within(activityRegion).getAllByRole("listitem");
    expect(breakdownItems).toHaveLength(2);
    expect(within(breakdownItems[0]).getByText("Final optimized AI answer")).toBeInTheDocument();
    expect(within(breakdownItems[0]).getByText("3 credits")).toBeInTheDocument();
    expect(
      within(breakdownItems[0]).getByText(
        "Includes Prompt Optimizer (1 optimizer attempt) and final answer · GPT-5.4 Mini · 1.5 question processing + 1.5 answer generation",
      ),
    ).toBeInTheDocument();
    expect(within(breakdownItems[1]).getByText("Web Search")).toBeInTheDocument();
    expect(
      within(breakdownItems[1]).getByText("2 search credits × 5 AI credits each"),
    ).toBeInTheDocument();
    expect(hookMocks.fetchCreditTransactions).toHaveBeenCalledWith(
      100,
      0,
      expect.any(AbortSignal),
    );
  });

  it("explains why older ledger rows have no question", async () => {
    hookMocks.fetchCreditTransactions.mockResolvedValue({
      items: [{ ...transactionFixture()[0], query: null }],
      limit: 20,
      offset: 0,
    });

    renderPage();

    expect(
      await screen.findByText(
        "Question unavailable for activity recorded before query tracking.",
      ),
    ).toBeInTheDocument();
  });

  it("combines Prompt Optimizer attempts into one understandable charge", async () => {
    const base = transactionFixture()[0];
    hookMocks.fetchCreditTransactions.mockResolvedValue({
      items: [
        {
          ...base,
          id: "optimizer-1",
          request_id: "optimizer-request",
          query: "Make this question clearer",
          operation_type: "optimize",
          input_credits: 300,
          output_credits: 200,
          total_credits: 500,
        },
        {
          ...base,
          id: "optimizer-2",
          request_id: "optimizer-request",
          query: "Make this question clearer",
          operation_type: "optimize",
          input_credits: 350,
          output_credits: 250,
          total_credits: 600,
        },
      ],
      limit: 100,
      offset: 0,
    });

    renderPage();

    const activity = await screen.findByRole("article", {
      name: "Credit activity for Make this question clearer",
    });
    expect(within(activity).getAllByText("1.1 credits")).toHaveLength(2);
    expect(within(activity).getAllByText("Prompt Optimizer")).toHaveLength(2);
    expect(within(activity).getByText("1 charge")).toBeInTheDocument();
    fireEvent.click(within(activity).getByText("View credit breakdown"));
    expect(
      screen.getByText(
        "GPT-5.4 Mini · 2 attempts · 0.65 question processing + 0.45 answer generation",
      ),
    ).toBeInTheDocument();
  });

  it("folds Prompt Optimizer and Compare models into one final optimized answer", async () => {
    const base = transactionFixture()[0];
    hookMocks.fetchCreditTransactions.mockResolvedValue({
      items: [
        {
          ...base,
          id: "optimizer-compare",
          operation_type: "optimize",
          input_credits: 200,
          output_credits: 300,
          total_credits: 500,
        },
        {
          ...base,
          id: "compare-1",
          operation_type: "compare",
          input_credits: 500,
          output_credits: 500,
          total_credits: 1_000,
        },
        {
          ...base,
          id: "compare-2",
          operation_type: "compare",
          provider: "anthropic",
          model: "claude-sonnet-4-5",
          input_credits: 700,
          output_credits: 800,
          total_credits: 1_500,
        },
      ],
      limit: 100,
      offset: 0,
    });

    renderPage();

    const activity = await screen.findByRole("article", {
      name: "Credit activity for How do atomic credit reservations work?",
    });
    expect(within(activity).getAllByText("Final optimized Compare answer")).toHaveLength(2);
    expect(within(activity).getByText("1 charge")).toBeInTheDocument();
    fireEvent.click(within(activity).getByText("View credit breakdown"));
    expect(
      within(activity).getByText(/Includes Prompt Optimizer \(1 optimizer attempt\) and 2 final answers/),
    ).toBeInTheDocument();
    expect(within(activity).getAllByText("3 credits")).toHaveLength(2);
  });
});

function renderPage() {
  render(
    <MemoryRouter>
      <CreditsPage />
    </MemoryRouter>,
  );
}

function entitlementFixture(): EntitlementsResponse {
  return {
    plan: {
      code: "free",
      display_name: "Free",
      status: "free",
      source: "default",
      renews_at: "2026-08-01T00:00:00Z",
      cancel_at_period_end: false,
      grace_until: null,
    },
    features: {
      compare_enabled: true,
      max_compare_models: 2,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      usage_export_enabled: false,
      saved_history_enabled: true,
      models_catalog_enabled: true,
    },
    model_access: { allowed_billing_classes: ["economical", "standard"] },
    limits: { max_files_per_request: 1, max_file_bytes: 10_000_000 },
    allowances: {
      ai_credits: { used: 10_000, reserved: 0, limit: 100_000, remaining: 90_000 },
    },
    period: { starts_at: "2026-07-01T00:00:00Z", ends_at: "2026-08-01T00:00:00Z" },
  };
}

function transactionFixture(): CreditTransaction[] {
  return [
    {
      id: "credit-1",
      request_id: "request-1",
      activity_id: "activity-1",
      query: "How do atomic credit reservations work?",
      operation_type: "chat",
      item_type: "model",
      provider: "openai",
      model: "gpt-5.4-mini",
      input_tokens: 600,
      output_tokens: 200,
      input_credits: 1_200,
      output_credits: 800,
      fixed_credits: 0,
      total_credits: 2_000,
      provider_cost_usd: 0.002,
      usage_estimated: false,
      pricing_version: "2026-07-29",
      metadata: {},
      created_at: "2026-07-31T14:30:00Z",
    },
    {
      id: "credit-2",
      request_id: "request-1",
      activity_id: "activity-1",
      query: "How do atomic credit reservations work?",
      operation_type: "chat",
      item_type: "research",
      provider: "tavily",
      model: null,
      input_tokens: 0,
      output_tokens: 0,
      input_credits: 0,
      output_credits: 0,
      fixed_credits: 10_000,
      total_credits: 10_000,
      provider_cost_usd: 0.002,
      usage_estimated: false,
      pricing_version: "2026-07-29",
      metadata: {
        provider_credits_used: 2,
        cortex_credits_per_provider_credit: 5_000,
      },
      created_at: "2026-07-31T14:30:00Z",
    },
    {
      id: "credit-3",
      request_id: "request-1",
      activity_id: "activity-1",
      query: "How do atomic credit reservations work?",
      operation_type: "chat",
      item_type: "adjustment",
      provider: null,
      model: null,
      input_tokens: 0,
      output_tokens: 0,
      input_credits: 0,
      output_credits: 0,
      fixed_credits: 0,
      total_credits: 0,
      provider_cost_usd: 0,
      usage_estimated: false,
      pricing_version: "2026-07-29",
      metadata: { unbilled_credits: 200 },
      created_at: "2026-07-31T14:30:00Z",
    },
    {
      id: "credit-4",
      request_id: "optimizer-request-1",
      activity_id: "activity-1",
      query: "How do atomic credit reservations work?",
      operation_type: "optimize",
      item_type: "model",
      provider: "openai",
      model: "gpt-5.4-mini",
      input_tokens: 150,
      output_tokens: 100,
      input_credits: 300,
      output_credits: 700,
      fixed_credits: 0,
      total_credits: 1_000,
      provider_cost_usd: 0.001,
      usage_estimated: false,
      pricing_version: "2026-07-29",
      metadata: {},
      created_at: "2026-07-31T14:29:58Z",
    },
  ];
}

function resetStore() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
}
