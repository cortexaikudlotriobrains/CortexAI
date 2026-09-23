import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { AccountMenu } from "../components/layout/AccountMenu";
import { WorkspaceSidebar } from "../components/layout/WorkspaceSidebar";
import { ProviderLogo } from "../components/shared/ProviderLogo";
import { CortexIcon } from "../components/shared/CortexIcon";
import { PlanBadge } from "../components/subscription/PlanBadge";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { UpgradeDialog } from "../components/subscription/UpgradeDialog";
import {
  MODELS_CATALOG,
  buildModelsCatalogFromLiveModels,
  findCatalogModelById,
  getDepthInfo,
  getModelSearchText,
  getModelsCatalogSummary,
  getPresentationProviderKey,
  getSpeedLevel,
  splitGatewayModelId,
  type CatalogModel,
  type CatalogProvider,
  type ModelsCatalog,
} from "../config/modelsCatalog";
import { getModelPresentation } from "../config/modelPresentation";
import { useAuth } from "../hooks/useAuth";
import { useChat } from "../hooks/useChat";
import { useModels } from "../hooks/useModels";
import { useSubscription } from "../hooks/useSubscription";
import { useTheme } from "../hooks/useTheme";
import { useChatStore } from "../store/chatStore";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import { modelAccessError, requiredPlanForModel } from "../subscription/subscriptionAccess";
import type { SubscriptionError } from "../subscription/subscriptionErrors";
import type { BillingPlansResponse, EntitlementsResponse, ModelCatalogItem } from "../types";
import styles from "./ModelsPage.module.css";

type ProviderStyle = CSSProperties & {
  "--models-provider-color": string;
  "--models-provider-soft": string;
};

type MeterStyle = CSSProperties & {
  "--models-meter-fill": string;
};

interface FilteredProvider {
  provider: CatalogProvider;
  models: CatalogModel[];
}

interface CatalogModelAccess {
  locked: boolean;
  label: string | null;
  error: SubscriptionError | null;
}

export function ModelsPage() {
  const navigate = useNavigate();
  const { cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const { cancel } = useChat();
  const { theme, toggleTheme } = useTheme();
  const startNewChat = useChatStore((s) => s.startNewChat);
  const setHistory = useChatStore((s) => s.setHistory);
  const setHistorySearch = useChatStore((s) => s.setHistorySearch);
  const authEnabled = cognitoConfig?.enabled ?? false;
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );
  const accountBillingDestination = accountSubscription.billingDestination;
  const { models: liveModels, loading: modelsLoading } = useModels(
    !authLoading && (!authEnabled || loggedIn),
    "catalog",
  );
  const liveCatalogSummary = modelsLoading
    ? getModelsCatalogSummary()
    : {
        providerCount: new Set(liveModels.map((model) => model.provider)).size,
        modelCount: liveModels.length,
      };
  const [accessError, setAccessError] = useState<SubscriptionError | null>(null);

  const handleLogout = () => {
    cancel();
    startNewChat();
    setHistory([]);
    setHistorySearch("");
    void logout();
  };

  const handleMobileBack = () => {
    if (window.history.length > 1) {
      navigate(-1);
    } else {
      navigate("/");
    }
  };

  return (
    <div className={styles.layout}>
      <WorkspaceSidebar
        activeView="models"
        authLoading={authLoading}
        authEnabled={authEnabled}
        loggedIn={loggedIn}
      />

      <main className={styles.main}>
        <header className={styles.mobileHeader}>
          <button
            type="button"
            className={styles.mobileBackButton}
            aria-label="Back to chat"
            onClick={handleMobileBack}
          >
            <CortexIcon name="chevron-left" size={17} strokeWidth={2} />
          </button>
          <div className={styles.mobileTitleBlock}>
            <h1>Models</h1>
            <p>{formatCatalogSummary(liveCatalogSummary)}</p>
          </div>
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
            onUsageInsights={() => navigate("/usage")}
            onCredits={() => navigate("/credits")}
            theme={theme}
            onToggleTheme={toggleTheme}
          />
        </header>

        <SubscriptionBanner
          entitlements={subscriptionState.entitlements}
          onManageBilling={() => navigate("/account/billing")}
        />
        <ModelsCatalogScreen
          entitlements={subscriptionState.entitlements}
          plans={subscriptionState.plans}
          liveModels={modelsLoading ? undefined : liveModels}
          onAccessDenied={setAccessError}
        />
        <UpgradeDialog
          error={accessError}
          onClose={() => setAccessError(null)}
          onViewPlans={() => {
            setAccessError(null);
            navigate("/pricing");
          }}
          onManageBilling={() => {
            setAccessError(null);
            navigate("/account/billing");
          }}
        />
      </main>
    </div>
  );
}

