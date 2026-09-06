import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createCheckoutSession,
  createPortalSession,
  fetchPlans,
  fetchSubscription,
} from "../api/billing";
import { fetchCreditTransactions, fetchEntitlements } from "../api/entitlements";
import { checkoutReturnFromLocation, useSubscription } from "../hooks/useSubscription";
import { toSubscriptionError } from "../subscription/subscriptionErrors";
import type {
  BillingPlansResponse,
  BillingSubscriptionResponse,
  EntitlementsResponse,
  SubscriptionPlanCode,
} from "../types";

describe("subscription data layer", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("uses only the server-owned billing and entitlement endpoints", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/v1/billing/plans") return jsonResponse(plansResponse());
      if (path === "/v1/billing/subscription") return jsonResponse(subscriptionResponse());
      if (path === "/v1/entitlements") return jsonResponse(entitlementsResponse());
      if (path === "/v1/credits/transactions?limit=20&offset=0") {
        return jsonResponse({
          items: [
            {
              id: "credit-1",
              request_id: "request-1",
              operation_type: "ask",
              item_type: "model",
              provider: "openai",
              model: "gpt-4.1-mini",
              input_tokens: 100,
              output_tokens: 50,
              input_credits: 100,
              output_credits: 200,
              fixed_credits: 0,
              total_credits: 300,
              provider_cost_usd: 0.001,
              usage_estimated: false,
              pricing_version: "2026-07-29",
              metadata: {},
              created_at: "2026-07-29T12:00:00Z",
            },
          ],
          limit: 20,
          offset: 0,
        });
      }
      if (path === "/v1/billing/checkout-session") {
        expect(JSON.parse(String(init?.body))).toEqual({
          plan_code: "plus",
          billing_period: "monthly",
        });
        return jsonResponse({
          checkout_url: "https://checkout.stripe.com/c/pay/unit",
          destination: "checkout",
        });
      }
      if (path === "/v1/billing/portal-session") {
        expect(JSON.parse(String(init?.body))).toEqual({});
        return jsonResponse({ portal_url: "https://billing.stripe.com/p/session/unit" });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchPlans()).resolves.toEqual(plansResponse());
    await expect(fetchSubscription()).resolves.toEqual(subscriptionResponse());
    await expect(fetchEntitlements()).resolves.toEqual(entitlementsResponse());
    await expect(fetchCreditTransactions()).resolves.toMatchObject({
      items: [{ total_credits: 300 }],
    });
    await expect(createCheckoutSession("plus")).resolves.toMatchObject({
      destination: "checkout",
    });
    await expect(createPortalSession()).resolves.toMatchObject({
      portal_url: "https://billing.stripe.com/p/session/unit",
    });

    expect(fetchMock).toHaveBeenCalledTimes(6);
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toEqual(expect.objectContaining({ credentials: "include" }));
    }
  });

  it("parses structured subscription errors into typed UI-safe errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        errorResponse(402, {
          code: "insufficient_credits",
          message: "This request needs 15,000 AI credits, but only 2,000 remain.",
          meter: "ai_credits",
          required: 15_000,
          remaining: 2_000,
          current_plan: "free",
          recommended_plan: "plus",
        }),
      ),
    );

    const rawError = await fetchEntitlements().catch((error: unknown) => error);
    const error = toSubscriptionError(rawError);

    expect(error.name).toBe("SubscriptionError");
    expect(error.code).toBe("insufficient_credits");
    expect(error.kind).toBe("allowance");
    expect(error.status).toBe(402);
    expect(error.retryable).toBe(false);
    expect(error.message).toBe(
      "This request is estimated to require 15 AI credits. You have 2 remaining.",
    );
    expect(error.details).toMatchObject({
      meter: "ai_credits",
      required: 15_000,
      remaining: 2_000,
      current_plan: "free",
      recommended_plan: "plus",
    });
  });

  it("treats checkout return parameters as refresh hints only", () => {
    window.history.replaceState({}, "", "/account/billing?checkout=success");
    expect(checkoutReturnFromLocation()).toBe("success");

    window.history.replaceState({}, "", "/account/billing?checkout=canceled");
    expect(checkoutReturnFromLocation()).toBe("cancelled");

    window.history.replaceState({}, "", "/account/billing?checkout=unknown");
    expect(checkoutReturnFromLocation()).toBeNull();
  });

  it("waits for auth bootstrap and never calls authenticated endpoints when signed out", async () => {
    const fetchMock = subscriptionFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(
      ({ authLoading }) => useSubscription({ authLoading, loggedIn: false, checkoutReturn: null }),
      { initialProps: { authLoading: true } },
    );

    expect(result.current.loading).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();

    rerender({ authLoading: false });
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.plans?.plans).toHaveLength(3);
    expect(result.current.entitlements).toBeNull();
    expect(result.current.subscription).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/billing/plans",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
  });

  it("uses backend entitlements instead of browser storage as billing authority", async () => {
    window.localStorage.setItem(
      "cortex.subscription",
      JSON.stringify({ plan_code: "pro", status: "active" }),
    );
    const fetchMock = subscriptionFetchMock({ planCode: "free" });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useSubscription({ authLoading: false, loggedIn: true, checkoutReturn: null }),
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.entitlements?.plan.code).toBe("free");
    expect(result.current.canUseModel("premium")).toBe(false);
    expect(result.current.canCompareTargets(3)).toBe(false);
    expect(result.current.canUseResearch).toBe(true);
  });

  it("reloads subscription snapshots explicitly without browser persistence", async () => {
    let currentPlan: SubscriptionPlanCode = "free";
    const fetchMock = subscriptionFetchMock({ plan: () => currentPlan });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() =>
      useSubscription({ authLoading: false, loggedIn: true, checkoutReturn: null }),
    );
    await waitFor(() => expect(result.current.entitlements?.plan.code).toBe("free"));

    currentPlan = "plus";
    act(() => result.current.reload());

    await waitFor(() => expect(result.current.entitlements?.plan.code).toBe("plus"));
    expect(result.current.subscription?.plan_code).toBe("plus");
    expect(result.current.lastLoadedAt).not.toBeNull();
  });

  it("polls checkout success until webhook-backed entitlements report a paid plan", async () => {
    let entitlementCalls = 0;
    const fetchMock = subscriptionFetchMock({
      entitlementPlan: () => (++entitlementCalls >= 2 ? "plus" : "free"),
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useSubscription({
        authLoading: false,
        loggedIn: true,
        checkoutReturn: "success",
        checkoutPollIntervalMs: 1,
        checkoutPollMaxAttempts: 3,
      }),
    );

    expect(result.current.checkoutConfirmation).toBe("confirming");
    await waitFor(() => expect(result.current.checkoutConfirmation).toBe("confirmed"));

    expect(entitlementCalls).toBe(2);
    expect(result.current.entitlements?.plan.code).toBe("plus");
    expect(result.current.loading).toBe(false);
  });

  it("stops checkout polling with a safe pending state when confirmation lags", async () => {
    let entitlementCalls = 0;
    const fetchMock = subscriptionFetchMock({
      entitlementPlan: () => {
        entitlementCalls += 1;
        return "free";
      },
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      useSubscription({
        authLoading: false,
        loggedIn: true,
        checkoutReturn: "success",
        checkoutPollIntervalMs: 1,
        checkoutPollMaxAttempts: 2,
      }),
    );

    await waitFor(() => expect(result.current.checkoutConfirmation).toBe("pending"));
    expect(entitlementCalls).toBe(2);
    expect(result.current.entitlements?.plan.code).toBe("free");
    expect(result.current.error).toBeNull();
  });

  it("does not treat Cortex-granted access as a confirmed Stripe payment", async () => {
    const baseFetch = subscriptionFetchMock({ planCode: "pro" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (...args: Parameters<typeof fetch>) => {
        const response = await baseFetch(args[0]);
        if (String(args[0]).includes("/v1/entitlements")) {
          const payload = await response.json();
          payload.plan.source = "cortex_grant";
          return new Response(JSON.stringify(payload), { status: 200 });
        }
        return response;
      }),
    );
    const { result } = renderHook(() =>
      useSubscription({
        authLoading: false,
        loggedIn: true,
        checkoutReturn: "success",
        checkoutPollIntervalMs: 1,
        checkoutPollMaxAttempts: 1,
      }),
    );
    await waitFor(() => expect(result.current.checkoutConfirmation).toBe("pending"));
    expect(result.current.entitlements?.plan.code).toBe("pro");
  });

  it("follows only validated server-returned hosted billing URLs", async () => {
    const navigate = vi.fn();
    const fetchMock = subscriptionFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() =>
      useSubscription({
        authLoading: false,
        loggedIn: true,
        checkoutReturn: null,
        navigateToHostedBilling: navigate,
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => result.current.startCheckout("plus"));
    expect(navigate).toHaveBeenLastCalledWith("https://checkout.stripe.com/c/pay/unit");

    await act(async () => result.current.openPortal());
    expect(navigate).toHaveBeenLastCalledWith("https://billing.stripe.com/p/session/unit");
    expect(result.current.error).toBeNull();
  });

  it("rejects unsafe hosted redirects with a typed configuration error", async () => {
    const navigate = vi.fn();
    const fetchMock = subscriptionFetchMock({ checkoutUrl: "javascript:alert(1)" });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() =>
      useSubscription({
        authLoading: false,
        loggedIn: true,
        checkoutReturn: null,
        navigateToHostedBilling: navigate,
      }),
    );
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => result.current.startCheckout("plus"));

    expect(navigate).not.toHaveBeenCalled();
    expect(result.current.error).toMatchObject({
      code: "invalid_billing_redirect",
      kind: "configuration",
      retryable: false,
    });
  });
});

