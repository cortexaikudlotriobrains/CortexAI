import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchCreditTransactions } from "../api/entitlements";
import { CortexIcon } from "../components/shared/CortexIcon";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { SubscriptionPageShell } from "../components/subscription/SubscriptionPageShell";
import { UsageAllowance } from "../components/subscription/UsageAllowance";
import { getModelPresentation } from "../config/modelPresentation";
import { useAuth } from "../hooks/useAuth";
import { useSubscription } from "../hooks/useSubscription";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import type { CreditTransaction } from "../types";
import { formatAiCredits } from "../utils/aiCredits";
import styles from "./CreditsPage.module.css";

export function CreditsPage() {
  const navigate = useNavigate();
  const { whoAmI, cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );
  const [transactions, setTransactions] = useState<CreditTransaction[] | null>(null);
  const [transactionsError, setTransactionsError] = useState<string | null>(null);
  const [transactionsReloadToken, setTransactionsReloadToken] = useState(0);
  const authEnabled = cognitoConfig?.enabled ?? false;

  useEffect(() => {
    if (!loggedIn || subscriptionState.lastLoadedAt === null) {
      setTransactions(null);
      setTransactionsError(null);
      return;
    }

    const controller = new AbortController();
    setTransactions(null);
    setTransactionsError(null);
    void fetchCreditTransactions(100, 0, controller.signal)
      .then((response) => setTransactions(response.items))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setTransactionsError(
          error instanceof Error ? error.message : "Recent credit activity could not load.",
        );
      });
    return () => controller.abort();
  }, [loggedIn, subscriptionState.lastLoadedAt, transactionsReloadToken]);

  return (
    <SubscriptionPageShell
      title="AI credits"
      subtitle="Balance and question costs"
      activeView="credits"
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
      <div className={styles.page}>
        <SubscriptionBanner
          entitlements={subscriptionState.entitlements}
          onManageBilling={() => navigate("/account/billing")}
        />

        <header className={styles.intro}>
          <span>AI CREDITS</span>
          <h1>See what each question cost.</h1>
          <p>
            Each activity shows one total. When Improve Prompt is used, optimization and answer
            generation appear together as one final optimized answer.
          </p>
        </header>

        {!loggedIn ? (
          <section className={styles.messagePanel} aria-labelledby="credits-sign-in-title">
            <CortexIcon name="cost" size={22} />
            <div>
              <h2 id="credits-sign-in-title">Sign in to view AI credits</h2>
              <p>Your credit balance and charge history are tied to your account.</p>
            </div>
            {authEnabled ? (
              <button type="button" onClick={login}>
                Sign in
              </button>
            ) : null}
          </section>
        ) : null}

        {loggedIn && subscriptionState.loading && !subscriptionState.entitlements ? (
          <section className={styles.messagePanel} aria-label="Loading AI credits" aria-busy="true">
            <CortexIcon name="cost" size={22} />
            <div>
              <h2>Loading AI credits</h2>
              <p>Retrieving your current balance and billing period.</p>
            </div>
          </section>
        ) : null}

        {loggedIn && subscriptionState.error && !subscriptionState.entitlements ? (
          <section className={styles.messagePanel} role="alert">
            <CortexIcon name="cost" size={22} />
            <div>
              <h2>AI credit balance could not load</h2>
              <p>{subscriptionState.error.message}</p>
            </div>
            <button type="button" onClick={subscriptionState.reload}>
              Retry
            </button>
          </section>
        ) : null}

        {subscriptionState.entitlements ? (
          <UsageAllowance entitlements={subscriptionState.entitlements} title="AI credit balance" />
        ) : null}

        {loggedIn ? (
          <RecentCreditActivity
            transactions={transactions}
            error={transactionsError}
            onRetry={() => setTransactionsReloadToken((value) => value + 1)}
          />
        ) : null}
      </div>
    </SubscriptionPageShell>
  );
}