export function ModelsCatalogScreen({
  catalog = MODELS_CATALOG,
  loading = false,
  entitlements = null,
  plans = null,
  liveModels,
  onAccessDenied,
}: {
  catalog?: ModelsCatalog;
  loading?: boolean;
  entitlements?: EntitlementsResponse | null;
  plans?: BillingPlansResponse | null;
  liveModels?: ModelCatalogItem[];
  onAccessDenied?: (error: SubscriptionError) => void;
}) {
  const effectiveCatalog = useMemo(
    () =>
      liveModels && liveModels.length > 0
        ? buildModelsCatalogFromLiveModels(liveModels, catalog)
        : catalog,
    [catalog, liveModels],
  );
  const [selectedTask, setSelectedTask] = useState(catalog.tasks[0] ?? "All");
  const [searchQuery, setSearchQuery] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const summary = useMemo(() => getModelsCatalogSummary(effectiveCatalog), [effectiveCatalog]);
  const normalizedSearch = searchQuery.trim().toLowerCase();
  const isAllTask = selectedTask === "All";
  const recId = isAllTask ? undefined : effectiveCatalog.rec[selectedTask];
  const recommendation = isAllTask ? null : findCatalogModelById(recId, effectiveCatalog);

  const filteredProviders = useMemo<FilteredProvider[]>(() => {
    return effectiveCatalog.providers
      .map((provider) => ({
        provider,
        models: provider.models.filter((model) => {
          const matchesTask = isAllTask || model.tags.includes(selectedTask);
          const matchesSearch =
            !normalizedSearch || getModelSearchText(model).includes(normalizedSearch);
          return matchesTask && matchesSearch;
        }),
      }))
      .filter((group) => group.models.length > 0);
  }, [effectiveCatalog, isAllTask, normalizedSearch, selectedTask]);

  const shownCount = filteredProviders.reduce((total, group) => total + group.models.length, 0);
  const visibleIds = useMemo(
    () => new Set(filteredProviders.flatMap((group) => group.models.map((model) => model.id))),
    [filteredProviders],
  );

  useEffect(() => {
    if (expandedId && !visibleIds.has(expandedId)) setExpandedId(null);
  }, [expandedId, visibleIds]);

  if (loading) return <ModelsLoadingState />;

  return (
    <>
      <header className={styles.header}>
        <div className={styles.titleBlock}>
          <h1>Models</h1>
          <p>
            {summary.providerCount} providers <span aria-hidden="true">·</span> {summary.modelCount}{" "}
            models <span aria-hidden="true">·</span> pick one manually or let Smart route
          </p>
        </div>
        <label className={styles.searchField}>
          <span className={styles.srOnly}>Search models</span>
          <CortexIcon name="search" size={16} strokeWidth={1.8} />
          <input
            type="search"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder="Search models..."
          />
        </label>
      </header>

      <section className={styles.body} aria-label="Models catalog">
        <section className={styles.taskPanel} aria-labelledby="models-task-title">
          <div className={styles.taskHeader}>
            <h2 id="models-task-title">What are you working on?</h2>
            <span aria-live="polite">
              {shownCount} of {summary.modelCount} models
            </span>
          </div>
          <div className={styles.taskChips} aria-label="Model task filters">
            {effectiveCatalog.tasks.map((task) => {
              const selected = selectedTask === task;
              return (
                <button
                  type="button"
                  className={`${styles.taskChip} ${selected ? styles.taskChipActive : ""}`}
                  aria-pressed={selected}
                  key={task}
                  onClick={() => setSelectedTask(task)}
                >
                  {task}
                </button>
              );
            })}
          </div>
        </section>

        <RecommendationCallout
          selectedTask={selectedTask}
          recommendation={recommendation}
          access={
            recommendation
              ? resolveCatalogModelAccess(recommendation.model, liveModels, entitlements, plans)
              : null
          }
        />

        {shownCount === 0 ? (
          <NoMatchesState
            onClear={() => {
              setSearchQuery("");
              setSelectedTask("All");
            }}
          />
        ) : (
          <section className={styles.providerList} aria-label="Models grouped by provider">
            {filteredProviders.map(({ provider, models }) => (
              <ProviderGroup
                expandedId={expandedId}
                models={models}
                provider={provider}
                recId={recId}
                selectedTask={selectedTask}
                entitlements={entitlements}
                plans={plans}
                liveModels={liveModels}
                key={provider.key}
                onToggle={(modelId) =>
                  setExpandedId((current) => (current === modelId ? null : modelId))
                }
                onAccessDenied={onAccessDenied}
              />
            ))}
          </section>
        )}
      </section>
    </>
  );
}

