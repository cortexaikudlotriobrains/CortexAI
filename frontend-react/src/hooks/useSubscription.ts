import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createCheckoutSession,
  createPortalSession,
  fetchPlans,
  fetchSubscription,
} from "../api/billing";
import { fetchEntitlements } from "../api/entitlements";
import {
  isAbortError,
  SubscriptionError,
  toSubscriptionError,
} from "../subscription/subscriptionErrors";
import type {
  BillingPlansResponse,
  BillingSubscriptionResponse,
  EntitlementsResponse,
  ModelBillingClass,
  SubscriptionPlanCode,
} from "../types";

const DEFAULT_CHECKOUT_POLL_INTERVAL_MS = 1_500;
const DEFAULT_CHECKOUT_POLL_MAX_ATTEMPTS = 10;

export type CheckoutReturnStatus = "success" | "cancelled" | null;
export type CheckoutConfirmationStatus =
  | "idle"
  | "cancelled"
  | "confirming"
  | "confirmed"
  | "pending";
export type HostedBillingAction = "checkout" | "portal" | null;

export interface UseSubscriptionOptions {
  authLoading: boolean;
  loggedIn: boolean;
  checkoutReturn?: CheckoutReturnStatus;
  checkoutPollIntervalMs?: number;
  checkoutPollMaxAttempts?: number;
  navigateToHostedBilling?: (url: string) => void;
}

export interface UseSubscriptionResult {
  plans: BillingPlansResponse | null;
  subscription: BillingSubscriptionResponse | null;
  entitlements: EntitlementsResponse | null;
  loading: boolean;
  action: HostedBillingAction;
  error: SubscriptionError | null;
  lastLoadedAt: number | null;
  checkoutConfirmation: CheckoutConfirmationStatus;
  reload: () => void;
  clearError: () => void;
  canUseModel: (billingClass: ModelBillingClass) => boolean;
  canCompareTargets: (targetCount: number) => boolean;
  canUseResearch: boolean;
  canUseImprove: boolean;
  canUseFiles: boolean;
  startCheckout: (planCode: SubscriptionPlanCode) => Promise<void>;
  openPortal: () => Promise<void>;
}

interface SubscriptionState {
  plans: BillingPlansResponse | null;
  subscription: BillingSubscriptionResponse | null;
  entitlements: EntitlementsResponse | null;
  loading: boolean;
  action: HostedBillingAction;
  error: SubscriptionError | null;
  lastLoadedAt: number | null;
  checkoutConfirmation: CheckoutConfirmationStatus;
}

const INITIAL_STATE: SubscriptionState = {
  plans: null,
  subscription: null,
  entitlements: null,
  loading: true,
  action: null,
  error: null,
  lastLoadedAt: null,
  checkoutConfirmation: "idle",
};

