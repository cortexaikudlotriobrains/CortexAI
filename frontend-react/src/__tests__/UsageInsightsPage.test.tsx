import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UsageInsightsPage } from "../pages/UsageInsightsPage";
import type { UserFacingError } from "../errors/userFacingError";
import { useChatStore } from "../store/chatStore";
import type { EntitlementsResponse, UsageSummary } from "../types";

interface MockUsageState {
  summary: UsageSummary | null;
  loading: boolean;
  error: UserFacingError | null;
  reload: () => void;
}

interface MockUsageParams {
  from?: string;
  to?: string;
}

const hookMocks = vi.hoisted(() => ({
  usageState: {
    current: {
      summary: null as UsageSummary | null,
      loading: true as boolean,
      error: null as UserFacingError | null,
      reload: vi.fn(),
    } satisfies MockUsageState,
  },
  usageParams: {
    current: {} as MockUsageParams,
  },
  exportUsageCsv: vi.fn(),
  loadHistory: vi.fn(),
  clearHistory: vi.fn(),
  removeThread: vi.fn(),
  cancel: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  toggleTheme: vi.fn(),
  subscriptionState: {
    current: { entitlements: null as EntitlementsResponse | null },
  },
}));

vi.mock("../hooks/useUsageSummary", () => ({
  useUsageSummary: (params: MockUsageParams = {}) => {
    hookMocks.usageParams.current = params;
    return hookMocks.usageState.current;
  },
}));

vi.mock("../api/usage", () => ({
  exportUsageCsv: hookMocks.exportUsageCsv,
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    whoAmI: null,
    cognitoConfig: { enabled: false },
    loading: false,
    loggedIn: false,
    login: hookMocks.login,
    logout: hookMocks.logout,
  }),
}));

vi.mock("../hooks/useHistory", () => ({
  useHistory: () => ({
    load: hookMocks.loadHistory,
    clear: hookMocks.clearHistory,
    removeThread: hookMocks.removeThread,
  }),
}));

vi.mock("../hooks/useChat", () => ({
  useChat: () => ({ cancel: hookMocks.cancel }),
}));

vi.mock("../hooks/useSubscription", () => ({
  useSubscription: () => hookMocks.subscriptionState.current,
}));

vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "light", toggleTheme: hookMocks.toggleTheme }),
}));