function RecommendationCallout({
  selectedTask,
  recommendation,
  access,
}: {
  selectedTask: string;
  recommendation: ReturnType<typeof findCatalogModelById>;
  access: CatalogModelAccess | null;
}) {
  if (!recommendation) {
    return (
      <section className={styles.smartCallout} aria-label="Smart routing hint">
        <span className={styles.smartIcon} aria-hidden="true">
          <CortexIcon name="smart" size={18} strokeWidth={1.85} />
        </span>
        <p>
          Not sure which to pick? Choose a task above to see the recommended model, or leave it to{" "}
          <strong>Smart routing</strong>, which selects the best model for every prompt
          automatically.
        </p>
      </section>
    );
  }

  const { provider, model } = recommendation;
  const providerStyle = buildProviderStyle(provider);
  const depth = getDepthInfo(model.tier);
  const speedLevel = getSpeedLevel(model.speed);

  return (
    <section
      className={styles.recommendationCallout}
      style={providerStyle}
      aria-label={`Best model for ${selectedTask}`}
    >
      <ProviderGlyphTile provider={provider} size={44} />
      <div className={styles.recommendationCopy}>
        <span>★ BEST FOR {selectedTask.toUpperCase()}</span>
        <h2>
          {model.name}
          {access?.locked ? (
            <PlanBadge label={access.label ?? "Unavailable"} tone="locked" />
          ) : null}
        </h2>
        <p>{model.bestFor}</p>
      </div>
      <div className={styles.recommendationMeters}>
        <Meter label="Speed" level={speedLevel} valueLabel={model.speed} fill="var(--cx-ink-900)" />
        <Meter
          label="Depth"
          level={depth.level}
          valueLabel={depth.label}
          fill="var(--models-provider-color)"
        />
      </div>
    </section>
  );
}

function ProviderGroup({
  provider,
  models,
  selectedTask,
  recId,
  expandedId,
  onToggle,
  entitlements,
  plans,
  liveModels,
  onAccessDenied,
}: {
  provider: CatalogProvider;
  models: CatalogModel[];
  selectedTask: string;
  recId?: string;
  expandedId: string | null;
  onToggle: (modelId: string) => void;
  entitlements: EntitlementsResponse | null;
  plans: BillingPlansResponse | null;
  liveModels?: ModelCatalogItem[];
  onAccessDenied?: (error: SubscriptionError) => void;
}) {
  const providerStyle = buildProviderStyle(provider);

  return (
    <section
      className={styles.providerGroup}
      style={providerStyle}
      aria-labelledby={`${provider.key}-models`}
    >
      <header className={styles.providerHeader}>
        <ProviderGlyphTile provider={provider} size={26} />
        <h2 id={`${provider.key}-models`}>{provider.name}</h2>
        <span>
          {models.length} {models.length === 1 ? "model" : "models"}
        </span>
        <i aria-hidden="true" />
      </header>
      <div className={styles.modelRows}>
        {models.map((model) => (
          <ModelRow
            access={resolveCatalogModelAccess(model, liveModels, entitlements, plans)}
            expanded={expandedId === model.id}
            key={model.id}
            model={model}
            provider={provider}
            recommended={selectedTask !== "All" && model.id === recId}
            selectedTask={selectedTask}
            onToggle={() => onToggle(model.id)}
            onAccessDenied={onAccessDenied}
          />
        ))}
      </div>
    </section>
  );
}

