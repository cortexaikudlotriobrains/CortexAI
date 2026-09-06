import { useMemo } from "react";
import { SubscriptionPageShell } from "../components/subscription/SubscriptionPageShell";
import { CortexIcon } from "../components/shared/CortexIcon";
import { useAuth } from "../hooks/useAuth";
import {
  useSubscription,
  type CheckoutConfirmationStatus,
  type HostedBillingAction,
} from "../hooks/useSubscription";
import type { SubscriptionError } from "../subscription/subscriptionErrors";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import type {
  BillingPlansResponse,
  BillingSubscriptionResponse,
  EntitlementsResponse,
  PublicBillingPlan,
  SubscriptionPlanCode,
  SubscriptionStatus,
} from "../types";
import { formatAiCredits } from "../utils/aiCredits";
import styles from "./PricingPage.module.css";

interface PricingPageContentProps {
  plans: BillingPlansResponse | null;
  subscription: BillingSubscriptionResponse | null;
  entitlements: EntitlementsResponse | null;
  loading: boolean;
  action: HostedBillingAction;
  error: SubscriptionError | null;
  checkoutConfirmation: CheckoutConfirmationStatus;
  loggedIn: boolean;
  authEnabled: boolean;
  onLogin: () => void;
  onCheckout: (planCode: SubscriptionPlanCode) => void;
  onPortal: () => void;
  onClearError: () => void;
}

interface PlanAction {
  label: string;
  disabled: boolean;
  kind: "primary" | "secondary";
  onClick?: () => void;
}

export function PricingPage() {
  const { whoAmI, cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const authEnabled = cognitoConfig?.enabled ?? false;
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );

  return (
    <SubscriptionPageShell
      title="Plans"
      subtitle="Choose the access and allowances that fit your work"
      authLoading={authLoading}
      authEnabled={authEnabled}
      loggedIn={loggedIn}
      whoAmI={whoAmI}
      onLogin={login}
      onLogout={logout}
      planLabel={loggedIn ? accountSubscription.planLabel : undefined}
      billingActionLabel={loggedIn ? accountSubscription.billingActionLabel : undefined}
      billingPastDue={accountSubscription.billingPastDue}
      billingDestination={accountSubscription.billingDestination}
    >
      <PricingPageContent
        plans={subscriptionState.plans}
        subscription={subscriptionState.subscription}
        entitlements={subscriptionState.entitlements}
        loading={subscriptionState.loading}
        action={subscriptionState.action}
        error={subscriptionState.error}
        checkoutConfirmation={subscriptionState.checkoutConfirmation}
        loggedIn={loggedIn}
        authEnabled={authEnabled}
        onLogin={login}
        onCheckout={(planCode) => void subscriptionState.startCheckout(planCode)}
        onPortal={() => void subscriptionState.openPortal()}
        onClearError={subscriptionState.clearError}
      />
    </SubscriptionPageShell>
  );
}

