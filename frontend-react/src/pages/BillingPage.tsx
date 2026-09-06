import { useNavigate } from "react-router-dom";
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
  AllowanceCounter,
  BillingPlansResponse,
  BillingSubscriptionResponse,
  EntitlementsResponse,
  SubscriptionMeterKey,
  SubscriptionStatus,
} from "../types";
import { formatAiCredits, toDisplayAiCredits } from "../utils/aiCredits";
import styles from "./BillingPage.module.css";

interface BillingPageContentProps {
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
  onPortal: () => void;
  onViewPlans: () => void;
  onClearError: () => void;
}

const USAGE_ROWS: Array<{ key: SubscriptionMeterKey; label: string }> = [
  { key: "ai_credits", label: "AI credits" },
];

export function BillingPage() {
  const navigate = useNavigate();
  const { whoAmI, cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const authEnabled = cognitoConfig?.enabled ?? false;
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );

  return (
    <SubscriptionPageShell
      title="Billing"
      subtitle="Plan status, allowance usage, and payment management"
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
      <BillingPageContent
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
        onPortal={() => void subscriptionState.openPortal()}
        onViewPlans={() => navigate("/pricing")}
        onClearError={subscriptionState.clearError}
      />
    </SubscriptionPageShell>
  );
}

export function BillingPageContent({
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
  onPortal,
  onViewPlans,
  onClearError,
}: BillingPageContentProps) {
  const billingEnabled = plans?.billing_enabled ?? false;
  const plan = entitlements?.plan;
  const status = plan?.status ?? subscription?.status;
  const paymentPastDue = isPaymentPastDue(status);
  const cancelled = isCancelled(status);
  const paidPlan = Boolean(plan && plan.code !== "free");
  const cortexGrant = plan?.source === "cortex_grant";
  const canManage = Boolean(!cortexGrant && subscription?.can_manage);
  const periodEnd = plan?.renews_at ?? subscription?.current_period_end ?? null;

  return (
    <div className={styles.page}>
      <section className={styles.heading} aria-labelledby="billing-title">
        <span>ACCOUNT</span>
        <h1 id="billing-title">Plan & billing</h1>
        <p>Your plan comes from verified server state. Checkout return links never grant access.</p>
      </section>

      <CheckoutNotice status={checkoutConfirmation} />

      {error ? (
        <div className={`${styles.banner} ${styles.bannerError}`} role="alert">
          <span>{error.message}</span>
          <button type="button" onClick={onClearError}>
            Dismiss
          </button>
        </div>
      ) : null}

      {!loggedIn && !loading ? (
        <section className={styles.signedOutCard} aria-label="Sign in required">
          <div className={styles.stateIcon}>
            <CortexIcon name="user" size={22} />
          </div>
          <h2>Sign in to view billing</h2>
          <p>Your plan, billing lifecycle, and usage allowances are tied to your account.</p>
          <button type="button" disabled={!authEnabled} onClick={authEnabled ? onLogin : undefined}>
            Sign in
          </button>
        </section>
      ) : null}

      {loggedIn && loading && !entitlements ? (
        <div className={styles.loadingPanel} role="status">
          Loading billing details…
        </div>
      ) : null}

      {loggedIn && entitlements && plan ? (
        <>
          {cortexGrant || !billingEnabled ? (
            <div className={`${styles.banner} ${styles.bannerNeutral}`} role="status">
              <CortexIcon name="cost" size={18} />
              <span>
                {cortexGrant
                  ? "Your plan access is provided by CortexAI. Online billing and payment management are not currently enabled for this access."
                  : "Online billing is currently unavailable. Your current plan allowances remain active."}
              </span>
            </div>
          ) : null}

          {paymentPastDue ? (
            <div className={`${styles.banner} ${styles.bannerWarning}`} role="alert">
              <CortexIcon name="cost" size={18} />
              <span>
                We could not renew your subscription. Update your payment method
                {plan?.grace_until ? ` before ${formatDate(plan.grace_until)}` : " soon"} to keep{" "}
                {plan?.display_name} access.
              </span>
            </div>
          ) : null}

          {plan?.cancel_at_period_end ? (
            <div className={`${styles.banner} ${styles.bannerWarning}`} role="status">
              <CortexIcon name="history" size={18} />
              <span>
                Your {plan.display_name} plan remains active until {formatDate(periodEnd)}.
              </span>
            </div>
          ) : null}

          {cancelled ? (
            <div className={styles.banner} role="status">
              <CortexIcon name="history" size={18} />
              <span>Your paid subscription has ended. Free plan access remains available.</span>
            </div>
          ) : null}

          <div className={styles.dashboardGrid}>
            <section className={styles.planCard} aria-label="Current subscription">
              <div className={styles.planCardTopline}>
                <span>{plan.display_name.toUpperCase()} PLAN</span>
                <strong className={statusClass(status)}>{formatStatus(status)}</strong>
              </div>
              <div className={styles.planMain}>
                <div>
                  <h2>{plan.display_name}</h2>
                  <p>
                    {cortexGrant
                      ? `Usage resets ${formatDate(periodEnd)}`
                      : periodCopy({
                          status,
                          paidPlan,
                          cancelAtPeriodEnd: plan.cancel_at_period_end,
                          periodEnd,
                        })}
                  </p>
                </div>
                <div className={styles.planMark}>
                  <CortexIcon name="cost" size={25} />
                </div>
              </div>
              <div className={styles.actionRow}>
                {canManage ? (
                  <button
                    type="button"
                    className={styles.primaryButton}
                    disabled={action !== null}
                    onClick={onPortal}
                  >
                    {action === "portal"
                      ? "Opening…"
                      : paymentPastDue
                        ? "Update payment method"
                        : "Manage subscription"}
                  </button>
                ) : null}
                {!billingEnabled && !cortexGrant ? (
                  <button type="button" className={styles.secondaryButton} disabled>
                    Billing unavailable
                  </button>
                ) : null}
                <button type="button" className={styles.secondaryButton} onClick={onViewPlans}>
                  View plans
                </button>
              </div>
            </section>

            <section className={styles.usageCard} aria-labelledby="allowance-title">
              <div className={styles.sectionHeading}>
                <div>
                  <span>CURRENT PERIOD</span>
                  <h2 id="allowance-title">Allowance usage</h2>
                </div>
                <p>Resets {formatDate(entitlements.period.ends_at)}</p>
              </div>

              <div className={styles.usageRows}>
                {USAGE_ROWS.map(({ key, label }) => (
                  <UsageRow key={key} label={label} counter={entitlements.allowances[key]} />
                ))}
              </div>
            </section>
          </div>
        </>
      ) : null}
    </div>
  );
}