function ModelRow({
  provider,
  model,
  selectedTask,
  recommended,
  expanded,
  onToggle,
  access,
  onAccessDenied,
}: {
  provider: CatalogProvider;
  model: CatalogModel;
  selectedTask: string;
  recommended: boolean;
  expanded: boolean;
  onToggle: () => void;
  access: CatalogModelAccess | null;
  onAccessDenied?: (error: SubscriptionError) => void;
}) {
  const providerStyle = buildProviderStyle(provider);
  const depth = getDepthInfo(model.tier);
  const speedLevel = getSpeedLevel(model.speed);
  const detailsId = `model-details-${model.id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;

  return (
    <article
      className={`${styles.modelRow} ${recommended ? styles.modelRowRecommended : ""} ${
        expanded ? styles.modelRowExpanded : ""
      }`}
      style={providerStyle}
    >
      <button
        type="button"
        className={styles.rowButton}
        aria-expanded={expanded}
        aria-controls={detailsId}
        aria-label={`${model.name} details`}
        onClick={onToggle}
      >
        <ProviderGlyphTile provider={provider} size={30} />
        <span className={styles.modelIdentity}>
          <span className={styles.modelNameLine}>
            <span className={styles.modelName}>{model.name}</span>
            {recommended ? <span className={styles.topBadge}>★ TOP</span> : null}
            {access?.locked ? (
              <PlanBadge label={access.label ?? "Unavailable"} tone="locked" />
            ) : null}
          </span>
          <span className={styles.modelTier}>{model.tier}</span>
        </span>
        <span className={styles.modelBestFor}>{model.bestFor}</span>
        <Meter
          compact
          label="Speed"
          level={speedLevel}
          valueLabel={model.speed}
          fill="var(--cx-ink-900)"
        />
        <Meter
          compact
          label="Depth"
          level={depth.level}
          valueLabel={depth.label}
          fill="var(--models-provider-color)"
        />
        <CortexIcon className={styles.rowChevron} name="chevron-down" size={17} strokeWidth={2} />
      </button>

      {expanded ? (
        <div id={detailsId} className={styles.rowDetails}>
          <div className={styles.tagList} aria-label={`${model.name} tags`}>
            {model.tags.map((tag) => (
              <span key={tag}>{tag}</span>
            ))}
          </div>
          <ul className={styles.strengthList}>
            {model.strengths.map((strength) => (
              <li key={strength}>
                <CortexIcon name="check" size={13} strokeWidth={2.2} />
                <span>{strength}</span>
              </li>
            ))}
          </ul>
          <code>{model.id}</code>
          {model.pricingLabel ? (
            <p className={styles.pricingEvidence}>
              <span>{model.pricingLabel}</span>
              {model.pricingSourceUrl ? (
                <a href={model.pricingSourceUrl} target="_blank" rel="noreferrer">
                  Official pricing
                  {model.sourceVerifiedAt ? ` · verified ${model.sourceVerifiedAt}` : ""}
                </a>
              ) : null}
            </p>
          ) : null}
          {recommended ? (
            <span className={styles.recommendedNote}>Recommended for {selectedTask}</span>
          ) : null}
          {access?.locked && access.error && onAccessDenied ? (
            <button
              type="button"
              className={styles.accessAction}
              onClick={() => onAccessDenied(access.error!)}
            >
              See plan options
            </button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function Meter({
  label,
  level,
  valueLabel,
  fill,
  compact = false,
}: {
  label: "Speed" | "Depth";
  level: number;
  valueLabel: string;
  fill: string;
  compact?: boolean;
}) {
  const meterStyle = { "--models-meter-fill": fill } as MeterStyle;

  return (
    <span
      className={`${styles.meter} ${compact ? styles.meterCompact : ""} ${
        label === "Speed" ? styles.speedMeter : ""
      }`}
      style={meterStyle}
      role="img"
      aria-label={`${label}: ${valueLabel}`}
    >
      <span className={styles.meterLabel}>{label.toUpperCase()}</span>
      <span className={styles.meterBars} aria-hidden="true">
        {Array.from({ length: 3 }).map((_, index) => (
          <span
            className={`${styles.meterBar} ${index < level ? styles.meterBarFilled : ""}`}
            key={`${label}-${index}`}
          />
        ))}
      </span>
      <span className={styles.meterValue}>{valueLabel}</span>
    </span>
  );
}

function ProviderGlyphTile({ provider, size }: { provider: CatalogProvider; size: number }) {
  const presentationKey = getPresentationProviderKey(provider.key);
  const firstModel = provider.models[0]?.id ?? "";
  const { model } = splitGatewayModelId(firstModel);
  const presentation = getModelPresentation(presentationKey, model);

  return (
    <span
      className={styles.providerTile}
      style={{
        width: `${size}px`,
        height: `${size}px`,
        flexBasis: `${size}px`,
      }}
      aria-hidden="true"
    >
      <ProviderLogo
        provider={provider.key}
        logoUrl={presentation.logoUrl}
        color={`var(${provider.colorVar})`}
        size={Math.max(14, Math.round(size * 0.58))}
        className={styles.providerLogo}
      />
    </span>
  );
}

function ModelsLoadingState() {
  return (
    <>
      <header className={styles.header}>
        <div className={styles.titleBlock}>
          <h1>Models</h1>
          <p>Loading provider catalog...</p>
        </div>
      </header>
      <section className={styles.body} aria-busy="true" aria-label="Loading models">
        <span className={styles.srOnly}>Loading models</span>
        <div className={styles.skeletonTask} aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
        </div>
        <div className={styles.skeletonCallout} aria-hidden="true" />
        <div className={styles.skeletonRows} aria-hidden="true">
          {Array.from({ length: 6 }).map((_, index) => (
            <span key={index} />
          ))}
        </div>
      </section>
    </>
  );
}

function NoMatchesState({ onClear }: { onClear: () => void }) {
  return (
    <section className={styles.noMatches} role="status">
      <p>No models match — clear filters</p>
      <button type="button" onClick={onClear}>
        Clear filters
      </button>
    </section>
  );
}

function buildProviderStyle(provider: CatalogProvider): ProviderStyle {
  return {
    "--models-provider-color": `var(${provider.colorVar})`,
    "--models-provider-soft": `var(${provider.colorSoftVar})`,
  };
}

function formatCatalogSummary(summary: ReturnType<typeof getModelsCatalogSummary>): string {
  return `${summary.modelCount} across ${summary.providerCount} providers`;
}

function resolveCatalogModelAccess(
  model: CatalogModel,
  liveModels: ModelCatalogItem[] | undefined,
  entitlements: EntitlementsResponse | null,
  plans: BillingPlansResponse | null,
): CatalogModelAccess | null {
  if (!entitlements || !liveModels) return null;
  const liveModel = liveModels.find(
    (candidate) => `${candidate.provider}:${candidate.model}` === model.id,
  );
  if (!liveModel) {
    return { locked: true, label: "Unavailable", error: null };
  }
  const error = modelAccessError(liveModel, entitlements, plans);
  if (!error) return { locked: false, label: null, error: null };
  const requiredPlan = requiredPlanForModel(liveModel.billing_class, entitlements, plans);
  return {
    locked: true,
    label: requiredPlan ? `${capitalize(requiredPlan)} plan` : "Unavailable",
    error,
  };
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}