describe("UsageInsightsPage states", () => {
  beforeEach(() => {
    hookMocks.usageState.current = {
      summary: null,
      loading: true,
      error: null,
      reload: vi.fn(),
    };
    hookMocks.usageParams.current = {};
    hookMocks.exportUsageCsv.mockResolvedValue(new Blob(["date,tokens\n"], { type: "text/csv" }));
    hookMocks.subscriptionState.current = { entitlements: null };
    resetStore();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetStore();
  });

  it("renders the Step 2 loading skeleton state", () => {
    renderPage();

    expect(screen.getByLabelText("Loading usage insights")).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("renders backend-driven KPI cards after usage data loads", () => {
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(
      screen.getByLabelText("TOTAL TOKENS 2.84M 18.4% vs prev 30d"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("REQUESTS 1,336 across 312 sessions"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("AVG LATENCY 4.6s p95 8.1s \u00b7 fastest 1.4s"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("AVG COST / REQ $0.0091 $12.16 total spend"),
    ).toBeInTheDocument();
  });

  it("keeps credit balance and activity out of the usage analytics page", () => {
    hookMocks.subscriptionState.current = { entitlements: entitlementFixture() };
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(screen.queryByRole("heading", { name: "AI credit balance" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Recent credit activity" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Models that replied" })).toBeInTheDocument();
  });

  it("renders the model leaderboard with Smart routing metadata and provider logos", () => {
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(screen.getByRole("heading", { name: "Models that replied" })).toBeInTheDocument();
    expect(
      screen.getByText("720 of 1,336 replies auto-routed by Smart"),
    ).toBeInTheDocument();
    expect(screen.getByText("1,336 total")).toBeInTheDocument();
    expect(screen.getByText("MOST USED IN SMART")).toBeInTheDocument();
    expect(screen.getByText("manual only")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "GPT-5.4 Mini: 512 replies, 470 via Smart",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Llama 3.3 70B: 34 replies, manual only",
      }),
    ).toBeInTheDocument();
    expectUsageProviderLogo("openai", "openai", "domain_url=openai.com");
    expectUsageProviderLogo("anthropic", "claude", "domain_url=claude.ai");
    expectUsageProviderLogo("deepseek", "deepseek", "domain_url=deepseek.com");
    expectUsageProviderLogo("google", "gemini", "domain_url=gemini.google.com");
    expectUsageProviderLogo("meta", "meta", "domain_url=meta.com");
    expectUsageProviderLogo("mistral", "mistral", "domain_url=mistral.ai");
  });

  it("renders the session modes panel from backend summary fields", () => {
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(screen.getByRole("heading", { name: "Session modes" })).toBeInTheDocument();
    expect(screen.getByText("How sessions split across modes")).toBeInTheDocument();
    expect(screen.getByText("Ask only")).toBeInTheDocument();
    expect(screen.getByText("Compare only")).toBeInTheDocument();
    expect(screen.getByText("Mixed")).toBeInTheDocument();
    expect(screen.getByText("168")).toBeInTheDocument();
    expect(screen.getByText("96")).toBeInTheDocument();
    expect(screen.getByText("54%")).toBeInTheDocument();
    expect(screen.getByText("31%")).toBeInTheDocument();
    expect(screen.getByText("15%")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Session modes: Ask only 168 sessions, 54%; Compare only 96 sessions, 31%; Mixed 48 sessions, 15%",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("48 sessions switched modes")).toBeInTheDocument();
    expect(screen.getByText(/Ask \u2194 Compare in one session\./)).toBeInTheDocument();
  });

  it("renders the 14-day activity chart from backend daily tokens", () => {
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(screen.getByRole("heading", { name: "Tokens \u00b7 last 14 days" })).toBeInTheDocument();
    expect(screen.getByText("2.84M total \u00b7 ~203K/day")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Tokens last 14 days: 2,839,590 total, approximately 202,828 per day",
      }),
    ).toBeInTheDocument();
    expect(document.querySelectorAll("[data-usage-activity-bar]")).toHaveLength(14);
    expect(document.querySelector('[data-usage-activity-bar="2026-06-20"]')).toHaveAttribute(
      "data-usage-activity-tone",
      "weekend",
    );
    expect(document.querySelector('[data-usage-activity-bar="2026-06-23"]')).toHaveAttribute(
      "data-usage-activity-tone",
      "weekday",
    );
    expect(screen.getByText("Jun 18")).toBeInTheDocument();
    expect(screen.getByText("Jun 22")).toBeInTheDocument();
    expect(screen.getByText("Jun 26")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
  });

  it("refetches usage summary when the period selector changes", async () => {
    const user = userEvent.setup();
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(hookMocks.usageParams.current).toEqual({});

    await user.click(screen.getByRole("button", { name: "Select usage period: Last 30 days" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Last 7 days" }));

    expect(screen.getByRole("button", { name: "Select usage period: Last 7 days" })).toBeInTheDocument();
    expect(hookMocks.usageParams.current).toEqual(expectedTrailingPeriod(7));
  });

  it("exports day-grouped CSV for the loaded usage period", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn(() => "blob:usage-export");
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };
    hookMocks.subscriptionState.current = { entitlements: entitlementFixture("plus") };

    try {
      renderPage();

      await user.click(screen.getByRole("button", { name: "Export usage CSV for Last 30 days" }));

      await waitFor(() => {
        expect(hookMocks.exportUsageCsv).toHaveBeenCalledWith({
          from: "2026-06-02",
          to: "2026-07-01",
          groupBy: "day",
        });
      });
      expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
      expect(click).toHaveBeenCalledTimes(1);
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:usage-export");
    } finally {
      click.mockRestore();
      Object.defineProperty(URL, "createObjectURL", {
        configurable: true,
        value: originalCreateObjectURL,
      });
      Object.defineProperty(URL, "revokeObjectURL", {
        configurable: true,
        value: originalRevokeObjectURL,
      });
    }
  });

  it("shows safe visible recovery copy when CSV export fails", async () => {
    const user = userEvent.setup();
    hookMocks.usageState.current = {
      summary: usageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };
    hookMocks.subscriptionState.current = { entitlements: entitlementFixture("plus") };
    hookMocks.exportUsageCsv.mockRejectedValueOnce(
      new Error("S3 bucket cortex-private-export was unavailable"),
    );

    renderPage();
    await user.click(screen.getByRole("button", { name: "Export usage CSV for Last 30 days" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Usage couldn't be exported");
    expect(alert).toHaveTextContent("No file was downloaded. Please try again.");
    expect(alert).not.toHaveTextContent("cortex-private-export");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("renders the empty model activity state", () => {
    hookMocks.usageState.current = {
      summary: emptyUsageSummary(),
      loading: false,
      error: null,
      reload: vi.fn(),
    };

    renderPage();

    expect(screen.getByLabelText("TOTAL TOKENS 0 \u2014")).toBeInTheDocument();
    expect(screen.getByLabelText("AVG LATENCY \u2014 \u2014")).toBeInTheDocument();
    expect(screen.getByText("No model activity yet for this period")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Session modes: Ask only 0 sessions, 0%; Compare only 0 sessions, 0%; Mixed 0 sessions, 0%",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("0 total \u00b7 ~0/day")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Tokens last 14 days: 0 total, approximately 0 per day",
      }),
    ).toBeInTheDocument();
  });

  it("renders an inline retry action for errors", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    hookMocks.usageState.current = {
      summary: null,
      loading: false,
      error: {
        code: "service_unavailable",
        title: "Usage data couldn't load",
        message: "Your usage data is safe. Please try again.",
        retryable: true,
        action: "retry",
        actionLabel: "Try again",
        context: "usage_load",
      },
      reload,
    };

    renderPage();

    expect(screen.getByRole("alert")).toHaveTextContent("Usage data couldn't load");
    expect(screen.getByRole("alert")).not.toHaveTextContent("Reporting service unavailable");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

function renderPage() {
  render(
    <MemoryRouter>
      <UsageInsightsPage />
    </MemoryRouter>,
  );
}

function emptyUsageSummary(): UsageSummary {
  return usageSummary({
    totalTokens: 0,
    totalRequests: 0,
    totalSessions: 0,
    avgLatencyMs: 0,
    p95LatencyMs: 0,
    minLatencyMs: 0,
    avgCostPerRequest: 0,
    totalSpend: 0,
    tokensDeltaPct: 0,
    smartRoutedTotal: 0,
    models: [],
    sessionModes: { askOnly: 0, compareOnly: 0, mixed: 0 },
    switchedMidSession: 0,
    activityDaily: [],
  });
}

function usageSummary(overrides: Partial<UsageSummary> = {}): UsageSummary {
  return {
    period: { from: "2026-06-02", to: "2026-07-01", label: "Last 30 days" },
    totalTokens: 2840000,
    totalRequests: 1336,
    totalSessions: 312,
    avgLatencyMs: 4600,
    p95LatencyMs: 8100,
    minLatencyMs: 1400,
    avgCostPerRequest: 0.0091,
    totalSpend: 12.16,
    tokensDeltaPct: 18.4,
    smartRoutedTotal: 720,
    models: [
      {
        provider: "openai",
        modelId: "gpt-5.4-mini",
        displayName: "GPT-5.4 Mini",
        replies: 512,
        viaSmart: 470,
      },
      {
        provider: "anthropic",
        modelId: "claude-sonnet-4-5",
        displayName: "Claude Sonnet 4.5",
        replies: 318,
        viaSmart: 88,
      },
      {
        provider: "deepseek",
        modelId: "deepseek-reasoner",
        displayName: "DeepSeek Reasoner",
        replies: 220,
        viaSmart: 105,
      },
      {
        provider: "google",
        modelId: "gemini-2.5-pro",
        displayName: "Gemini 2.5 Pro",
        replies: 120,
        viaSmart: 57,
      },
      {
        provider: "mistral",
        modelId: "mistral-large-latest",
        displayName: "Mistral Large",
        replies: 64,
        viaSmart: 12,
      },
      {
        provider: "meta",
        modelId: "llama-3.3-70b",
        displayName: "Llama 3.3 70B",
        replies: 34,
        viaSmart: 0,
      },
    ],
    sessionModes: { askOnly: 168, compareOnly: 96, mixed: 48 },
    switchedMidSession: 48,
    activityDaily: usageActivityDays(),
    ...overrides,
  };
}

function usageActivityDays() {
  return [
    { date: "2026-06-18", tokens: 147420 },
    { date: "2026-06-19", tokens: 203580 },
    { date: "2026-06-20", tokens: 175500 },
    { date: "2026-06-21", tokens: 252720 },
    { date: "2026-06-22", tokens: 224640 },
    { date: "2026-06-23", tokens: 115830 },
    { date: "2026-06-24", tokens: 98280 },
    { date: "2026-06-25", tokens: 210600 },
    { date: "2026-06-26", tokens: 259740 },
    { date: "2026-06-27", tokens: 238680 },
    { date: "2026-06-28", tokens: 315900 },
    { date: "2026-06-29", tokens: 280800 },
    { date: "2026-06-30", tokens: 126360 },
    { date: "2026-07-01", tokens: 189540 },
  ];
}

function expectedTrailingPeriod(days: number) {
  const to = new Date();
  const from = new Date(to);
  from.setDate(to.getDate() - (days - 1));
  return {
    from: toIsoDate(from),
    to: toIsoDate(to),
  };
}

function entitlementFixture(planCode: "free" | "plus" = "free"): EntitlementsResponse {
  const isPaid = planCode === "plus";
  const counter = (used: number, limit: number) => ({
    used,
    reserved: 0,
    limit,
    remaining: limit - used,
  });
  return {
    plan: {
      code: planCode,
      display_name: isPaid ? "Plus" : "Free",
      status: isPaid ? "active" : "free",
      source: isPaid ? "stripe" : "default",
      renews_at: "2026-08-19T00:00:00Z",
      cancel_at_period_end: false,
      grace_until: null,
    },
    features: {
      compare_enabled: true,
      max_compare_models: 2,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      usage_export_enabled: isPaid,
      saved_history_enabled: true,
      models_catalog_enabled: true,
    },
    model_access: {
      allowed_billing_classes: isPaid
        ? ["economical", "standard", "advanced"]
        : ["economical", "standard"],
    },
    limits: {
      max_files_per_request: isPaid ? 3 : 1,
      max_file_bytes: isPaid ? 20_000_000 : 10_000_000,
    },
    allowances: {
      ai_credits: counter(isPaid ? 124_000 : 10_000, isPaid ? 1_000_000 : 100_000),
    },
    period: { starts_at: "2026-07-19T00:00:00Z", ends_at: "2026-08-19T00:00:00Z" },
  };
}

function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function resetStore() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
}

function expectUsageProviderLogo(
  usageProvider: string,
  presentationProvider: string,
  expectedDomain: string,
) {
  expect(
    document.querySelector(
      `[data-usage-provider-logo="${usageProvider}"] [data-provider-logo="${presentationProvider}"]`,
    ),
  ).toHaveAttribute("src", expect.stringContaining(expectedDomain));
}
