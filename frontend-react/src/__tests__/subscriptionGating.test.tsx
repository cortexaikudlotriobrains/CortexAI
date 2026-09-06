import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { uploadFiles } from "../api/files";
import { AttachmentStrip } from "../components/composer/AttachmentStrip";
import { CompareSelector } from "../components/composer/CompareSelector";
import { ModelSelector } from "../components/composer/ModelSelector";
import { PromptComposer } from "../components/composer/PromptComposer";
import { PlanBadge } from "../components/subscription/PlanBadge";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { UpgradeDialog } from "../components/subscription/UpgradeDialog";
import { UsageAllowance } from "../components/subscription/UsageAllowance";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { useChatStore } from "../store/chatStore";
import { localSubscriptionDenial } from "../subscription/subscriptionErrors";
import type {
  BillingPlansResponse,
  EntitlementsResponse,
  ModelBillingClass,
  ModelCatalogItem,
  SubscriptionPlanCode,
} from "../types";

vi.mock("../api/files", () => ({
  uploadFiles: vi.fn(),
  deleteFile: vi.fn().mockResolvedValue(undefined),
  fetchFileStatus: vi.fn(),
}));

describe("subscription feature gating", () => {
  beforeEach(() => {
    useChatStore.getState().startNewChat();
    useChatStore.setState({
      mode: "single",
      smartMode: false,
      researchMode: false,
      compareResearchMode: false,
      optimizeMode: false,
      selectedModelKey: "openai:gpt-5.1",
      compareModelKeys: ["openai:gpt-5.1", "claude:claude-sonnet-4-5", ""],
      subscriptionError: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders reusable plan, status, and allowance controls accessibly", async () => {
    const user = userEvent.setup();
    const manage = vi.fn();
    const entitlements = entitlementFixture({
      status: "past_due",
      grace_until: "2026-08-03T00:00:00Z",
    });

    render(
      <>
        <PlanBadge label="Free" tone="current" />
        <SubscriptionBanner entitlements={entitlements} onManageBilling={manage} />
        <UsageAllowance entitlements={entitlements} />
      </>,
    );

    expect(screen.getAllByText("Free")[0]!.closest("[data-plan-badge]"))
      .toHaveAttribute("data-plan-badge", "current");
    expect(screen.getByRole("alert", { name: "Subscription status" })).toHaveTextContent(
      "Update your payment method",
    );
    expect(screen.getByRole("heading", { name: "Plan allowances" })).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "AI credits: 90 left of 100" }),
    ).toHaveAttribute("aria-valuenow", "10");

    await user.click(screen.getByRole("button", { name: "Manage billing" }));
    expect(manage).toHaveBeenCalledTimes(1);
  });

  it("traps a contextual denial in an accessible, keyboard-dismissible dialog", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const viewPlans = vi.fn();
    const error = localSubscriptionDenial({
      code: "model_not_in_plan",
      message: "gpt-ultra is not available on the Free plan.",
      details: { current_plan: "free", recommended_plan: "pro" },
    });

    render(
      <UpgradeDialog
        error={error}
        onClose={onClose}
        onViewPlans={viewPlans}
        onManageBilling={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "This model is locked on your plan" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Close subscription message" })).toHaveFocus();
    });
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(within(dialog).getByRole("button", { name: "View Pro" })).toHaveFocus();
    await user.keyboard("{Tab}");
    expect(screen.getByRole("button", { name: "Close subscription message" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "View Pro" }));
    expect(viewPlans).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps locked models visible and explains the lock instead of selecting them", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onLocked = vi.fn();
    const models = [model("standard-model", "standard"), model("premium-model", "premium")];

    render(
      <ModelSelector
        models={models}
        value="openai:standard-model"
        onChange={onChange}
        lockedKeys={["openai:premium-model"]}
        lockedLabels={{ "openai:premium-model": "Pro" }}
        onLockedSelect={onLocked}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /Model: ChatGPT \(standard-model\)/ }),
    );
    await user.click(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: /ChatGPT, 2 models/,
      }),
    );
    const locked = within(screen.getByRole("listbox")).getByRole("option", {
      name: /ChatGPT.*premium-model.*Pro/,
    });
    expect(locked).toHaveAttribute("aria-disabled", "true");
    expect(locked).not.toBeDisabled();
    expect(
      document.querySelector<HTMLOptionElement>(
        '#modelSelector option[value="openai:premium-model"]',
      ),
    ).toBeDisabled();
    await user.click(locked);

    expect(onLocked).toHaveBeenCalledWith("openai:premium-model");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows the third-model plan CTA without adding a target", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onTargetLimit = vi.fn();

    render(
      <CompareSelector
        models={DEFAULT_MODELS}
        keys={["openai:gpt-5.1", "claude:claude-sonnet-4-5", ""]}
        onChange={onChange}
        maxTargets={2}
        thirdTargetPlanLabel="Pro"
        onTargetLimit={onTargetLimit}
      />,
    );

    const cta = screen.getByRole("button", { name: "Unlock third comparison model" });
    expect(cta).toHaveTextContent("Add third model");
    expect(cta).toHaveTextContent("Pro");
    await user.click(cta);
    expect(onTargetLimit).toHaveBeenCalledTimes(1);
    expect(onChange).not.toHaveBeenCalled();
  });

  it.each([
    ["free", "Free", ["economical", "standard"], "openai:gpt-5.6-luna"],
    [
      "plus",
      "Plus",
      ["economical", "standard", "advanced"],
      "claude:claude-sonnet-4-6",
    ],
    [
      "pro",
      "Pro",
      ["economical", "standard", "advanced", "premium"],
      "openai:gpt-5.6-terra",
    ],
  ] as Array<[SubscriptionPlanCode, string, ModelBillingClass[], string]>)(
    "shows the %s plan default when Smart routing is turned off",
    async (planCode, displayName, allowedBillingClasses, expectedKey) => {
      const user = userEvent.setup();
      const entitlements = entitlementFixture({
        code: planCode,
        display_name: displayName,
        status: planCode === "free" ? "free" : "active",
      });
      entitlements.model_access.allowed_billing_classes = allowedBillingClasses;
      useChatStore.setState({
        mode: "single",
        smartMode: true,
        selectedModelKey: "",
      });

      render(
        <PromptComposer
          models={DEFAULT_MODELS}
          modelsLoading={false}
          subscription={{ plans: plansFixture(), entitlements, loading: false }}
        />,
      );

      await waitFor(() => {
        expect(useChatStore.getState().selectedModelKey).toBe(expectedKey);
      });
      await user.click(screen.getByRole("switch", { name: "Smart routing" }));

      expect(screen.getByRole("switch", { name: "Smart routing" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
      expect(document.querySelector<HTMLSelectElement>("#singleModel")).toHaveValue(
        expectedKey,
      );
      expect(document.querySelectorAll("#singleModel option")).toHaveLength(
        DEFAULT_MODELS.length,
      );
    },
  );

  it("waits for Free entitlements and initializes Compare with allowed defaults only", async () => {
    useChatStore.setState({
      mode: "compare",
      compareModelKeys: ["", "", ""],
    });
    const plans = plansFixture();
    const { rerender } = render(
      <PromptComposer
        models={DEFAULT_MODELS}
        subscription={{ plans, entitlements: null, loading: true }}
      />,
    );

    expect(useChatStore.getState().compareModelKeys).toEqual(["", "", ""]);

    rerender(
      <PromptComposer
        models={DEFAULT_MODELS}
        subscription={{ plans, entitlements: entitlementFixture(), loading: false }}
      />,
    );

    await waitFor(() => {
      expect(useChatStore.getState().compareModelKeys).toEqual([
        "openai:gpt-5.6-luna",
        "deepseek:deepseek-v4-flash",
        "",
      ]);
    });
    expect(document.querySelectorAll("#compareModel1 option")).toHaveLength(
      DEFAULT_MODELS.length,
    );
    expect(
      document.querySelector<HTMLOptionElement>(
        '#compareModel1 option[value="claude:claude-sonnet-5"]',
      ),
    ).toBeDisabled();
  });

  it("skips a premium fallback for Plus without removing it from the offering", async () => {
    const premium = model("premium-fallback", "premium");
    const economicalFallback = DEFAULT_MODELS.find(
      (candidate) => candidate.model === "deepseek-v4-flash",
    )!;
    const models = [DEFAULT_MODELS[0]!, premium, economicalFallback];
    const entitlements = entitlementFixture({ code: "plus", display_name: "Plus" });
    entitlements.model_access.allowed_billing_classes = [
      "economical",
      "standard",
      "advanced",
    ];
    useChatStore.setState({
      mode: "compare",
      compareModelKeys: ["", "", ""],
    });

    render(
      <PromptComposer
        models={models}
        subscription={{ plans: plansFixture(), entitlements, loading: false }}
      />,
    );

    await waitFor(() => {
      expect(useChatStore.getState().compareModelKeys).toEqual([
        "openai:gpt-5.6-luna",
        "deepseek:deepseek-v4-flash",
        "",
      ]);
    });
    expect(document.querySelectorAll("#compareModel1 option")).toHaveLength(models.length);
    expect(
      document.querySelector<HTMLOptionElement>(
        '#compareModel1 option[value="openai:premium-fallback"]',
      ),
    ).toBeDisabled();
  });

  it("explains exhausted Web access without changing or clearing the draft", async () => {
    const user = userEvent.setup();
    const entitlements = entitlementFixture();
    entitlements.allowances.ai_credits = {
      used: 100_000,
      reserved: 0,
      limit: 100_000,
      remaining: 0,
    };
    useChatStore.setState({ prompt: "Keep this draft" });

    render(
      <PromptComposer
        models={DEFAULT_MODELS}
        subscription={{ plans: plansFixture(), entitlements }}
      />,
    );

    const research = screen.getByRole("switch", { name: "Research mode" });
    expect(research).toHaveAttribute("aria-disabled", "true");
    expect(
      screen.getByRole("tooltip", {
        name: /Uses latest information from the web.*0 credits left/,
      }),
    ).toBeInTheDocument();
    await user.click(research);

    expect(useChatStore.getState().subscriptionError?.code).toBe(
      "insufficient_credits",
    );
    expect(useChatStore.getState().prompt).toBe("Keep this draft");
    expect(research).toHaveAttribute("aria-checked", "false");
  });

  it("does not show an estimated credit warning for expensive Compare requests", () => {
    const premium = model("premium-model", "premium");
    const models = [...DEFAULT_MODELS, premium];
    const entitlements = entitlementFixture({ code: "pro", display_name: "Pro" });
    entitlements.features.max_compare_models = 3;
    entitlements.model_access.allowed_billing_classes = [
      "economical",
      "standard",
      "advanced",
      "premium",
    ];
    useChatStore.setState({
      mode: "compare",
      compareResearchMode: true,
      compareModelKeys: [
        "openai:gpt-5.1",
        "claude:claude-sonnet-4-5",
        "openai:premium-model",
      ],
    });

    render(
      <PromptComposer
        models={models}
        subscription={{ plans: plansFixture(), entitlements }}
      />,
    );

    expect(screen.queryByLabelText("Estimated credit usage warning")).not.toBeInTheDocument();
    expect(screen.queryByText(/Higher credit use expected/i)).not.toBeInTheDocument();
  });

  it("blocks over-limit files before upload and keeps existing attachments", async () => {
    const entitlements = entitlementFixture();
    const oversized = new File([new Uint8Array(11_000_000)], "large.pdf", {
      type: "application/pdf",
    });

    render(<AttachmentStrip entitlements={entitlements} plans={plansFixture()} />);
    expect(screen.getByText("Up to 1 file · 10 MB each")).toBeInTheDocument();

    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: { files: [oversized] },
    });

    await waitFor(() => {
      expect(useChatStore.getState().subscriptionError?.details.feature).toBe(
        "attachment_size",
      );
    });
    expect(uploadFiles).not.toHaveBeenCalled();
    expect(useChatStore.getState().attachments).toEqual([]);
  });

  it("rejects an over-count batch before requesting upload authorization", async () => {
    const entitlements = entitlementFixture();
    render(<AttachmentStrip entitlements={entitlements} plans={plansFixture()} />);

    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: {
        files: [
          new File(["one"], "one.txt", { type: "text/plain" }),
          new File(["two"], "two.txt", { type: "text/plain" }),
        ],
      },
    });

    await waitFor(() => {
      expect(useChatStore.getState().subscriptionError?.details.feature).toBe(
        "attachment_count",
      );
    });
    expect(uploadFiles).not.toHaveBeenCalled();
    expect(screen.queryByText("one.txt")).not.toBeInTheDocument();
    expect(screen.queryByText("two.txt")).not.toBeInTheDocument();
  });

  it("preserves file-analysis entitlement gating before any upload request", async () => {
    const entitlements = entitlementFixture();
    entitlements.features.file_analysis_enabled = false;
    render(<AttachmentStrip entitlements={entitlements} plans={plansFixture()} />);

    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: {
        files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
      },
    });

    await waitFor(() => {
      expect(useChatStore.getState().subscriptionError?.details.feature).toBe(
        "file_analysis",
      );
    });
    expect(uploadFiles).not.toHaveBeenCalled();
    expect(screen.queryByText("notes.txt")).not.toBeInTheDocument();
  });

  it("uploads an accepted multi-file selection through one batch request", async () => {
    const entitlements = {
      ...entitlementFixture({
        code: "plus",
        display_name: "Plus",
        status: "active",
        source: "stripe",
      }),
      limits: { max_files_per_request: 3, max_file_bytes: 20_000_000 },
    };
    const uploaded = [
      {
        file_id: "file-one",
        original_filename: "one.txt",
        mime_type: "text/plain",
        size_bytes: 3,
        status: "ready" as const,
        error_code: null,
        error_message: null,
        ingestion_meta: {},
        created_at: "2026-07-30T00:00:00Z",
        updated_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-08-06T00:00:00Z",
        deduplicated: false,
      },
      {
        file_id: "file-two",
        original_filename: "two.txt",
        mime_type: "text/plain",
        size_bytes: 3,
        status: "ready" as const,
        error_code: null,
        error_message: null,
        ingestion_meta: {},
        created_at: "2026-07-30T00:00:00Z",
        updated_at: "2026-07-30T00:00:00Z",
        expires_at: "2026-08-06T00:00:00Z",
        deduplicated: false,
      },
    ];
    vi.mocked(uploadFiles).mockResolvedValue(uploaded);
    render(<AttachmentStrip entitlements={entitlements} plans={plansFixture()} />);
    const selected = [
      new File(["one"], "one.txt", { type: "text/plain" }),
      new File(["two"], "two.txt", { type: "text/plain" }),
    ];

    fireEvent.change(document.querySelector("#attachmentInput")!, {
      target: { files: selected },
    });

    await waitFor(() => expect(useChatStore.getState().attachments).toHaveLength(2));
    expect(uploadFiles).toHaveBeenCalledTimes(1);
    expect(uploadFiles).toHaveBeenCalledWith(selected, {
      provider: "openai",
      model: "gpt-5.1",
    });
  });

  it("keeps allowance details and denial actions available at a phone viewport", () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 360 });
    const error = localSubscriptionDenial({
      code: "feature_not_in_plan",
      message: "Three-model Compare is not available on the Free plan.",
      details: {
        feature: "compare_model_count",
        current_plan: "free",
        recommended_plan: "pro",
      },
    });

    try {
      render(
        <>
          <UsageAllowance entitlements={entitlementFixture()} compact />
          <UpgradeDialog
            error={error}
            onClose={vi.fn()}
            onViewPlans={vi.fn()}
            onManageBilling={vi.fn()}
          />
        </>,
      );

      expect(screen.getAllByRole("progressbar")).toHaveLength(1);
      expect(screen.getByRole("dialog", { name: "Three-model Compare requires Pro" }))
        .toBeVisible();
      expect(screen.getByRole("button", { name: "View Pro" })).toBeVisible();
    } finally {
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: originalWidth,
      });
    }
  });
});

