import type {
  AllowanceCounter,
  EntitlementsResponse,
  SubscriptionMeterKey,
} from "../../types";
import { formatAiCredits, toDisplayAiCredits } from "../../utils/aiCredits";
import { PlanBadge } from "./PlanBadge";
import styles from "./UsageAllowance.module.css";

const METER_PRESENTATION: Array<{
  key: SubscriptionMeterKey;
  label: string;
  shortLabel: string;
}> = [
  { key: "ai_credits", label: "AI credits", shortLabel: "Credits" },
];

export function UsageAllowance({
  entitlements,
  compact = false,
  title = "Plan allowances",
  eyebrow = "Current billing period",
}: {
  entitlements: EntitlementsResponse;
  compact?: boolean;
  title?: string;
  eyebrow?: string;
}) {
  const meters = METER_PRESENTATION.filter(
    ({ key }) => entitlements.allowances[key] !== undefined,
  );

  return (
    <section
      className={`${styles.panel} ${compact ? styles.compact : ""}`}
      aria-labelledby="subscription-allowances-title"
    >
      <header className={styles.header}>
        <div>
          <span className={styles.eyebrow}>{eyebrow}</span>
          <h2 id="subscription-allowances-title">{title}</h2>
        </div>
        <div className={styles.planMeta}>
          <PlanBadge label={entitlements.plan.display_name} tone="current" />
          <span>Resets {formatDate(entitlements.period.ends_at)}</span>
        </div>
      </header>

      <div className={styles.grid}>
        {meters.map(({ key, label, shortLabel }) => (
          <AllowanceItem
            counter={entitlements.allowances[key]!}
            key={key}
            label={compact ? shortLabel : label}
          />
        ))}
      </div>
    </section>
  );
}

function AllowanceItem({
  counter,
  label,
}: {
  counter: AllowanceCounter;
  label: string;
}) {
  const consumed = Math.max(0, counter.used + counter.reserved);
  const percent = counter.limit > 0 ? Math.min(100, (consumed / counter.limit) * 100) : 100;
  const exhausted = counter.remaining <= 0;
  const value = `${formatAiCredits(counter.remaining)} left of ${formatAiCredits(counter.limit)}`;

  return (
    <article className={styles.item} data-exhausted={exhausted ? "true" : "false"}>
      <div className={styles.itemTop}>
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <div
        className={styles.track}
        role="progressbar"
        aria-label={`${label}: ${value}`}
        aria-valuemin={0}
        aria-valuemax={toDisplayAiCredits(counter.limit)}
        aria-valuenow={toDisplayAiCredits(consumed)}
        aria-valuetext={value}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
    </article>
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "at period end";
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}