export function PricingPageContent({
  plans,
  subscription,
  entitlements,
  loading,
  action,
  error,
  checkoutConfirmation,
  loggedIn,
  authEnabled,
  onLogin,
  onCheckout,
  onPortal,
  onClearError,
}: PricingPageContentProps) {
  const currentPlanCode = entitlements?.plan.code ?? subscription?.plan_code ?? null;
  const currentStatus = entitlements?.plan.status ?? subscription?.status;
  const billingEnabled = plans?.billing_enabled ?? false;
  const orderedPlans = useMemo(
    () => [...(plans?.plans ?? [])].sort((left, right) => left.monthly_price - right.monthly_price),
    [plans],
  );

  return (
    <div className={styles.page}>
      <section className={styles.hero} aria-labelledby="pricing-title">
        <span className={styles.eyebrow}>MONTHLY PLANS</span>
        <h1 id="pricing-title">More choice, with clear monthly limits.</h1>
        <p>
          Start free, then move up when you need more AI credits, broader model access, or
          three-model comparisons.
        </p>
      </section>

      <CheckoutNotice status={checkoutConfirmation} />

      {isPaymentPastDue(currentStatus) ? (
        <div className={`${styles.notice} ${styles.noticeWarning}`} role="alert">
          <CortexIcon name="cost" size={18} />
          <span>Your payment needs attention. Use Update payment to keep paid access.</span>
        </div>
      ) : null}

      {isCancelled(currentStatus) ? (
        <div className={styles.notice} role="status">
          <CortexIcon name="history" size={18} />
          <span>Your paid subscription has ended. You can choose a new plan at any time.</span>
        </div>
      ) : null}

      {error ? (
        <div className={`${styles.notice} ${styles.noticeError}`} role="alert">
          <span>{error.message}</span>
          <button type="button" onClick={onClearError}>
            Dismiss
          </button>
        </div>
      ) : null}

      {loading && orderedPlans.length === 0 ? (
        <div className={styles.loadingPanel} role="status">
          Loading plans…
        </div>
      ) : null}

      {!loading && orderedPlans.length === 0 ? (
        <div className={styles.loadingPanel} role="alert">
          Plan information is temporarily unavailable. Try again shortly.
        </div>
      ) : null}

      {orderedPlans.length > 0 ? (
        <section className={styles.planGrid} aria-label="Subscription plans">
          {orderedPlans.map((plan) => {
            const isCurrent = loggedIn && plan.code === currentPlanCode;
            const planAction = resolvePlanAction({
              plan,
              currentPlanCode,
              currentStatus,
              billingEnabled,
              canManage: subscription?.can_manage ?? false,
              loggedIn,
              authEnabled,
              action,
              onLogin,
              onCheckout,
              onPortal,
            });
            return (
              <article
                key={plan.code}
                className={`${styles.planCard} ${plan.recommended ? styles.planCardRecommended : ""} ${isCurrent ? styles.planCardCurrent : ""}`}
              >
                <div className={styles.badgeRow}>
                  {plan.recommended ? (
                    <span className={styles.recommendedBadge}>RECOMMENDED</span>
                  ) : (
                    <span />
                  )}
                  {isCurrent ? <span className={styles.currentBadge}>CURRENT PLAN</span> : null}
                </div>

                <div className={styles.planHeading}>
                  <div>
                    <h2>{plan.display_name}</h2>
                    <p>{planSummary(plan.code)}</p>
                  </div>
                  <div className={styles.price}>
                    <strong>{formatPrice(plan.monthly_price)}</strong>
                    <span>/ month</span>
                  </div>
                </div>

                <ul className={styles.featureList}>
                  {planFeatures(plan).map((feature) => (
                    <li key={feature}>
                      <CortexIcon name="check" size={16} strokeWidth={2.2} />
                      <span>{feature}</span>
                    </li>
                  ))}
                </ul>

                <button
                  type="button"
                  className={
                    planAction.kind === "primary" ? styles.primaryButton : styles.secondaryButton
                  }
                  disabled={planAction.disabled}
                  onClick={planAction.onClick}
                >
                  {action && !planAction.disabled ? "Opening…" : planAction.label}
                </button>
              </article>
            );
          })}
        </section>
      ) : null}

      <section className={styles.disclosure} aria-label="Plan details">
        <h2>Good to know</h2>
        <ul>
          <li>Plans are monthly subscriptions and usage resets each billing period.</li>
          <li>Model availability can change as providers update their services.</li>
          <li>Allowances are defined limits; no plan promises unlimited usage.</li>
          <li>
            Taxes may apply. Cancellation and payment details are managed in the billing portal.
          </li>
        </ul>
      </section>
    </div>
  );
}

function CheckoutNotice({ status }: { status: CheckoutConfirmationStatus }) {
  if (status === "idle") return null;
  const copy = {
    cancelled: "Checkout was cancelled. Your existing plan has not changed.",
    confirming: "Payment received. Waiting for the verified subscription update…",
    confirmed: "Your paid plan is active and ready to use.",
    pending: "Payment is still being confirmed. Your current access remains unchanged for now.",
  }[status];
  const tone =
    status === "confirmed"
      ? styles.noticeSuccess
      : status === "pending"
        ? styles.noticeWarning
        : "";
  return (
    <div className={`${styles.notice} ${tone}`} role="status">
      {copy}
    </div>
  );
}