interface SubscriptionFetchOptions {
  planCode?: SubscriptionPlanCode;
  plan?: () => SubscriptionPlanCode;
  entitlementPlan?: () => SubscriptionPlanCode;
  checkoutUrl?: string;
}

function subscriptionFetchMock(options: SubscriptionFetchOptions = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/v1/billing/plans") return jsonResponse(plansResponse());
    if (path === "/v1/billing/subscription") {
      return jsonResponse(subscriptionResponse(options.plan?.() ?? options.planCode ?? "free"));
    }
    if (path === "/v1/entitlements") {
      return jsonResponse(
        entitlementsResponse(
          options.entitlementPlan?.() ?? options.plan?.() ?? options.planCode ?? "free",
        ),
      );
    }
    if (path === "/v1/billing/checkout-session") {
      return jsonResponse({
        checkout_url: options.checkoutUrl ?? "https://checkout.stripe.com/c/pay/unit",
        destination: "checkout",
      });
    }
    if (path === "/v1/billing/portal-session") {
      return jsonResponse({ portal_url: "https://billing.stripe.com/p/session/unit" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, detail: Record<string, unknown>): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    statusText: "Request failed",
    headers: { "Content-Type": "application/json" },
  });
}

function plansResponse(): BillingPlansResponse {
  return {
    currency: "USD",
    billing_period: "monthly",
    billing_enabled: true,
    plans: (
      [
        ["free", "Free", 0, false, ["economical", "standard"]],
        ["plus", "Plus", 6.99, true, ["economical", "standard", "advanced"]],
        ["pro", "Pro", 12.99, false, ["economical", "standard", "advanced", "premium"]],
      ] as const
    ).map(([code, displayName, monthlyPrice, recommended, billingClasses]) => ({
      code,
      display_name: displayName,
      monthly_price: monthlyPrice,
      recommended,
      features: {
        max_compare_models: code === "pro" ? 3 : 2,
        research_enabled: true,
        prompt_improvement_enabled: true,
        file_analysis_enabled: true,
        allowed_billing_classes: [...billingClasses],
      },
      allowances: {
        ai_credits: code === "free" ? 100_000 : code === "plus" ? 1_000_000 : 3_000_000,
      },
    })),
  };
}

