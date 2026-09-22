import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelsCatalogScreen, ModelsPage } from "../pages/ModelsPage";
import { MODELS_CATALOG, getModelsCatalogSummary } from "../config/modelsCatalog";
import { useChatStore } from "../store/chatStore";
import type {
  BillingPlansResponse,
  EntitlementsResponse,
  ModelCatalogItem,
} from "../types";

const hookMocks = vi.hoisted(() => ({
  loadHistory: vi.fn(),
  clearHistory: vi.fn(),
  removeThread: vi.fn(),
  cancel: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  toggleTheme: vi.fn(),
  subscriptionState: {
    current: { entitlements: null as EntitlementsResponse | null, plans: null as BillingPlansResponse | null },
  },
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

vi.mock("../hooks/useModels", () => ({
  useModels: () => ({ models: [], loading: false, error: null }),
}));

vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "light", toggleTheme: hookMocks.toggleTheme }),
}));

describe("ModelsPage", () => {
  const catalogModelCount = getModelsCatalogSummary(MODELS_CATALOG).modelCount;
  const codingModelCount = MODELS_CATALOG.providers.reduce(
    (total, provider) =>
      total + provider.models.filter((model) => model.tags.includes("Coding")).length,
    0,
  );

  beforeEach(() => {
    resetStore();
    hookMocks.subscriptionState.current = { entitlements: null, plans: null };
    (
      window as unknown as { CORTEX_RUNTIME_CONFIG?: Record<string, unknown> }
    ).CORTEX_RUNTIME_CONFIG = { modelsCatalogEnabled: true };
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    resetStore();
    delete (
      window as unknown as { CORTEX_RUNTIME_CONFIG?: Record<string, unknown> }
    ).CORTEX_RUNTIME_CONFIG;
  });

  it("renders the Models route with the compact task-first catalog", () => {
    renderPage();

    expect(screen.getAllByRole("heading", { name: "Models" }).length).toBeGreaterThan(0);
    expect(
      screen.getByText(`${catalogModelCount} of ${catalogModelCount} models`),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Models" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: "Coding" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(screen.getByLabelText("Smart routing hint")).toHaveTextContent("Smart routing");
    expect(screen.getByRole("button", { name: "GPT-5.6 Luna details" })).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Speed: Fast" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("img", { name: "Depth: Deep" }).length).toBeGreaterThan(0);
  });

  it("filters by task and highlights the recommended row", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Coding" }));

    expect(
      screen.getByText(`${codingModelCount} of ${catalogModelCount} models`),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Coding" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByLabelText("Best model for Coding")).toHaveTextContent(
      "DeepSeek V4 Flash",
    );
    const sonnetRow = screen
      .getByRole("button", { name: "DeepSeek V4 Flash details" })
      .closest("article");
    expect(sonnetRow).not.toBeNull();
    expect(within(sonnetRow!).getByText("★ TOP")).toBeInTheDocument();
  });

  it("filters by search and exposes the no-match state with a clear action", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(screen.getByRole("searchbox", { name: "Search models" }), "not-a-model");

    expect(screen.getByText(`0 of ${catalogModelCount} models`)).toBeInTheDocument();
    expect(screen.getByText("No models match — clear filters")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    expect(
      screen.getByText(`${catalogModelCount} of ${catalogModelCount} models`),
    ).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search models" })).toHaveValue("");
  });

  it("expands a compact model row to show tags, strengths, and the model id", async () => {
    const user = userEvent.setup();
    renderPage();

    const rowButton = screen.getByRole("button", { name: "DeepSeek V4 Flash details" });
    expect(rowButton).toHaveAttribute("aria-expanded", "false");

    await user.click(rowButton);

    expect(rowButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("deepseek:deepseek-v4-flash")).toBeInTheDocument();
    expect(screen.getByText("Reasoning modes: none, thinking")).toBeInTheDocument();
    expect(screen.getAllByText("Long context").length).toBeGreaterThan(1);
  });

  it("renders the loading skeleton state for future async catalog loading", () => {
    render(<ModelsCatalogScreen loading />);

    expect(screen.getByLabelText("Loading models")).toHaveAttribute("aria-busy", "true");
  });

  it("keeps a live premium catalogue model visible while explaining its lock", async () => {
    const user = userEvent.setup();
    const onAccessDenied = vi.fn();
    render(
      <ModelsCatalogScreen
        entitlements={entitlementFixture()}
        plans={plansFixture()}
        liveModels={[liveUltraModel()]}
        onAccessDenied={onAccessDenied}
      />,
    );

    const row = screen.getByRole("button", { name: "GPT-5.4 details" }).closest("article");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("Pro plan")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "GPT-5.4 details" }));
    await user.click(within(row!).getByRole("button", { name: "See plan options" }));
    expect(onAccessDenied).toHaveBeenCalledWith(
      expect.objectContaining({ code: "model_not_in_plan" }),
    );
  });
});

function renderPage() {
  render(
    <MemoryRouter>
      <ModelsPage />
    </MemoryRouter>,
  );
}

function resetStore() {
  useChatStore.getState().startNewChat();
  useChatStore.getState().setHistory([]);
  useChatStore.getState().setHistorySearch("");
  useChatStore.getState().setMode("single");
}

function entitlementFixture(): EntitlementsResponse {
  return {
    plan: {
      code: "free",
      display_name: "Free",
      status: "free",
      source: "default",
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
      usage_export_enabled: true,
      saved_history_enabled: true,
      models_catalog_enabled: true,
    },
    model_access: { allowed_billing_classes: ["standard", "advanced"] },
    limits: { max_files_per_request: 1, max_file_bytes: 10_000_000 },
    allowances: {},
    period: { starts_at: "2026-07-19T00:00:00Z", ends_at: "2026-08-19T00:00:00Z" },
  };
}

function plansFixture(): BillingPlansResponse {
  const baseAllowances = {
    ai_credits: 100_000,
  };
  return {
    currency: "USD",
    billing_period: "monthly",
    billing_enabled: true,
    plans: [
      {
        code: "free",
        display_name: "Free",
        monthly_price: 0,
        recommended: false,
        features: {
          max_compare_models: 2,
          research_enabled: true,
          prompt_improvement_enabled: true,
          file_analysis_enabled: true,
          allowed_billing_classes: ["economical", "standard"],
        },
        allowances: baseAllowances,
      },
      {
        code: "pro",
        display_name: "Pro",
        monthly_price: 12.99,
        recommended: false,
        features: {
          max_compare_models: 3,
          research_enabled: true,
          prompt_improvement_enabled: true,
          file_analysis_enabled: true,
          allowed_billing_classes: ["economical", "standard", "advanced", "premium"],
        },
        allowances: { ai_credits: 3_000_000 },
      },
    ],
  };
}

function liveUltraModel(): ModelCatalogItem {
  return {
    provider: "openai",
    model: "gpt-5.4",
    tier: "frontier",
    billing_class: "premium",
    input_cost_per_1m: 0,
    output_cost_per_1m: 0,
    context_limit: 128_000,
    tags: [],
    enabled: true,
    supports_image_input: true,
    supported_attachment_mime_types: [],
  };
}
