import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { exportUsageCsv, type UsageSummaryParams } from "../api/usage";
import { AccountMenu } from "../components/layout/AccountMenu";
import { WorkspaceSidebar } from "../components/layout/WorkspaceSidebar";
import { ProviderLogo } from "../components/shared/ProviderLogo";
import { CortexIcon, type CortexIconName } from "../components/shared/CortexIcon";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { getModelPresentation } from "../config/modelPresentation";
import { presentError, type UserFacingError } from "../errors/userFacingError";
import { useAuth } from "../hooks/useAuth";
import { useChat } from "../hooks/useChat";
import { useSubscription } from "../hooks/useSubscription";
import { useTheme } from "../hooks/useTheme";
import { useUsageSummary } from "../hooks/useUsageSummary";
import { useChatStore } from "../store/chatStore";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import type { ChatMode, UsageSummary, UsageSummaryPeriod } from "../types";
import styles from "./UsageInsightsPage.module.css";

export function UsageInsightsPage() {
  const navigate = useNavigate();
  const { cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const { cancel } = useChat();
  const { theme, toggleTheme } = useTheme();
  const setMode = useChatStore((s) => s.setMode);
  const startNewChat = useChatStore((s) => s.startNewChat);
  const setHistory = useChatStore((s) => s.setHistory);
  const setHistorySearch = useChatStore((s) => s.setHistorySearch);
  const [selectedPeriodKey, setSelectedPeriodKey] = useState<UsagePeriodKey>("last30");
  const [periodMenuOpen, setPeriodMenuOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const [exportError, setExportError] = useState<UserFacingError | null>(null);
  const periodControlRef = useRef<HTMLDivElement>(null);
  const selectedPeriod = getUsagePeriodOption(selectedPeriodKey);
  const usageParams = useMemo(() => buildUsagePeriodParams(selectedPeriodKey), [selectedPeriodKey]);
  const {
    summary,
    loading: usageLoading,
    error: usageError,
    reload,
  } = useUsageSummary(usageParams);
  const authEnabled = cognitoConfig?.enabled ?? false;
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );

  const accountBillingDestination = accountSubscription.billingDestination;
  const periodLabel = summary?.period.label ?? selectedPeriod.label;
  const periodControlLabel = selectedPeriod.label;
  const empty = summary ? isUsageSummaryEmpty(summary) : false;

  useEffect(() => {
    if (!periodMenuOpen) return;

    const closeOnOutsidePointer = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (periodControlRef.current?.contains(target)) return;
      setPeriodMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPeriodMenuOpen(false);
    };

    document.addEventListener("mousedown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [periodMenuOpen]);

  const openChatMode = (nextMode: ChatMode) => {
    setMode(nextMode);
    navigate("/");
  };

  const handleLogout = () => {
    cancel();
    startNewChat();
    setHistory([]);
    setHistorySearch("");
    void logout();
  };

  const handleSelectPeriod = (periodKey: UsagePeriodKey) => {
    setSelectedPeriodKey(periodKey);
    setPeriodMenuOpen(false);
    setExportStatus(null);
    setExportError(null);
  };

  const handleExportUsage = async () => {
    if (!summary || exporting || !subscriptionState.entitlements?.features.usage_export_enabled) {
      return;
    }

    setExporting(true);
    setExportStatus("Exporting usage CSV");
    setExportError(null);
    try {
      const blob = await exportUsageCsv({
        from: summary.period.from,
        to: summary.period.to,
        groupBy: "day",
      });
      triggerCsvDownload(blob, buildUsageExportFilename(summary.period));
      setExportStatus(`Exported usage CSV for ${summary.period.label}`);
    } catch (err) {
      const visibleError = presentError(err, "usage_export");
      setExportStatus(null);
      setExportError(visibleError);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className={styles.layout}>
      <WorkspaceSidebar
        activeView="usage"
        authLoading={authLoading}
        authEnabled={authEnabled}
        loggedIn={loggedIn}
      />

      <main className={styles.main}>
        <header className={styles.header}>
          <button
            type="button"
            className={styles.mobileBackButton}
            aria-label="Back to chat"
            onClick={() => navigate("/")}
          >
            <CortexIcon name="chevron-left" size={16} strokeWidth={2} />
          </button>
          <div className={styles.titleBlock}>
            <h1 aria-label="Usage & insights">
              <span className={styles.desktopTitle} aria-hidden="true">
                Usage &amp; insights
              </span>
              <span className={styles.mobileTitle} aria-hidden="true">
                Usage
              </span>
            </h1>
            <p className={styles.desktopSubtitle}>
              All models <span aria-hidden="true">&middot;</span> {periodLabel.toLowerCase()}
            </p>
            <p className={styles.mobileSubtitle}>{periodLabel}</p>
          </div>
          <div className={styles.actions} aria-label="Usage controls">
            <div className={styles.periodControl} ref={periodControlRef}>
              <button
                type="button"
                className={styles.periodButton}
                aria-haspopup="menu"
                aria-expanded={periodMenuOpen}
                aria-label={`Select usage period: ${periodControlLabel}`}
                onClick={() => setPeriodMenuOpen((open) => !open)}
              >
                <span className={styles.periodDesktopLabel}>{periodControlLabel}</span>
                <span className={styles.periodMobileLabel}>{selectedPeriod.mobileLabel}</span>
                <CortexIcon name="chevron-down" size={14} strokeWidth={2} />
              </button>
              {periodMenuOpen ? (
                <div className={styles.periodMenu} role="menu" aria-label="Usage period options">
                  {USAGE_PERIOD_OPTIONS.map((option) => {
                    const selected = option.key === selectedPeriodKey;
                    return (
                      <button
                        type="button"
                        className={`${styles.periodMenuItem} ${
                          selected ? styles.periodMenuItemSelected : ""
                        }`}
                        role="menuitemradio"
                        aria-checked={selected}
                        data-usage-period-option={option.key}
                        key={option.key}
                        onClick={() => handleSelectPeriod(option.key)}
                      >
                        <span>{option.label}</span>
                        {selected ? <CortexIcon name="check" size={14} strokeWidth={2} /> : null}
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </div>
            <button
              type="button"
              className={styles.exportButton}
              aria-busy={exporting}
              aria-label={`Export usage CSV for ${periodLabel}`}
              disabled={
                usageLoading ||
                !summary ||
                exporting ||
                !subscriptionState.entitlements?.features.usage_export_enabled
              }
              title={
                subscriptionState.entitlements?.features.usage_export_enabled
                  ? undefined
                  : "CSV export is available on Plus and Pro"
              }
              onClick={() => void handleExportUsage()}
            >
              <CortexIcon name="download" size={15} />
              <span>Export</span>
            </button>
            {exportStatus ? (
              <span className={styles.srOnly} role="status" aria-live="polite">
                {exportStatus}
              </span>
            ) : null}
            <div className={styles.mobileAccountMenu}>
              <AccountMenu
                authEnabled={authEnabled}
                loggedIn={loggedIn}
                onLogin={authEnabled ? login : undefined}
                onLogout={handleLogout}
                planLabel={accountSubscription.planLabel}
                billingActionLabel={accountSubscription.billingActionLabel}
                billingPastDue={accountSubscription.billingPastDue}
                onBilling={
                  accountBillingDestination ? () => navigate(accountBillingDestination) : undefined
                }
                onModels={() => navigate("/models")}
                theme={theme}
                onToggleTheme={toggleTheme}
                onUsageInsights={() => navigate("/usage")}
                onCredits={() => navigate("/credits")}
              />
            </div>
          </div>
        </header>
        <SubscriptionBanner
          entitlements={subscriptionState.entitlements}
          onManageBilling={() => navigate("/account/billing")}
        />
        {exportError ? (
          <section className={styles.exportError} role="alert">
            <div>
              <strong>{exportError.title}</strong>
              <span>{exportError.message}</span>
              {exportError.requestId ? <small>Support code: {exportError.requestId}</small> : null}
            </div>
            <button type="button" onClick={() => void handleExportUsage()}>Try again</button>
            <button type="button" onClick={() => setExportError(null)}>Dismiss</button>
          </section>
        ) : null}
        <section className={styles.body} aria-label="Usage dashboard content">
          {usageLoading ? (
            <UsageLoadingState />
          ) : usageError ? (
            <UsageErrorState error={usageError} onRetry={reload} />
          ) : summary ? (
            <UsageDashboard summary={summary} showModelEmpty={empty} />
          ) : (
            <section className={styles.readyState} aria-label="Usage summary loaded" />
          )}
        </section>
        <nav className={styles.mobileNav} aria-label="Mobile navigation">
          <button type="button" onClick={() => openChatMode("single")}>
            <span className={styles.mobileNavIcon}>
              <CortexIcon name="ask" />
            </span>
            <span>Ask</span>
          </button>
          <button type="button" onClick={() => openChatMode("compare")}>
            <span className={styles.mobileNavIcon}>
              <CortexIcon name="compare" />
            </span>
            <span>Compare</span>
          </button>
          <button type="button" onClick={() => navigate("/")}>
            <span className={styles.mobileNavIcon}>
              <CortexIcon name="history" />
            </span>
            <span>History</span>
          </button>
        </nav>
      </main>
    </div>
  );
}

function UsageDashboard({
  summary,
  showModelEmpty,
}: {
  summary: UsageSummary;
  showModelEmpty: boolean;
}) {
  return (
    <section className={styles.dashboard} aria-label="Usage summary loaded">
      <KpiRow summary={summary} />
      <div className={styles.panelsRow}>
        <ModelLeaderboard summary={summary} empty={showModelEmpty} />
        <SessionModesPanel summary={summary} />
      </div>
      <ActivityChartPanel summary={summary} />
    </section>
  );
}

function KpiRow({ summary }: { summary: UsageSummary }) {
  const cards = buildKpiCards(summary);

  return (
    <section className={styles.kpiGrid} aria-label="Usage key metrics">
      {cards.map((card) => (
        <article className={styles.kpiCard} aria-label={card.ariaLabel} key={card.key}>
          <div className={styles.kpiHeader}>
            <span className={`${styles.kpiTile} ${card.accent ? styles.kpiTileAccent : ""}`}>
              <CortexIcon name={card.icon} size={16} strokeWidth={1.85} />
            </span>
            <span className={styles.kpiLabel}>
              <span className={styles.kpiDesktopLabel}>{card.label}</span>
              <span className={styles.kpiMobileLabel}>{card.mobileLabel}</span>
            </span>
          </div>
          <div className={styles.kpiFigure}>
            {card.figure}
            {card.suffix ? <span className={styles.kpiSuffix}>{card.suffix}</span> : null}
          </div>
          <div
            className={`${styles.kpiSubnote} ${
              card.positiveTrend ? styles.kpiSubnotePositive : ""
            }`}
          >
            <span className={styles.kpiDesktopSubnote}>{card.subnote}</span>
            <span className={styles.kpiMobileSubnote}>{card.mobileSubnote ?? card.subnote}</span>
          </div>
        </article>
      ))}
    </section>
  );
}

function ModelLeaderboard({ summary, empty }: { summary: UsageSummary; empty: boolean }) {
  const rows = [...summary.models].sort((left, right) => right.replies - left.replies);
  const maxReplies = Math.max(1, ...rows.map((row) => row.replies));
  const maxViaSmart = Math.max(0, ...rows.map((row) => row.viaSmart));
  const topSmartIndex =
    maxViaSmart > 0 ? rows.findIndex((row) => row.viaSmart === maxViaSmart) : -1;
  const topSmartRow = topSmartIndex >= 0 ? rows[topSmartIndex] : null;

  return (
    <section className={styles.modelsPanel} aria-labelledby="usage-models-title">
      <div className={styles.panelHeader}>
        <div>
          <h2 id="usage-models-title">Models that replied</h2>
          <p>
            {formatInteger(summary.smartRoutedTotal)} of {formatInteger(summary.totalRequests)}{" "}
            replies auto-routed by Smart
          </p>
        </div>
        <span className={styles.panelTotal}>
          <span className={styles.modelTotalDesktop}>
            {formatInteger(summary.totalRequests)} total
          </span>
          <span className={styles.modelTotalMobile}>
            {formatInteger(summary.smartRoutedTotal)} via Smart
          </span>
        </span>
      </div>

      {topSmartRow ? (
        <div className={styles.topSmartStrip}>
          <CortexIcon name="smart" size={14} strokeWidth={1.9} />
          <span>
            <strong>{topSmartRow.displayName}</strong> is your top Smart pick{" \u2014 "}
            {formatInteger(topSmartRow.viaSmart)} routes
          </span>
        </div>
      ) : null}

      {empty ? (
        <div className={styles.modelEmptyState}>No model activity yet for this period</div>
      ) : (
        <div className={styles.modelScroll} tabIndex={0} aria-label="Models that replied list">
          {rows.map((model, index) => {
            const provider = getUsageProviderMeta(model.provider);
            const presentation = getModelPresentation(provider.presentationProvider, model.modelId);
            const barWidth = `${Math.max(0, Math.min(100, (model.replies / maxReplies) * 100))}%`;
            const viaSmartCaption =
              model.viaSmart > 0 ? `${formatInteger(model.viaSmart)} via Smart` : "manual only";
            const barLabel = `${model.displayName}: ${formatInteger(
              model.replies,
            )} replies, ${viaSmartCaption}`;
            const rowStyle = {
              "--usage-model-bar-width": barWidth,
              "--usage-model-bar-color": provider.barColor,
            } as CSSProperties;

            return (
              <article className={styles.modelRow} key={`${model.provider}:${model.modelId}`}>
                <div className={styles.modelTopLine}>
                  <span
                    className={styles.providerTile}
                    style={{ backgroundColor: provider.tileBg }}
                    data-usage-provider-logo={provider.key}
                    aria-hidden="true"
                  >
                    <ProviderLogo
                      provider={provider.presentationProvider}
                      logoUrl={presentation.logoUrl}
                      color={presentation.color}
                      size={17}
                      className={styles.providerLogo}
                    />
                  </span>
                  <div className={styles.modelIdentity}>
                    <div className={styles.modelNameLine}>
                      <h3>{model.displayName}</h3>
                      {index === topSmartIndex ? (
                        <span className={styles.smartBadge}>MOST USED IN SMART</span>
                      ) : null}
                    </div>
                    <p>{model.modelId}</p>
                  </div>
                  <div className={styles.modelCount}>
                    <strong>{formatInteger(model.replies)}</strong>
                    <span>replies</span>
                  </div>
                </div>
                <div
                  className={styles.modelBarLine}
                  style={rowStyle}
                  role="img"
                  aria-label={barLabel}
                >
                  <span className={styles.modelBarTrack}>
                    <span className={styles.modelBarFill} />
                  </span>
                  <span
                    className={`${styles.modelSmartCaption} ${
                      model.viaSmart === 0 ? styles.modelSmartCaptionMuted : ""
                    }`}
                  >
                    {viaSmartCaption}
                  </span>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function SessionModesPanel({ summary }: { summary: UsageSummary }) {
  const rows = buildSessionModeRows(summary);
  const total = Math.max(
    safeUsageInteger(summary.totalSessions),
    rows.reduce((sum, row) => sum + row.count, 0),
  );
  const switched = safeUsageInteger(summary.switchedMidSession);
  const ariaLabel = `Session modes: ${rows
    .map((row) => `${row.label} ${formatInteger(row.count)} sessions, ${row.percentLabel}`)
    .join("; ")}`;

  return (
    <section className={styles.sessionsPanel} aria-labelledby="usage-session-modes-title">
      <div className={styles.panelHeader}>
        <div>
          <h2 id="usage-session-modes-title">Session modes</h2>
          <p>How sessions split across modes</p>
        </div>
        <span className={styles.panelTotal}>
          <span className={styles.sessionTotalDesktop}>{formatInteger(total)}</span>
          <span className={styles.sessionTotalMobile}>{formatInteger(total)} sessions</span>
        </span>
      </div>

      <div className={styles.sessionStack} role="img" aria-label={ariaLabel}>
        {rows.map((row) => (
          <span
            className={styles.sessionSegment}
            key={row.key}
            style={
              {
                "--usage-session-width": row.width,
                "--usage-session-color": row.color,
              } as CSSProperties
            }
            aria-hidden="true"
          />
        ))}
      </div>

      <div className={styles.sessionLegend} aria-label="Session mode breakdown">
        {rows.map((row) => (
          <div className={styles.sessionLegendRow} key={row.key}>
            <span
              className={styles.sessionDot}
              style={{ backgroundColor: row.color }}
              aria-hidden="true"
            />
            <span className={styles.sessionLegendLabel}>
              {row.mobileLabel === row.label ? (
                <span className={styles.sessionLegendAllLabel}>{row.label}</span>
              ) : (
                <>
                  <span className={styles.sessionLegendDesktopLabel}>{row.label}</span>
                  <span className={styles.sessionLegendMobileLabel}>{row.mobileLabel}</span>
                </>
              )}
            </span>
            <span className={styles.sessionLegendCount}>{formatInteger(row.count)}</span>
            <span className={styles.sessionLegendPercent}>{row.percentLabel}</span>
          </div>
        ))}
      </div>

      <div className={styles.sessionInsightWrap}>
        <div className={styles.sessionInsight}>
          <CortexIcon name="swap" size={15} strokeWidth={2} />
          <span>
            <strong>
              {formatInteger(switched)} {formatSessionNoun(switched)} switched modes
            </strong>{" "}
            mid-conversation{" \u2014 "}Ask {"\u2194"} Compare in one session.
          </span>
        </div>
      </div>
    </section>
  );
}

function ActivityChartPanel({ summary }: { summary: UsageSummary }) {
  const days = buildActivityDays(summary);
  const maxTokens = Math.max(1, ...days.map((day) => day.tokens));
  const totalTokens = days.reduce((sum, day) => sum + day.tokens, 0);
  const averageTokens = days.length > 0 ? totalTokens / days.length : 0;
  const totalValue = formatCompactNumber(totalTokens);
  const caption = `${totalValue.figure}${totalValue.suffix ?? ""} total \u00b7 ~${formatCompactWholeNumber(
    averageTokens,
  )}/day`;
  const axisLabels = buildActivityAxisLabels(days);
  const chartLabel = `Tokens last 14 days: ${formatInteger(totalTokens)} total, approximately ${formatInteger(
    averageTokens,
  )} per day`;

  return (
    <section className={styles.activityPanel} aria-labelledby="usage-activity-title">
      <div className={styles.activityHeader}>
        <h2 id="usage-activity-title">Tokens {"\u00b7"} last 14 days</h2>
        <span>{caption}</span>
      </div>
      <div className={styles.activityBars} role="img" aria-label={chartLabel}>
        {days.map((day) => (
          <span
            className={`${styles.activityBar} ${isWeekendIsoDate(day.date) ? styles.activityBarWeekend : ""}`}
            key={day.date}
            data-usage-activity-bar={day.date}
            data-usage-activity-tone={isWeekendIsoDate(day.date) ? "weekend" : "weekday"}
            style={
              {
                "--usage-activity-height": formatActivityHeight(day.tokens, maxTokens),
              } as CSSProperties
            }
            title={`${formatActivityDate(day.date)}: ${formatInteger(day.tokens)} tokens`}
            aria-hidden="true"
          />
        ))}
      </div>
      <div className={styles.activityAxis} aria-hidden="true">
        {axisLabels.map((label, index) => (
          <span key={`${label}:${index}`}>{label}</span>
        ))}
      </div>
    </section>
  );
}

interface KpiCardConfig {
  key: string;
  label: string;
  mobileLabel: string;
  icon: CortexIconName;
  accent?: boolean;
  figure: string;
  suffix?: string;
  subnote: ReactNode;
  mobileSubnote?: ReactNode;
  ariaLabel: string;
  positiveTrend?: boolean;
}

function buildKpiCards(summary: UsageSummary): KpiCardConfig[] {
  const hasUsage = summary.totalRequests > 0 || summary.totalTokens > 0;
  const tokenValue = formatCompactNumber(summary.totalTokens);
  const tokenDelta = hasUsage ? formatPercent(summary.tokensDeltaPct) : DASH;
  const tokenDirection = summary.tokensDeltaPct < 0 ? "down" : "up";
  const latencyValue = hasUsage ? formatLatency(summary.avgLatencyMs) : DASH;
  const latencySub = hasUsage
    ? `p95 ${formatLatency(summary.p95LatencyMs)} \u00b7 fastest ${formatLatency(summary.minLatencyMs)}`
    : DASH;
  const costValue = hasUsage ? formatCost(summary.avgCostPerRequest, 4) : DASH;
  const spendSub = hasUsage ? `${formatCost(summary.totalSpend, 2)} total spend` : DASH;

  return [
    {
      key: "tokens",
      label: "TOTAL TOKENS",
      mobileLabel: "TOKENS",
      icon: "tokens",
      accent: true,
      figure: tokenValue.figure,
      suffix: tokenValue.suffix,
      subnote: hasUsage ? (
        <>
          <TrendIcon direction={tokenDirection} />
          {tokenDelta} vs prev 30d
        </>
      ) : (
        DASH
      ),
      mobileSubnote: hasUsage ? (
        <>
          <TrendIcon direction={tokenDirection} />
          {tokenDelta}
        </>
      ) : (
        DASH
      ),
      ariaLabel: `TOTAL TOKENS ${tokenValue.figure}${tokenValue.suffix ?? ""} ${
        hasUsage ? `${tokenDelta} vs prev 30d` : DASH
      }`,
      positiveTrend: hasUsage && summary.tokensDeltaPct >= 0,
    },
    {
      key: "requests",
      label: "REQUESTS",
      mobileLabel: "REQUESTS",
      icon: "swap",
      figure: formatInteger(summary.totalRequests),
      subnote: `across ${formatInteger(summary.totalSessions)} sessions`,
      mobileSubnote: `${formatInteger(summary.totalSessions)} sessions`,
      ariaLabel: `REQUESTS ${formatInteger(summary.totalRequests)} across ${formatInteger(
        summary.totalSessions,
      )} sessions`,
    },
    {
      key: "latency",
      label: "AVG LATENCY",
      mobileLabel: "AVG LATENCY",
      icon: "latency",
      figure: latencyValue,
      subnote: latencySub,
      mobileSubnote: hasUsage ? `p95 ${formatLatency(summary.p95LatencyMs)}` : DASH,
      ariaLabel: `AVG LATENCY ${latencyValue} ${latencySub}`,
    },
    {
      key: "cost",
      label: "AVG COST / REQ",
      mobileLabel: "AVG COST",
      icon: "cost",
      figure: costValue,
      subnote: spendSub,
      mobileSubnote: hasUsage ? `${formatCost(summary.totalSpend, 2)} total` : DASH,
      ariaLabel: `AVG COST / REQ ${costValue} ${spendSub}`,
    },
  ];
}

function TrendIcon({ direction }: { direction: "up" | "down" }) {
  return (
    <svg
      className={direction === "down" ? styles.trendIconDown : styles.trendIcon}
      width="9"
      height="9"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M12 5l8 13H4z" />
    </svg>
  );
}

function UsageLoadingState() {
  return (
    <section className={styles.loadingState} aria-busy="true" aria-label="Loading usage insights">
      <span className={styles.srOnly}>Loading usage insights</span>
      <div className={styles.skeletonKpis} aria-hidden="true">
        {Array.from({ length: 4 }).map((_, index) => (
          <div className={styles.skeletonCard} key={index}>
            <span className={`${styles.skeletonBlock} ${styles.skeletonTile}`} />
            <span className={`${styles.skeletonBlock} ${styles.skeletonFigure}`} />
            <span className={`${styles.skeletonBlock} ${styles.skeletonCaption}`} />
          </div>
        ))}
      </div>
      <div className={styles.skeletonPanels} aria-hidden="true">
        <div className={`${styles.skeletonPanel} ${styles.skeletonPanelWide}`}>
          <span className={`${styles.skeletonBlock} ${styles.skeletonHeader}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonRow}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonRowShort}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonRow}`} />
        </div>
        <div className={styles.skeletonPanel}>
          <span className={`${styles.skeletonBlock} ${styles.skeletonHeader}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonBar}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonRowShort}`} />
          <span className={`${styles.skeletonBlock} ${styles.skeletonRowShort}`} />
        </div>
      </div>
      <div className={styles.skeletonPanel} aria-hidden="true">
        <span className={`${styles.skeletonBlock} ${styles.skeletonHeader}`} />
        <span className={`${styles.skeletonBlock} ${styles.skeletonChart}`} />
      </div>
    </section>
  );
}

function UsageErrorState({ error, onRetry }: { error: UserFacingError; onRetry: () => void }) {
  return (
    <section className={styles.statePanel} role="alert">
      <div>
        <p className={styles.stateTitle}>{error.title}</p>
        <p className={styles.stateText}>{error.message}</p>
        {error.requestId && <p className={styles.stateText}>Support code: {error.requestId}</p>}
      </div>
      <button type="button" className={styles.retryButton} onClick={onRetry}>
        Retry
      </button>
    </section>
  );
}

const DASH = "\u2014";

type UsagePeriodKey = "last7" | "last30" | "last90";

interface UsagePeriodOption {
  key: UsagePeriodKey;
  label: string;
  mobileLabel: string;
  days: number;
  useBackendDefault?: boolean;
}

const USAGE_PERIOD_OPTIONS: UsagePeriodOption[] = [
  { key: "last7", label: "Last 7 days", mobileLabel: "7d", days: 7 },
  {
    key: "last30",
    label: "Last 30 days",
    mobileLabel: "30d",
    days: 30,
    useBackendDefault: true,
  },
  { key: "last90", label: "Last 90 days", mobileLabel: "90d", days: 90 },
];

function getUsagePeriodOption(key: UsagePeriodKey): UsagePeriodOption {
  return USAGE_PERIOD_OPTIONS.find((option) => option.key === key) ?? USAGE_PERIOD_OPTIONS[1];
}

function buildUsagePeriodParams(key: UsagePeriodKey): UsageSummaryParams {
  const option = getUsagePeriodOption(key);
  if (option.useBackendDefault) return {};

  const to = new Date();
  const from = new Date(to);
  from.setDate(to.getDate() - (option.days - 1));
  return {
    from: toIsoDate(from),
    to: toIsoDate(to),
  };
}

function buildUsageExportFilename(period: UsageSummaryPeriod): string {
  const from = normalizeIsoDate(period.from) || "from";
  const to = normalizeIsoDate(period.to) || "to";
  return `usage-report-${from}-${to}.csv`;
}

function triggerCsvDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

interface SessionModeRow {
  key: string;
  label: string;
  mobileLabel: string;
  count: number;
  color: string;
  percentLabel: string;
  width: string;
}

function buildSessionModeRows(summary: UsageSummary): SessionModeRow[] {
  const askOnly = safeUsageInteger(summary.sessionModes.askOnly);
  const compareOnly = safeUsageInteger(summary.sessionModes.compareOnly);
  const mixed = safeUsageInteger(summary.sessionModes.mixed);
  const sessionTotal = safeUsageInteger(summary.totalSessions);
  const modeTotal = askOnly + compareOnly + mixed;
  const denominator = Math.max(sessionTotal, modeTotal);

  return [
    {
      key: "askOnly",
      label: "Ask only",
      mobileLabel: "Ask",
      count: askOnly,
      color: "#0b1220",
    },
    {
      key: "compareOnly",
      label: "Compare only",
      mobileLabel: "Compare",
      count: compareOnly,
      color: "#5b5bd6",
    },
    {
      key: "mixed",
      label: "Mixed",
      mobileLabel: "Mixed",
      count: mixed,
      color: "#8b8bf0",
    },
  ].map((row) => ({
    ...row,
    percentLabel: formatWholePercent(row.count, denominator),
    width: formatStackWidth(row.count, denominator),
  }));
}

function safeUsageInteger(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.round(value);
}

function formatStackWidth(count: number, total: number): string {
  if (total <= 0) return "0%";
  const percent = Math.max(0, Math.min(100, (count / total) * 100));
  return `${percent.toFixed(1)}%`;
}

function formatWholePercent(count: number, total: number): string {
  if (total <= 0) return "0%";
  return `${Math.round((count / total) * 100)}%`;
}

function formatSessionNoun(count: number): string {
  return count === 1 ? "session" : "sessions";
}

interface ActivityChartDay {
  date: string;
  tokens: number;
}

function buildActivityDays(summary: UsageSummary): ActivityChartDay[] {
  const activityDays = summary.activityDaily
    .map((day) => ({
      date: normalizeIsoDate(day.date),
      tokens: safeUsageInteger(day.tokens),
    }))
    .filter((day): day is ActivityChartDay => Boolean(day.date))
    .sort((left, right) => left.date.localeCompare(right.date))
    .slice(-14);

  if (activityDays.length > 0) return activityDays;
  return buildEmptyActivityDays(summary.period.to);
}

function buildEmptyActivityDays(periodEnd: string): ActivityChartDay[] {
  const end = parseIsoDate(periodEnd);
  if (!end) return [];
  return Array.from({ length: 14 }, (_, index) => {
    const date = new Date(end);
    date.setDate(end.getDate() - (13 - index));
    return {
      date: toIsoDate(date),
      tokens: 0,
    };
  });
}

function buildActivityAxisLabels(days: ActivityChartDay[]): string[] {
  if (days.length === 0) return [];
  const labelIndexes = uniqueActivityAxisIndexes(days.length);
  return labelIndexes.map((index, position) =>
    position === labelIndexes.length - 1 ? "Today" : formatActivityDate(days[index]?.date ?? ""),
  );
}

function uniqueActivityAxisIndexes(length: number): number[] {
  if (length <= 1) return [0];
  return [0, Math.round((length - 1) * 0.31), Math.round((length - 1) * 0.62), length - 1]
    .map((index) => Math.max(0, Math.min(length - 1, index)))
    .filter((index, position, indexes) => indexes.indexOf(index) === position);
}

function formatActivityHeight(tokens: number, maxTokens: number): string {
  if (tokens <= 0 || maxTokens <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (tokens / maxTokens) * 100)).toFixed(1)}%`;
}

function formatActivityDate(date: string): string {
  const parsed = parseIsoDate(date);
  if (!parsed) return "";
  return parsed.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function isWeekendIsoDate(date: string): boolean {
  const parsed = parseIsoDate(date);
  if (!parsed) return false;
  const day = parsed.getDay();
  return day === 0 || day === 6;
}

function parseIsoDate(value: string): Date | null {
  const normalized = normalizeIsoDate(value);
  if (!normalized) return null;
  const parsed = new Date(`${normalized}T12:00:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function normalizeIsoDate(value: string): string {
  const match = value.trim().match(/^(\d{4}-\d{2}-\d{2})/);
  return match?.[1] ?? "";
}

function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatCompactNumber(value: number): { figure: string; suffix?: string } {
  if (!Number.isFinite(value) || value <= 0) return { figure: "0" };
  if (value >= 1_000_000) return { figure: trimNumber(value / 1_000_000, 2), suffix: "M" };
  if (value >= 1_000) return { figure: trimNumber(value / 1_000, 1), suffix: "K" };
  return { figure: formatInteger(value) };
}

function formatCompactWholeNumber(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1_000_000) return `${Math.round(value / 1_000_000).toLocaleString("en-US")}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000).toLocaleString("en-US")}K`;
  return formatInteger(value);
}

function trimNumber(value: number, maximumFractionDigits: number): string {
  return value
    .toLocaleString("en-US", {
      maximumFractionDigits,
      minimumFractionDigits: maximumFractionDigits,
    })
    .replace(/\.0+$/, "");
}

function formatInteger(value: number): string {
  if (!Number.isFinite(value)) return "0";
  return Math.round(value).toLocaleString("en-US");
}

function formatLatency(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return DASH;
  return `${(ms / 1000).toLocaleString("en-US", {
    maximumFractionDigits: 1,
    minimumFractionDigits: 1,
  })}s`;
}

function formatCost(value: number, fractionDigits: number): string {
  if (!Number.isFinite(value) || value < 0) return DASH;
  return `$${value.toLocaleString("en-US", {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
  })}`;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "0%";
  const normalized = Math.abs(value);
  return `${normalized.toLocaleString("en-US", {
    maximumFractionDigits: 1,
    minimumFractionDigits: Number.isInteger(normalized) ? 0 : 1,
  })}%`;
}

interface UsageProviderMeta {
  key: string;
  presentationProvider: string;
  tileBg: string;
  barColor: string;
}

const USAGE_PROVIDER_META: Record<string, UsageProviderMeta> = {
  openai: {
    key: "openai",
    presentationProvider: "openai",
    tileBg: "#eef0fb",
    barColor: "#5b5bd6",
  },
  anthropic: {
    key: "anthropic",
    presentationProvider: "claude",
    tileBg: "#fbeee6",
    barColor: "#e07a4d",
  },
  deepseek: {
    key: "deepseek",
    presentationProvider: "deepseek",
    tileBg: "#e7f0fc",
    barColor: "#3d7ff0",
  },
  google: {
    key: "google",
    presentationProvider: "gemini",
    tileBg: "#f1f3f5",
    barColor: "#4285f4",
  },
  meta: {
    key: "meta",
    presentationProvider: "meta",
    tileBg: "#f1f3f5",
    barColor: "#0866ff",
  },
  mistral: {
    key: "mistral",
    presentationProvider: "mistral",
    tileBg: "#f1f3f5",
    barColor: "#ff7000",
  },
};

function getUsageProviderMeta(provider: string): UsageProviderMeta {
  const normalized = normalizeUsageProvider(provider);
  return (
    USAGE_PROVIDER_META[normalized] ?? {
      key: normalized || "model",
      presentationProvider: provider,
      tileBg: "#f1f3f5",
      barColor: "#94a3b8",
    }
  );
}

function normalizeUsageProvider(provider: string): string {
  const normalized = provider.trim().toLowerCase();
  if (normalized === "claude") return "anthropic";
  if (normalized === "gemini") return "google";
  if (normalized === "google_gemini" || normalized === "googlegemini") return "google";
  if (normalized === "mistralai" || normalized === "mistral-ai") return "mistral";
  return normalized;
}

function isUsageSummaryEmpty(summary: UsageSummary): boolean {
  return summary.totalRequests === 0 || summary.models.length === 0;
}