export function useSubscription(options: UseSubscriptionOptions): UseSubscriptionResult {
  const {
    authLoading,
    loggedIn,
    checkoutPollIntervalMs = DEFAULT_CHECKOUT_POLL_INTERVAL_MS,
    checkoutPollMaxAttempts = DEFAULT_CHECKOUT_POLL_MAX_ATTEMPTS,
  } = options;
  const checkoutReturn = options.checkoutReturn ?? checkoutReturnFromLocation();
  const navigateToHostedBilling = options.navigateToHostedBilling ?? defaultHostedBillingNavigation;
  const [state, setState] = useState<SubscriptionState>(INITIAL_STATE);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (authLoading) {
      setState((previous) => ({ ...previous, loading: true }));
      return;
    }

    const controller = new AbortController();
    const { signal } = controller;

    const load = async () => {
      setState((previous) => ({
        ...previous,
        loading: true,
        error: null,
        subscription: loggedIn ? previous.subscription : null,
        entitlements: loggedIn ? previous.entitlements : null,
        checkoutConfirmation:
          checkoutReturn === "cancelled"
            ? "cancelled"
            : checkoutReturn === "success" && loggedIn
              ? "confirming"
              : "idle",
      }));

      let plans: BillingPlansResponse | null = null;
      let loadError: SubscriptionError | null = null;
      try {
        plans = await fetchPlans(signal);
      } catch (error: unknown) {
        if (isAbortError(error)) return;
        loadError = toSubscriptionError(error, "Subscription plans could not be loaded.");
      }

      if (!loggedIn) {
        if (signal.aborted) return;
        setState((previous) => ({
          ...previous,
          plans,
          subscription: null,
          entitlements: null,
          loading: false,
          error: loadError,
          lastLoadedAt: Date.now(),
        }));
        return;
      }

      const maxAttempts = Math.max(1, Math.floor(checkoutPollMaxAttempts));
      const shouldPoll = checkoutReturn === "success";
      for (let attempt = 1; attempt <= (shouldPoll ? maxAttempts : 1); attempt += 1) {
        try {
          const [subscription, entitlements] = await Promise.all([
            fetchSubscription(signal),
            fetchEntitlements(signal),
          ]);
          if (signal.aborted) return;

          const confirmed =
            shouldPoll &&
            entitlements.plan.code !== "free" &&
            entitlements.plan.source === "stripe";
          const exhausted = shouldPoll && attempt === maxAttempts && !confirmed;
          setState((previous) => ({
            ...previous,
            plans,
            subscription,
            entitlements,
            loading: shouldPoll && !confirmed && !exhausted,
            error: loadError,
            lastLoadedAt: Date.now(),
            checkoutConfirmation: confirmed
              ? "confirmed"
              : exhausted
                ? "pending"
                : shouldPoll
                  ? "confirming"
                  : checkoutReturn === "cancelled"
                    ? "cancelled"
                    : "idle",
          }));

          if (!shouldPoll || confirmed || exhausted) return;
          await abortableDelay(Math.max(0, checkoutPollIntervalMs), signal);
        } catch (error: unknown) {
          if (isAbortError(error)) return;
          setState((previous) => ({
            ...previous,
            plans,
            loading: false,
            error: toSubscriptionError(error),
            checkoutConfirmation: shouldPoll ? "pending" : previous.checkoutConfirmation,
          }));
          return;
        }
      }
    };

    void load();
    return () => controller.abort();
  }, [
    authLoading,
    checkoutPollIntervalMs,
    checkoutPollMaxAttempts,
    checkoutReturn,
    loggedIn,
    reloadToken,
  ]);

  const reload = useCallback(() => {
    setReloadToken((current) => current + 1);
  }, []);

  const clearError = useCallback(() => {
    setState((previous) => ({ ...previous, error: null }));
  }, []);

  const canUseModel = useCallback(
    (billingClass: ModelBillingClass) =>
      state.entitlements?.model_access.allowed_billing_classes.includes(billingClass) ?? false,
    [state.entitlements],
  );

  const canCompareTargets = useCallback(
    (targetCount: number) =>
      Boolean(
        state.entitlements?.features.compare_enabled &&
        targetCount >= 2 &&
        targetCount <= state.entitlements.features.max_compare_models,
      ),
    [state.entitlements],
  );

  const startCheckout = useCallback(
    async (planCode: SubscriptionPlanCode) => {
      setState((previous) => ({ ...previous, action: "checkout", error: null }));
      try {
        const redirect = await createCheckoutSession(planCode);
        navigateToHostedBilling(validateHostedBillingUrl(redirect.checkout_url));
      } catch (error: unknown) {
        setState((previous) => ({
          ...previous,
          error: toSubscriptionError(error, "Checkout could not be started."),
        }));
      } finally {
        setState((previous) => ({ ...previous, action: null }));
      }
    },
    [navigateToHostedBilling],
  );

  const openPortal = useCallback(async () => {
    setState((previous) => ({ ...previous, action: "portal", error: null }));
    try {
      const redirect = await createPortalSession();
      navigateToHostedBilling(validateHostedBillingUrl(redirect.portal_url));
    } catch (error: unknown) {
      setState((previous) => ({
        ...previous,
        error: toSubscriptionError(error, "Billing management could not be opened."),
      }));
    } finally {
      setState((previous) => ({ ...previous, action: null }));
    }
  }, [navigateToHostedBilling]);

  return useMemo(
    () => ({
      ...state,
      reload,
      clearError,
      canUseModel,
      canCompareTargets,
      canUseResearch: state.entitlements?.features.research_enabled ?? false,
      canUseImprove: state.entitlements?.features.prompt_improvement_enabled ?? false,
      canUseFiles: state.entitlements?.features.file_analysis_enabled ?? false,
      startCheckout,
      openPortal,
    }),
    [canCompareTargets, canUseModel, clearError, openPortal, reload, startCheckout, state],
  );
}

export function checkoutReturnFromLocation(): CheckoutReturnStatus {
  const value = new URLSearchParams(window.location.search).get("checkout");
  if (value === "success") return "success";
  if (value === "cancelled" || value === "canceled") return "cancelled";
  return null;
}

function validateHostedBillingUrl(value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) throw new Error();
    return url.toString();
  } catch {
    throw new SubscriptionError({
      code: "invalid_billing_redirect",
      message: "The billing provider returned an invalid redirect.",
      status: null,
      kind: "configuration",
      retryable: false,
    });
  }
}

function defaultHostedBillingNavigation(url: string): void {
  window.location.assign(url);
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortException());
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeoutId);
        reject(abortException());
      },
      { once: true },
    );
  });
}

function abortException(): DOMException {
  return new DOMException("The subscription request was aborted.", "AbortError");
}