function entitlementFixture(
  planOverrides: Partial<EntitlementsResponse["plan"]> = {},
): EntitlementsResponse {
  const counter = (used: number, limit: number) => ({
    used,
    reserved: 0,
    limit,
    remaining: Math.max(0, limit - used),
  });
  return {
    plan: {
      code: "free",
      display_name: "Free",
      status: "free",
      source: "default",
      renews_at: "2026-08-19T00:00:00Z",
      cancel_at_period_end: false,
      grace_until: null,
      ...planOverrides,
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
      ai_credits: counter(10_000, 100_000),
    },
    period: {
      starts_at: "2026-07-19T00:00:00Z",
      ends_at: "2026-08-19T00:00:00Z",
    },
  };
}

function plansFixture(): BillingPlansResponse {
  const plan = (
    code: "free" | "plus" | "pro",
    classes: ModelBillingClass[],
    maxCompare: number,
  ) => ({
    code,
    display_name: `${code.charAt(0).toUpperCase()}${code.slice(1)}`,
    monthly_price: code === "free" ? 0 : code === "plus" ? 6.99 : 12.99,
    recommended: code === "plus",
    features: {
      max_compare_models: maxCompare,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      allowed_billing_classes: classes,
    },
    allowances: {
      ai_credits: code === "free" ? 100_000 : code === "plus" ? 1_000_000 : 3_000_000,
    },
  });
  return {
    currency: "USD",
    billing_period: "monthly",
    billing_enabled: true,
    plans: [
      plan("free", ["economical", "standard"], 2),
      plan("plus", ["economical", "standard", "advanced"], 2),
      plan("pro", ["economical", "standard", "advanced", "premium"], 3),
    ],
  };
}

function model(name: string, billingClass: ModelBillingClass): ModelCatalogItem {
  return {
    provider: "openai",
    model: name,
    tier: "frontier",
    billing_class: billingClass,
    access_category: billingClass,
    input_credit_multiplier: billingClass === "premium" ? 6 : 2,
    output_credit_multiplier: billingClass === "premium" ? 30 : 8,
    credit_usage_label: billingClass === "premium" ? "Premium" : "Standard",
    credit_pricing_version: "2026-07-29",
    input_cost_per_1m: 0,
    output_cost_per_1m: 0,
    context_limit: 128_000,
    tags: [],
    enabled: true,
    supports_image_input: false,
    supported_attachment_mime_types: [],
  };
}