function RecentCreditActivity({
  transactions,
  error,
  onRetry,
}: {
  transactions: CreditTransaction[] | null;
  error: string | null;
  onRetry: () => void;
}) {
  const activities = transactions ? groupCreditTransactions(transactions).slice(0, 20) : null;

  return (
    <section className={styles.activityPanel} aria-labelledby="recent-credit-activity-title">
      <div className={styles.panelHeader}>
        <div>
          <span>CREDIT HISTORY</span>
          <h2 id="recent-credit-activity-title">Recent activity</h2>
          <p>One card per question or credit-using action, newest first.</p>
        </div>
      </div>

      {error ? (
        <div className={styles.activityMessage} role="alert">
          <span>{error}</span>
          <button type="button" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : transactions === null ? (
        <p className={styles.activityMessage} role="status" aria-busy="true">
          Loading recent credit activity…
        </p>
      ) : activities?.length === 0 ? (
        <p className={styles.activityMessage}>No credit activity yet for this billing period.</p>
      ) : (
        <div className={styles.creditList}>
          {activities?.map((activity) => {
            const breakdown = buildCreditBreakdown(activity);
            const question =
              activity.query || "Question unavailable for activity recorded before query tracking.";
            return (
              <article
                className={styles.activityCard}
                key={activity.activityId}
                aria-label={`Credit activity for ${question}`}
              >
                <div className={styles.activityOverview}>
                  <div className={styles.queryBlock}>
                    <span>YOUR QUESTION</span>
                    <p>{question}</p>
                  </div>
                  <div className={styles.creditAmount}>
                    <strong>{formatAiCredits(activity.totalCredits)} credits</strong>
                    <span>{formatCreditTimestamp(activity.createdAt)}</span>
                  </div>
                </div>

                <p className={styles.activityType}>{creditActivityLabel(activity)}</p>

                <details className={styles.breakdown}>
                  <summary>
                    <span>View credit breakdown</span>
                    <span>
                      {breakdown.length} {breakdown.length === 1 ? "charge" : "charges"}
                    </span>
                  </summary>
                  <div className={styles.breakdownList} role="list">
                    {breakdown.map((item) => (
                      <div className={styles.breakdownRow} role="listitem" key={item.key}>
                        <div>
                          <strong>{item.label}</strong>
                          <span>{item.detail}</span>
                        </div>
                        <strong>{formatAiCredits(item.totalCredits)} credits</strong>
                      </div>
                    ))}
                  </div>
                </details>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

interface CreditActivity {
  activityId: string;
  query: string | null;
  createdAt: string;
  totalCredits: number;
  items: CreditTransaction[];
}

interface CreditBreakdownItem {
  key: string;
  label: string;
  detail: string;
  totalCredits: number;
}

function groupCreditTransactions(transactions: CreditTransaction[]): CreditActivity[] {
  const grouped = new Map<string, CreditActivity>();
  transactions.forEach((transaction) => {
    const current = grouped.get(transaction.activity_id);
    if (current) {
      current.items.push(transaction);
      current.totalCredits += transaction.total_credits;
      if (!current.query && transaction.query) current.query = transaction.query;
      return;
    }
    grouped.set(transaction.activity_id, {
      activityId: transaction.activity_id,
      query: transaction.query,
      createdAt: transaction.created_at,
      totalCredits: transaction.total_credits,
      items: [transaction],
    });
  });
  return [...grouped.values()].filter((activity) => activity.totalCredits > 0);
}

function creditActivityLabel(activity: CreditActivity): string {
  const chargedItems = activity.items.filter((item) => item.total_credits > 0);
  const hasOptimizerCharge = chargedItems.some((item) => item.operation_type === "optimize");
  const answerItems = chargedItems.filter(
    (item) => item.item_type === "model" && item.operation_type !== "optimize",
  );
  const hasSearchCharge = chargedItems.some((item) => item.item_type === "research");
  const parts: string[] = [];
  if (hasOptimizerCharge && answerItems.length > 0) {
    parts.push(finalOptimizedAnswerLabel(answerItems));
  } else {
    if (hasOptimizerCharge) parts.push("Prompt Optimizer");
    if (answerItems.length > 0) parts.push(answerActivityLabel(answerItems));
  }
  if (hasSearchCharge) parts.push("Web Search");
  return parts.join(" + ") || "Credit activity";
}

function buildCreditBreakdown(activity: CreditActivity): CreditBreakdownItem[] {
  const chargedItems = activity.items.filter((item) => item.total_credits > 0);
  const optimizerItems = chargedItems.filter(
    (item) => item.item_type === "model" && item.operation_type === "optimize",
  );
  const answerItems = chargedItems.filter(
    (item) => item.item_type === "model" && item.operation_type !== "optimize",
  );
  const combinesOptimizedAnswer = optimizerItems.length > 0 && answerItems.length > 0;
  const otherItems = chargedItems.filter(
    (item) =>
      !optimizerItems.includes(item) &&
      !(combinesOptimizedAnswer && answerItems.includes(item)),
  );
  const breakdown: CreditBreakdownItem[] = [];
  if (combinesOptimizedAnswer) {
    const combinedItems = [...optimizerItems, ...answerItems];
    const optimizerAttemptCopy = `${optimizerItems.length} optimizer ${
      optimizerItems.length === 1 ? "attempt" : "attempts"
    }`;
    const answerCopy =
      answerItems.length === 1 ? "final answer" : `${answerItems.length} final answers`;
    breakdown.push({
      key: "final-optimized-answer",
      label: finalOptimizedAnswerLabel(answerItems),
      detail: `Includes Prompt Optimizer (${optimizerAttemptCopy}) and ${answerCopy} · ${modelSummary(
        combinedItems,
      )} · ${formatModelCreditParts(
        sumCredits(combinedItems, "input_credits"),
        sumCredits(combinedItems, "output_credits"),
      )}`,
      totalCredits: sumCredits(combinedItems, "total_credits"),
    });
  } else if (optimizerItems.length > 0) {
    const inputCredits = sumCredits(optimizerItems, "input_credits");
    const outputCredits = sumCredits(optimizerItems, "output_credits");
    const model = optimizerItems[0];
    breakdown.push({
      key: "prompt-optimizer",
      label: "Prompt Optimizer",
      detail: `${modelDisplayName(model)} · ${optimizerItems.length} ${
        optimizerItems.length === 1 ? "attempt" : "attempts"
      } · ${formatModelCreditParts(inputCredits, outputCredits)}`,
      totalCredits: sumCredits(optimizerItems, "total_credits"),
    });
  }
  return [...breakdown, ...otherItems.map(toCreditBreakdownItem)];
}

function answerActivityLabel(answerItems: CreditTransaction[]): string {
  if (answerItems.some((item) => item.operation_type === "cortex_analysis")) {
    return "Cortex Analysis";
  }
  if (answerItems.some((item) => item.operation_type === "compare")) return "Compare answer";
  if (answerItems.some((item) => item.operation_type === "regenerate")) {
    return "Regenerated answer";
  }
  if (answerItems.some((item) => item.metadata.file_context === true)) return "File answer";
  return "AI answer";
}

function finalOptimizedAnswerLabel(answerItems: CreditTransaction[]): string {
  const answerLabel = answerActivityLabel(answerItems);
  if (answerLabel === "Compare answer") return "Final optimized Compare answer";
  if (answerLabel === "Regenerated answer") return "Final optimized regenerated answer";
  if (answerLabel === "File answer") return "Final optimized file answer";
  return "Final optimized AI answer";
}

function toCreditBreakdownItem(transaction: CreditTransaction): CreditBreakdownItem {
  if (transaction.item_type === "research") {
    const providerCredits = numericMetadata(transaction, "provider_credits_used");
    const conversion = numericMetadata(transaction, "cortex_credits_per_provider_credit");
    const detail =
      providerCredits !== null && conversion !== null
        ? `${formatInteger(providerCredits)} search ${
            providerCredits === 1 ? "credit" : "credits"
          } × ${formatAiCredits(conversion)} AI credits each`
        : "Search retrieval charge";
    return {
      key: transaction.id,
      label: "Web Search",
      detail: `${detail}${transaction.usage_estimated ? " · estimated" : ""}`,
      totalCredits: transaction.total_credits,
    };
  }

  if (transaction.item_type === "adjustment") {
    return {
      key: transaction.id,
      label: "Billing adjustment",
      detail: "Credit reconciliation",
      totalCredits: transaction.total_credits,
    };
  }

  return {
    key: transaction.id,
    label: creditTransactionLabel(transaction),
    detail: `${modelDisplayName(transaction)} · ${formatModelCreditParts(
      transaction.input_credits,
      transaction.output_credits,
    )}${transaction.usage_estimated ? " · estimated" : ""}`,
    totalCredits: transaction.total_credits,
  };
}

function creditTransactionLabel(transaction: CreditTransaction): string {
  if (transaction.operation_type === "cortex_analysis") return "Cortex Analysis";
  if (transaction.operation_type === "compare") return "Compare answer";
  if (transaction.operation_type === "regenerate") return "Regenerated answer";
  if (transaction.metadata.file_context === true) return "File answer";
  return "AI answer";
}

function modelDisplayName(transaction: CreditTransaction): string {
  return transaction.model
    ? getModelPresentation(transaction.provider ?? "", transaction.model).label
    : "AI model";
}

function modelSummary(transactions: CreditTransaction[]): string {
  const models = [...new Set(transactions.map(modelDisplayName))];
  return models.length <= 2 ? models.join(" + ") : `${models.length} AI models`;
}

function formatModelCreditParts(inputCredits: number, outputCredits: number): string {
  return `${formatAiCredits(inputCredits)} question processing + ${formatAiCredits(
    outputCredits,
  )} answer generation`;
}

function sumCredits(
  transactions: CreditTransaction[],
  key: "input_credits" | "output_credits" | "total_credits",
): number {
  return transactions.reduce((total, transaction) => total + transaction[key], 0);
}

function numericMetadata(transaction: CreditTransaction, key: string): number | null {
  const value = transaction.metadata[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function formatCreditTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}