function subscriptionResponse(
  planCode: SubscriptionPlanCode = "free",
): BillingSubscriptionResponse {
  return {
    plan_code: planCode,
    status: planCode === "free" ? "free" : "active",
    provider: planCode === "free" ? null : "stripe",
    current_period_start: "2026-07-01T00:00:00Z",
    current_period_end: "2026-08-01T00:00:00Z",
    cancel_at_period_end: false,
    can_manage: planCode !== "free",
  };
}

function entitlementsResponse(planCode: SubscriptionPlanCode = "free"): EntitlementsResponse {
  const paid = planCode !== "free";
  return {
    plan: {
      code: planCode,
      display_name: planCode === "free" ? "Free" : planCode === "plus" ? "Plus" : "Pro",
      status: paid ? "active" : "free",
      source: paid ? "stripe" : "free_default",
      renews_at: "2026-08-01T00:00:00Z",
      cancel_at_period_end: false,
      grace_until: null,
    },
    features: {
      compare_enabled: true,
      max_compare_models: planCode === "pro" ? 3 : 2,
      research_enabled: true,
      prompt_improvement_enabled: true,
      file_analysis_enabled: true,
      usage_export_enabled: true,
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
      max_files_per_request: paid ? 10 : 1,
      max_file_bytes: paid ? 25_000_000 : 10_000_000,
    },
    allowances: {
      ai_credits: {
        used: 1,
        reserved: 0,
        limit: paid ? 1_000_000 : 100_000,
        remaining: paid ? 999_999 : 99_999,
      },
    },
    period: {
      starts_at: "2026-07-01T00:00:00Z",
      ends_at: "2026-08-01T00:00:00Z",
    },
  };
}