function UsageRow({ label, counter }: { label: string; counter?: AllowanceCounter }) {
  const used = counter?.used ?? 0;
  const limit = counter?.limit ?? 0;
  const percentage = limit > 0 ? Math.min(100, Math.max(0, (used / limit) * 100)) : 0;
  return (
    <div className={styles.usageRow}>
      <div className={styles.usageLabel}>
        <span>{label}</span>
        <strong>
          {limit > 0 ? `${formatAiCredits(used)} / ${formatAiCredits(limit)}` : "Not included"}
        </strong>
      </div>
      <div
        className={styles.progressTrack}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={toDisplayAiCredits(limit)}
        aria-valuenow={toDisplayAiCredits(Math.min(used, limit))}
        aria-valuetext={`${formatAiCredits(used)} of ${formatAiCredits(limit)} AI credits used`}
      >
        <span style={{ width: `${percentage}%` }} />
      </div>
    </div>
  );
}

function CheckoutNotice({ status }: { status: CheckoutConfirmationStatus }) {
  if (status === "idle") return null;
  const copy = {
    cancelled: "Checkout was cancelled. Your current plan remains unchanged.",
    confirming: "Waiting for Stripe's verified subscription update…",
    confirmed: "Your paid plan is active and your allowances are ready.",
    pending: "Confirmation is taking longer than expected. Current access remains unchanged.",
  }[status];
  const tone =
    status === "confirmed"
      ? styles.bannerSuccess
      : status === "pending"
        ? styles.bannerWarning
        : "";
  return (
    <div className={`${styles.banner} ${tone}`} role="status">
      {copy}
    </div>
  );
}

function periodCopy({
  status,
  paidPlan,
  cancelAtPeriodEnd,
  periodEnd,
}: {
  status?: SubscriptionStatus;
  paidPlan: boolean;
  cancelAtPeriodEnd: boolean;
  periodEnd: string | null;
}): string {
  if (isCancelled(status)) return "Paid access ended; Free allowances remain available.";
  if (cancelAtPeriodEnd) return `Access through ${formatDate(periodEnd)}`;
  if (isPaymentPastDue(status)) return `Payment update due before the grace period ends`;
  if (paidPlan) return `Renews ${formatDate(periodEnd)}`;
  return `Usage resets ${formatDate(periodEnd)}`;
}

function statusClass(status?: SubscriptionStatus): string {
  if (isPaymentPastDue(status)) return styles.statusWarning;
  if (isCancelled(status)) return styles.statusMuted;
  return styles.statusActive;
}

function formatStatus(status?: SubscriptionStatus): string {
  if (!status || status === "free") return "Free";
  if (status === "past_due") return "Past due";
  if (status === "canceled" || status === "incomplete_expired") return "Cancelled";
  return status.charAt(0).toUpperCase() + status.slice(1).replaceAll("_", " ");
}

function formatDate(value: string | null): string {
  if (!value) return "the end of the current period";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "the end of the current period";
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function isPaymentPastDue(status?: SubscriptionStatus): boolean {
  return status === "past_due" || status === "unpaid" || status === "incomplete";
}

function isCancelled(status?: SubscriptionStatus): boolean {
  return status === "canceled" || status === "incomplete_expired";
}