function resolvePlanAction({
  plan,
  currentPlanCode,
  currentStatus,
  billingEnabled,
  canManage,
  loggedIn,
  authEnabled,
  action,
  onLogin,
  onCheckout,
  onPortal,
}: {
  plan: PublicBillingPlan;
  currentPlanCode: SubscriptionPlanCode | null;
  currentStatus?: SubscriptionStatus;
  billingEnabled: boolean;
  canManage: boolean;
  loggedIn: boolean;
  authEnabled: boolean;
  action: HostedBillingAction;
  onLogin: () => void;
  onCheckout: (planCode: SubscriptionPlanCode) => void;
  onPortal: () => void;
}): PlanAction {
  if (loggedIn && currentPlanCode === plan.code && (!billingEnabled || !canManage)) {
    return { label: "Current plan", disabled: true, kind: "secondary" };
  }
  if (!billingEnabled) return { label: "Unavailable", disabled: true, kind: "secondary" };
  if (!loggedIn) {
    return {
      label: "Sign in to choose",
      disabled: !authEnabled,
      kind: plan.recommended ? "primary" : "secondary",
      onClick: authEnabled ? onLogin : undefined,
    };
  }
  if (isPaymentPastDue(currentStatus)) {
    return {
      label: "Update payment",
      disabled: action !== null || !canManage,
      kind: "primary",
      onClick: canManage ? onPortal : undefined,
    };
  }
  if (currentPlanCode === plan.code && plan.code === "free") {
    return { label: "Current plan", disabled: true, kind: "secondary" };
  }
  if (currentPlanCode && currentPlanCode !== "free") {
    if (!canManage) {
      return {
        label: currentPlanCode === plan.code ? "Current plan" : "Unavailable",
        disabled: true,
        kind: "secondary",
      };
    }
    return {
      label: currentPlanCode === plan.code ? "Manage current plan" : "Manage plan",
      disabled: action !== null,
      kind: currentPlanCode === plan.code ? "primary" : "secondary",
      onClick: onPortal,
    };
  }
  if (plan.code === "free") {
    return { label: "Current plan", disabled: true, kind: "secondary" };
  }
  return {
    label: "Upgrade",
    disabled: action !== null,
    kind: plan.recommended ? "primary" : "secondary",
    onClick: () => onCheckout(plan.code),
  };
}

function planFeatures(plan: PublicBillingPlan): string[] {
  const allowances = plan.allowances;
  const classes = plan.features.allowed_billing_classes;
  const workFeatures = plan.features.work_enabled
    ? [
        `CortexAI Work with up to ${plan.features.max_active_work_runs} active ${plan.features.max_active_work_runs === 1 ? "run" : "runs"}`,
        plan.features.custom_mcp_enabled
          ? "Verified connectors and custom MCP servers"
          : "Verified Work connectors",
        plan.features.action_tools_enabled ? "Approval-gated Work actions" : "Read-only Work tools",
      ]
    : ["Core Chat and Compare access"];
  return [
    `${formatAiCredits(allowances.ai_credits)} AI credits per month`,
    `Compare up to ${plan.features.max_compare_models} models`,
    ...workFeatures,
    "Advanced Web Search draws from AI credits",
    "Improve Prompt draws from AI credits",
    "File upload is free; model processing uses AI credits",
    classes.includes("premium")
      ? "Premium model access"
      : classes.includes("advanced")
        ? "Advanced model access"
        : "Economical and selected standard models",
  ];
}

function planSummary(code: SubscriptionPlanCode): string {
  if (code === "plus") return "For regular research and creation";
  if (code === "pro") return "For high-volume and premium-model work";
  return "For trying CortexAI and occasional work";
}

function formatPrice(value: number): string {
  return value === 0 ? "$0" : `$${value.toFixed(2)}`;
}

function isPaymentPastDue(status?: SubscriptionStatus): boolean {
  return status === "past_due" || status === "unpaid" || status === "incomplete";
}

function isCancelled(status?: SubscriptionStatus): boolean {
  return status === "canceled" || status === "incomplete_expired";
}
