import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BillingPageContent } from "../pages/BillingPage";
import { PricingPageContent } from "../pages/PricingPage";
import type {
  BillingPlansResponse,
  BillingSubscriptionResponse,
  EntitlementsResponse,
  SubscriptionPlanCode,
  SubscriptionStatus,
} from "../types";

describe("PricingPageContent", () => {
  afterEach(cleanup);

  it("renders the public catalogue and sign-in actions for signed-out visitors", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderPricing({ loggedIn: false, authEnabled: true, onLogin });

    expect(
      screen.getByRole("heading", { name: "More choice, with clear monthly limits." }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Free" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Plus" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pro" })).toBeInTheDocument();
    expect(screen.getByText("$6.99")).toBeInTheDocument();
    expect(within(planCard("Free")).getByText("100 AI credits per month")).toBeInTheDocument();
    expect(within(planCard("Plus")).getByText("1,000 AI credits per month")).toBeInTheDocument();
    expect(within(planCard("Pro")).getByText("3,000 AI credits per month")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Sign in to choose" })).toHaveLength(3);

    await user.click(screen.getAllByRole("button", { name: "Sign in to choose" })[1]);
    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it("marks Free as current and starts only server-owned paid-plan Checkout", async () => {
    const user = userEvent.setup();
    const onCheckout = vi.fn();
    renderPricing({
      loggedIn: true,
      subscription: subscriptionResponse("free"),
      entitlements: entitlementsResponse("free"),
      onCheckout,
    });

    const freeCard = planCard("Free");
    const plusCard = planCard("Plus");
    expect(within(freeCard).getByText("CURRENT PLAN")).toBeInTheDocument();
    expect(within(freeCard).getByRole("button", { name: "Current plan" })).toBeDisabled();

    await user.click(within(plusCard).getByRole("button", { name: "Upgrade" }));
    expect(onCheckout).toHaveBeenCalledWith("plus");
  });

  it("sends paid subscribers to the billing portal instead of starting plan-switch Checkout", async () => {
    const user = userEvent.setup();
    const onPortal = vi.fn();
    const onCheckout = vi.fn();
    renderPricing({
      loggedIn: true,
      subscription: subscriptionResponse("plus"),
      entitlements: entitlementsResponse("plus"),
      onPortal,
      onCheckout,
    });

    expect(
      within(planCard("Plus")).getByRole("button", { name: "Manage current plan" }),
    ).toBeInTheDocument();
    await user.click(within(planCard("Pro")).getByRole("button", { name: "Manage plan" }));

    expect(onPortal).toHaveBeenCalledTimes(1);
    expect(onCheckout).not.toHaveBeenCalled();
  });

  it("does not offer Portal actions when the backend says the paid account is not manageable", () => {
    renderPricing({
      loggedIn: true,
      subscription: { ...subscriptionResponse("plus"), can_manage: false },
      entitlements: entitlementsResponse("plus"),
    });

    expect(within(planCard("Plus")).getByRole("button", { name: "Current plan" })).toBeDisabled();
    expect(within(planCard("Pro")).getByRole("button", { name: "Unavailable" })).toBeDisabled();
  });

  it("renders payment, cancellation, disabled-billing, and pending-confirmation states conservatively", () => {
    const { rerender } = renderPricing({
      loggedIn: true,
      subscription: subscriptionResponse("plus", "past_due"),
      entitlements: entitlementsResponse("plus", "past_due"),
    });
    expect(screen.getByRole("alert")).toHaveTextContent("payment needs attention");
    expect(screen.getAllByRole("button", { name: "Update payment" })).toHaveLength(3);

    rerender(
      pricingElement({
        loggedIn: true,
        subscription: subscriptionResponse("free", "canceled"),
        entitlements: entitlementsResponse("free", "canceled"),
        checkoutConfirmation: "confirming",
      }),
    );
    expect(screen.getByText(/paid subscription has ended/i)).toBeInTheDocument();
    expect(screen.getByText(/waiting for the verified subscription update/i)).toBeInTheDocument();

    rerender(
      pricingElement({
        loggedIn: true,
        plans: plansResponse(false),
        subscription: subscriptionResponse("free"),
        entitlements: entitlementsResponse("free"),
      }),
    );
    expect(screen.getAllByRole("button", { name: "Unavailable" })).toHaveLength(2);
    expect(within(planCard("Free")).getByRole("button", { name: "Current plan" })).toBeDisabled();
  });

  it.each(["plus", "pro"] as const)(
    "shows granted %s as current when hosted billing is disabled",
    (code) => {
      const entitlements = entitlementsResponse(code);
      entitlements.plan.source = "cortex_grant";
      renderPricing({
        loggedIn: true,
        plans: plansResponse(false),
        entitlements,
        subscription: { ...subscriptionResponse(code), provider: null, can_manage: false },
      });
      expect(
        within(planCard(code === "plus" ? "Plus" : "Pro")).getByRole("button", {
          name: "Current plan",
        }),
      ).toBeDisabled();
      expect(screen.getAllByRole("button", { name: "Unavailable" })).toHaveLength(2);
      expect(screen.queryByRole("button", { name: /Manage|Upgrade/ })).not.toBeInTheDocument();
    },
  );
});

describe("BillingPageContent", () => {
  afterEach(cleanup);

  it.each(["plus", "pro"] as const)(
    "describes granted %s access and monthly resets without payment actions",
    (code) => {
      const entitlements = entitlementsResponse(code);
      entitlements.plan.source = "cortex_grant";
      renderBilling({
        loggedIn: true,
        plans: plansResponse(false),
        entitlements,
        subscription: { ...subscriptionResponse(code), provider: null, can_manage: false },
      });
      expect(screen.getByText(/plan access is provided by CortexAI/)).toBeInTheDocument();
      expect(screen.getByText(/Usage resets August 18, 2026/)).toBeInTheDocument();
      expect(screen.queryByText(/Free allowances|Renews/)).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: /Manage subscription|Update payment/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("asks signed-out visitors to authenticate", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderBilling({ loggedIn: false, authEnabled: true, onLogin });

    expect(screen.getByRole("heading", { name: "Sign in to view billing" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onLogin).toHaveBeenCalledTimes(1);
  });

  it("shows active paid-plan dates, allowance progress, and portal management", async () => {
    const user = userEvent.setup();
    const onPortal = vi.fn();
    renderBilling({
      loggedIn: true,
      subscription: subscriptionResponse("plus"),
      entitlements: entitlementsResponse("plus"),
      onPortal,
    });

    expect(screen.getByText("PLUS PLAN")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Renews August 18, 2026")).toBeInTheDocument();
    expect(screen.getByText("124 / 1,000")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "AI credits" })).toHaveAttribute(
      "aria-valuenow",
      "124",
    );

    await user.click(screen.getByRole("button", { name: "Manage subscription" }));
    expect(onPortal).toHaveBeenCalledTimes(1);
  });

  it("renders Free and billing-disabled states without a paid action", () => {
    renderBilling({
      loggedIn: true,
      plans: plansResponse(false),
      subscription: subscriptionResponse("free"),
      entitlements: entitlementsResponse("free"),
    });

    expect(screen.getByText("FREE PLAN")).toBeInTheDocument();
    expect(screen.getByText(/online billing is currently unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Billing unavailable" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Manage subscription" })).not.toBeInTheDocument();
  });

  it("keeps the backend-authorized Portal action visible when can_manage is true", () => {
    renderBilling({
      loggedIn: true,
      plans: plansResponse(false),
      subscription: { ...subscriptionResponse("plus"), can_manage: true },
      entitlements: entitlementsResponse("plus"),
    });

    expect(screen.getByRole("button", { name: "Manage subscription" })).toBeInTheDocument();
  });

  it("shows the grace deadline and Update payment action for past-due plans", async () => {
    const user = userEvent.setup();
    const onPortal = vi.fn();
    renderBilling({
      loggedIn: true,
      subscription: subscriptionResponse("plus", "past_due"),
      entitlements: entitlementsResponse("plus", "past_due"),
      onPortal,
    });

    expect(screen.getByRole("alert")).toHaveTextContent("before July 21, 2026");
    await user.click(screen.getByRole("button", { name: "Update payment method" }));
    expect(onPortal).toHaveBeenCalledTimes(1);
  });

  it.each(["past_due", "unpaid", "incomplete"] as const)(
    "keeps payment recovery visible for effective-Free %s subscriptions",
    async (status) => {
      const user = userEvent.setup();
      const onPortal = vi.fn();
      renderBilling({
        loggedIn: true,
        subscription: {
          ...subscriptionResponse("free", status),
          provider: "stripe",
          can_manage: true,
        },
        entitlements: entitlementsResponse("free", status),
        onPortal,
      });

      expect(screen.getByText("FREE PLAN")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Update payment method" }));
      expect(onPortal).toHaveBeenCalledTimes(1);
    },
  );

  it("shows subscription management for active Pro and scheduled cancellation", () => {
    const { rerender } = renderBilling({
      loggedIn: true,
      subscription: subscriptionResponse("pro"),
      entitlements: entitlementsResponse("pro"),
    });
    expect(screen.getByRole("button", { name: "Manage subscription" })).toBeInTheDocument();

    rerender(
      billingElement({
        loggedIn: true,
        subscription: { ...subscriptionResponse("pro"), cancel_at_period_end: true },
        entitlements: entitlementsResponse("pro", "active", true),
      }),
    );
    expect(screen.getByRole("button", { name: "Manage subscription" })).toBeInTheDocument();
  });

  it("renders cancellation-at-period-end and fully cancelled lifecycle states", () => {
    const { rerender } = renderBilling({
      loggedIn: true,
      subscription: { ...subscriptionResponse("plus"), cancel_at_period_end: true },
      entitlements: entitlementsResponse("plus", "active", true),
    });
    expect(
      screen.getByText("Your Plus plan remains active until August 18, 2026."),
    ).toBeInTheDocument();

    rerender(
      billingElement({
        loggedIn: true,
        subscription: subscriptionResponse("free", "canceled"),
        entitlements: entitlementsResponse("free", "canceled"),
      }),
    );
    expect(screen.getByText(/paid subscription has ended/i)).toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });
});

function renderPricing(overrides: Partial<React.ComponentProps<typeof PricingPageContent>> = {}) {
  return render(pricingElement(overrides));
}

function pricingElement(overrides: Partial<React.ComponentProps<typeof PricingPageContent>> = {}) {
  return <PricingPageContent {...pricingProps(overrides)} />;
}

function pricingProps(overrides: Partial<React.ComponentProps<typeof PricingPageContent>> = {}) {
  return {
    plans: plansResponse(),
    subscription: null,
    entitlements: null,
    loading: false,
    action: null,
    error: null,
    checkoutConfirmation: "idle" as const,
    loggedIn: false,
    authEnabled: true,
    onLogin: vi.fn(),
    onCheckout: vi.fn(),
    onPortal: vi.fn(),
    onClearError: vi.fn(),
    ...overrides,
  };
}

function renderBilling(overrides: Partial<React.ComponentProps<typeof BillingPageContent>> = {}) {
  return render(billingElement(overrides));
}

function billingElement(overrides: Partial<React.ComponentProps<typeof BillingPageContent>> = {}) {
  return <BillingPageContent {...billingProps(overrides)} />;
}

function billingProps(overrides: Partial<React.ComponentProps<typeof BillingPageContent>> = {}) {
  return {
    plans: plansResponse(),
    subscription: null,
    entitlements: null,
    loading: false,
    action: null,
    error: null,
    checkoutConfirmation: "idle" as const,
    loggedIn: false,
    authEnabled: true,
    onLogin: vi.fn(),
    onPortal: vi.fn(),
    onViewPlans: vi.fn(),
    onClearError: vi.fn(),
    ...overrides,
  };
}

function planCard(name: string): HTMLElement {
  return screen.getByRole("heading", { name }).closest("article") as HTMLElement;
}

function plansResponse(billingEnabled = true): BillingPlansResponse {
  return {
    currency: "USD",
    billing_period: "monthly",
    billing_enabled: billingEnabled,
    plans: (
      [
        ["free", "Free", 0, false, 100_000, 2],
        ["plus", "Plus", 6.99, true, 1_000_000, 2],
        ["pro", "Pro", 12.99, false, 3_000_000, 3],
      ] as const
    ).map(([code, displayName, price, recommended, credits, compare]) => ({
      code,
      display_name: displayName,
      monthly_price: price,
      recommended,
      features: {
        max_compare_models: compare,
        research_enabled: true,
        prompt_improvement_enabled: true,
        file_analysis_enabled: true,
        work_enabled: code !== "free",
        verified_connectors_enabled: code !== "free",
        custom_mcp_enabled: code === "pro",
        action_tools_enabled: code !== "free",
        max_active_work_runs: code === "pro" ? 3 : code === "plus" ? 1 : 0,
        allowed_billing_classes:
          code === "pro"
            ? ["economical", "standard", "advanced", "premium"]
            : code === "plus"
              ? ["economical", "standard", "advanced"]
              : ["economical", "standard"],
      },
      allowances: {
        ai_credits: credits,
      },
    })),
  };
}

function subscriptionResponse(
  planCode: SubscriptionPlanCode,
  status: SubscriptionStatus = planCode === "free" ? "free" : "active",
): BillingSubscriptionResponse {
  return {
    plan_code: planCode,
    status,
    provider: planCode === "free" ? null : "stripe",
    current_period_start: "2026-07-18T00:00:00Z",
    current_period_end: "2026-08-18T00:00:00Z",
    cancel_at_period_end: false,
    can_manage: planCode !== "free",
  };
}

function entitlementsResponse(
  planCode: SubscriptionPlanCode,
  status: SubscriptionStatus = planCode === "free" ? "free" : "active",
  cancelAtPeriodEnd = false,
): EntitlementsResponse {
  const creditLimit = planCode === "free" ? 100_000 : planCode === "plus" ? 1_000_000 : 3_000_000;
  const counters = (used: number, limit: number) => ({
    used,
    reserved: 0,
    limit,
    remaining: Math.max(0, limit - used),
  });
  return {
    plan: {
      code: planCode,
      display_name: planCode === "free" ? "Free" : planCode === "plus" ? "Plus" : "Pro",
      status,
      source: planCode === "free" ? "default" : "stripe",
      renews_at: "2026-08-18T00:00:00Z",
      cancel_at_period_end: cancelAtPeriodEnd,
      grace_until: status === "past_due" ? "2026-07-21T00:00:00Z" : null,
    },
    features: {
      compare_enabled: true,
      max_compare_models: planCode === "pro" ? 3 : 2,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      usage_export_enabled: planCode !== "free",
      saved_history_enabled: true,
      models_catalog_enabled: true,
    },
    model_access: {
      allowed_billing_classes:
        planCode === "pro"
          ? ["economical", "standard", "advanced", "premium"]
          : planCode === "plus"
            ? ["economical", "standard", "advanced"]
            : ["economical", "standard"],
    },
    limits: {
      max_files_per_request: planCode === "free" ? 1 : 10,
      max_file_bytes: planCode === "free" ? 10_000_000 : 25_000_000,
    },
    allowances: {
      ai_credits: counters(planCode === "plus" ? 124_000 : 4_000, creditLimit),
    },
    period: {
      starts_at: "2026-07-18T00:00:00Z",
      ends_at: "2026-08-18T00:00:00Z",
    },
  };
}
