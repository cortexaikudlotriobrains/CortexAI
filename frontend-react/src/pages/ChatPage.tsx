import { type MouseEvent, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useLocation, useNavigate } from "react-router-dom";
import { fetchCortexAnalysisRuns } from "../api/cortexAnalysis";
import { fetchHistory } from "../api/history";
import { listWorkSessions } from "../api/work";
import { PromptComposer } from "../components/composer/PromptComposer";
import { ResultsSection } from "../components/results/ResultsSection";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { ExampleChips } from "../components/shared/ExampleChips";
import { CortexIcon } from "../components/shared/CortexIcon";
import { ProviderLogo } from "../components/shared/ProviderLogo";
import { AccountMenu } from "../components/layout/AccountMenu";
import { Sidebar } from "../components/layout/Sidebar";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { UpgradeDialog } from "../components/subscription/UpgradeDialog";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { getModelPresentation } from "../config/modelPresentation";
import { getRuntimeConfig } from "../config/runtimeConfig";
import { formatHistoryDateTime } from "../history/historyDate";
import { buildHistoryThreads, filterHistoryThreads } from "../history/historyThreads";
import { useAuth } from "../hooks/useAuth";
import { useChat } from "../hooks/useChat";
import { useHistory } from "../hooks/useHistory";
import { useModels } from "../hooks/useModels";
import { useSubscription } from "../hooks/useSubscription";
import { useTheme } from "../hooks/useTheme";
import { normalizeSessionId } from "../session/activeSession";
import { useChatStore } from "../store/chatStore";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import type { ChatMode, HistoryThread, ModelCatalogItem, WorkSession } from "../types";
import brandMarkUrl from "../assets/brand/brand-mark.svg";
import styles from "./ChatPage.module.css";

type MobilePanel = "chat" | "history";

interface MobileHistoryDateGroup {
  key: string;
  label: string;
  threads: HistoryThread[];
}

export function ChatPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { whoAmI, cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const authEnabled = cognitoConfig?.enabled ?? false;
  const signedOut = !authLoading && authEnabled && !loggedIn;
  const workspaceReady = !authLoading && !signedOut;
  const subscriptionState = useSubscription({ authLoading, loggedIn });
  const accountSubscription = getAccountMenuSubscriptionPresentation(
    subscriptionState.entitlements,
  );
  const accountBillingDestination = accountSubscription.billingDestination;
  const { models, loading: modelsLoading } = useModels(workspaceReady);
  const { load: loadHistory, removeThread } = useHistory();
  const { submit, regenerate, cancel } = useChat();
  const { theme, toggleTheme } = useTheme();
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>("chat");
  const [composerCollapsed, setComposerCollapsed] = useState(false);
  const [workSessions, setWorkSessions] = useState<WorkSession[]>([]);
  const streaming = useChatStore((s) => s.streaming);
  const error = useChatStore((s) => s.error);
  const setError = useChatStore((s) => s.setError);
  const subscriptionError = useChatStore((s) => s.subscriptionError);
  const setSubscriptionError = useChatStore((s) => s.setSubscriptionError);
  const hydrateFromHistoryThread = useChatStore((s) => s.hydrateFromHistoryThread);
  const mode = useChatStore((s) => s.mode);
  const sessionId = useChatStore((s) => s.sessionId);
  const setMode = useChatStore((s) => s.setMode);
  const startNewChat = useChatStore((s) => s.startNewChat);
  const setHistory = useChatStore((s) => s.setHistory);
  const setHistorySearch = useChatStore((s) => s.setHistorySearch);
  const hasTurns = useChatStore((s) => s.turns.length > 0);
  const retryTurnId = useChatStore((s) => {
    for (let index = s.turns.length - 1; index >= 0; index -= 1) {
      if (s.turns[index]?.status === "error") return s.turns[index]!.id;
    }
    return null;
  });
  const showComposerSheet = !composerCollapsed;
  const showComposerBackdrop = showComposerSheet && hasTurns;

  useEffect(() => {
    if (workspaceReady) void loadHistory({ restoreActiveTranscript: true });
  }, [loadHistory, workspaceReady]);

  useEffect(() => {
    if (!workspaceReady || getRuntimeConfig().workEnabled === false) return;
    void listWorkSessions().then(setWorkSessions).catch(() => setWorkSessions([]));
  }, [workspaceReady]);

  useEffect(() => {
    const requestedMode = new URLSearchParams(location.search).get("mode");
    if (requestedMode === "compare") setMode("compare");
  }, [location.search, setMode]);

  // Collapse the composer sheet on mobile as soon as the user submits
  const prevStreamingRef = useRef(false);
  useEffect(() => {
    if (streaming && !prevStreamingRef.current) setComposerCollapsed(true);
    prevStreamingRef.current = streaming;
  }, [streaming]);

  useEffect(() => {
    if (!subscriptionError) return;
    setMobilePanel("chat");
    setComposerCollapsed(false);
  }, [subscriptionError]);

  useEffect(() => {
    if (!hasTurns) setComposerCollapsed(false);
  }, [hasTurns]);

  const handleSelectHistoryThread = async (thread: HistoryThread) => {
    try {
      const [entries, analysisRuns] = thread.sessionId
        ? await Promise.all([
            fetchHistory(500, thread.sessionId),
            fetchCortexAnalysisRuns({ sessionId: thread.sessionId }),
          ])
        : [thread.entries, []];
      const completeThread = buildHistoryThreads(entries)[0] ?? thread;
      hydrateFromHistoryThread(completeThread, analysisRuns);
      setMobilePanel("chat");
      setComposerCollapsed(true);
    } catch (historyError) {
      setError(
        historyError instanceof Error ? historyError.message : "Failed to load chat history",
      );
    }
  };

  const handleDeleteHistoryThread = async (thread: HistoryThread) => {
    const deleted = await removeThread(thread);
    if (
      deleted &&
      normalizeSessionId(thread.sessionId) &&
      normalizeSessionId(thread.sessionId) === normalizeSessionId(sessionId)
    ) {
      cancel();
      startNewChat();
      setMobilePanel("chat");
      setComposerCollapsed(false);
    }
  };

  const handleMobileMode = (nextMode: ChatMode) => {
    setMode(nextMode);
    setMobilePanel("chat");
    setComposerCollapsed(hasTurns);
  };

  const handleStartNewChat = () => {
    cancel();
    startNewChat();
    setMobilePanel("chat");
    setComposerCollapsed(false);
  };

  const handleOpenMobileComposer = () => {
    flushSync(() => setComposerCollapsed(false));

    const promptInput = document.getElementById("promptInput");
    if (!(promptInput instanceof HTMLTextAreaElement) || promptInput.disabled) return;

    promptInput.focus({ preventScroll: true });
    const cursorPosition = promptInput.value.length;
    promptInput.setSelectionRange(cursorPosition, cursorPosition);
  };

  const handleLogout = () => {
    cancel();
    startNewChat();
    setHistory([]);
    setHistorySearch("");
    setMobilePanel("chat");
    setComposerCollapsed(false);
    void logout();
  };

  return (
    <div className={styles.layout} data-theme={theme}>
      <Sidebar
        onSelectThread={(thread) => void handleSelectHistoryThread(thread)}
        activeView="chat"
        onNavigateUsage={() => navigate("/usage")}
        onNavigateWork={getRuntimeConfig().workEnabled === false ? undefined : () => navigate("/work")}
        onNavigateCredits={() => navigate("/credits")}
        onNavigateModels={() => navigate("/models")}
        whoAmI={whoAmI}
        loggedIn={loggedIn}
        onLogin={authEnabled ? login : undefined}
        signedOut={signedOut}
        workSessions={workSessions}
        onSelectWorkSession={(session) => navigate(`/work/${session.id}`)}
      />

      <main className={styles.main}>
        <header className={styles.mobileTopbar}>
          <span className={styles.mobileBrand}>
            <img src={brandMarkUrl} alt="" aria-hidden="true" />
            <span>CortexAI</span>
          </span>
          <div className={styles.mobileHeaderActions}>
            <button
              type="button"
              className={`${styles.iconButton} ${styles.mobileComposeButton}`}
              aria-label="Start new chat"
              onClick={handleStartNewChat}
              disabled={signedOut}
            >
              <CortexIcon name="new-chat" />
            </button>
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
          </div>
        </header>

        <header className={styles.topbar}>
          <nav className={styles.tabs} aria-label="Workspace mode">
            <button
              id="btnSingleMode"
              type="button"
              className={`${styles.tab} ${mode === "single" ? styles.activeTab : ""}`}
              onClick={() => setMode("single")}
              aria-pressed={mode === "single"}
              disabled={signedOut}
            >
              Ask
            </button>
            <button
              id="btnCompareMode"
              type="button"
              className={`${styles.tab} ${mode === "compare" ? styles.activeTab : ""}`}
              onClick={() => setMode("compare")}
              aria-pressed={mode === "compare"}
              disabled={signedOut}
            >
              Compare
            </button>
            {getRuntimeConfig().workEnabled !== false && <button
              type="button"
              className={styles.tab}
              onClick={() => navigate("/work")}
              aria-label="Work"
              disabled={signedOut}
            >
              Work
            </button>}
          </nav>
          <div className={styles.topActions} aria-label="Workspace actions">
            <button
              type="button"
              className={styles.iconButton}
              aria-label="New chat"
              onClick={startNewChat}
              disabled={signedOut}
            >
              <CortexIcon name="plus" />
            </button>
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
          </div>
        </header>

        <SubscriptionBanner
          entitlements={subscriptionState.entitlements}
          onManageBilling={() => navigate("/account/billing")}
        />

        <div className={styles.canvas}>
          {authLoading ? (
            <WorkspaceLoading />
          ) : signedOut ? (
            <SignedOutGate onLogin={login} />
          ) : mobilePanel === "history" ? (
            <MobileHistory
              onSelectThread={(thread) => void handleSelectHistoryThread(thread)}
              onDeleteThread={(thread) => void handleDeleteHistoryThread(thread)}
            />
          ) : (
            <>
              <ResultsSection />
              {error && (
                <ErrorBanner
                  message={error}
                  onRetry={() => {
                    setError(null);
                    if (retryTurnId) {
                      void regenerate(retryTurnId);
                    } else {
                      void submit();
                    }
                  }}
                  onDismiss={() => setError(null)}
                />
              )}
              <ExampleChips />
            </>
          )}
        </div>

        {workspaceReady && mobilePanel === "chat" && (
          <>
            {/* Dim backdrop — mobile only, shown when sheet is open */}
            <div
              className={`${styles.composerBackdrop} ${showComposerBackdrop ? styles.composerBackdropVisible : ""}`}
              role="presentation"
              onClick={() => setComposerCollapsed(true)}
            />

            {/* Composer: inline on desktop, fixed sheet overlay on mobile */}
            <div className={styles.composerWrap} data-collapsed={composerCollapsed}>
              {/* Handle + collapse chevron — mobile sheet header */}
              <div className={styles.composerSheetHeader} aria-hidden="true">
                <div className={styles.composerSheetHandle} />
                <button
                  type="button"
                  className={styles.composerSheetClose}
                  aria-label="Collapse composer"
                  onClick={() => setComposerCollapsed(true)}
                >
                  <CortexIcon name="chevron-down" size={18} />
                </button>
              </div>
              <PromptComposer
                models={models}
                modelsLoading={modelsLoading}
                subscription={subscriptionState}
              />
            </div>

            {/* Docked mobile composer pill, shown when the sheet is collapsed */}
            {composerCollapsed && (
              <MobileComposerDock models={models} onOpen={handleOpenMobileComposer} />
            )}
          </>
        )}

        {workspaceReady && (
          <nav className={styles.mobileNav} aria-label="Mobile navigation">
            <button
              type="button"
              className={mobilePanel === "chat" && mode === "single" ? styles.mobileNavActive : ""}
              onClick={() => handleMobileMode("single")}
            >
              <span className={styles.mobileNavIcon}>
                <CortexIcon name="ask" />
              </span>
              <span>Ask</span>
            </button>
            <button
              type="button"
              className={mobilePanel === "chat" && mode === "compare" ? styles.mobileNavActive : ""}
              onClick={() => handleMobileMode("compare")}
            >
              <span className={styles.mobileNavIcon}>
                <CortexIcon name="compare" />
              </span>
              <span>Compare</span>
            </button>
            {getRuntimeConfig().workEnabled !== false && <button type="button" onClick={() => navigate("/work")}>
              <span className={styles.mobileNavIcon}>
                <CortexIcon name="work" />
              </span>
              <span>Work</span>
            </button>}
            <button
              type="button"
              className={mobilePanel === "history" ? styles.mobileNavActive : ""}
              onClick={() => setMobilePanel("history")}
            >
              <span className={styles.mobileNavIcon}>
                <CortexIcon name="history" />
              </span>
              <span>History</span>
            </button>
          </nav>
        )}

        <UpgradeDialog
          error={subscriptionError}
          onClose={() => setSubscriptionError(null)}
          onViewPlans={() => {
            setSubscriptionError(null);
            navigate("/pricing");
          }}
          onManageBilling={() => {
            setSubscriptionError(null);
            navigate("/account/billing");
          }}
        />
      </main>
    </div>
  );
}

function WorkspaceLoading() {
  return (
    <div className={styles.workspaceLoading} role="status">
      Preparing your workspace...
    </div>
  );
}

function SignedOutGate({ onLogin }: { onLogin: () => void }) {
  return (
    <div className={styles.signInGate}>
      <section className={styles.signInPanel} aria-labelledby="sign-in-title">
        <span className={styles.signInIcon} aria-hidden="true">
          <CortexIcon name="user" size={24} />
        </span>
        <div className={styles.signInCopy}>
          <p className={styles.signInEyebrow}>CortexAI workspace</p>
          <h2 id="sign-in-title">Sign in to use CortexAI</h2>
          <p>Access your AI workspace, saved chats, model comparison, and file analysis.</p>
        </div>
        <button type="button" className={styles.signInButton} onClick={onLogin}>
          Sign in
        </button>
      </section>
    </div>
  );
}

function MobileComposerDock({
  models,
  onOpen,
}: {
  models: ModelCatalogItem[];
  onOpen: () => void;
}) {
  const availableModels = models.length > 0 ? models : DEFAULT_MODELS;
  const mode = useChatStore((s) => s.mode);
  const smartMode = useChatStore((s) => s.smartMode);
  const selectedModelKey = useChatStore((s) => s.selectedModelKey);
  const compareModelKeys = useChatStore((s) => s.compareModelKeys);

  return (
    <button
      type="button"
      className={styles.composerDock}
      aria-label="Open follow-up composer"
      onClick={onOpen}
    >
      <span className={styles.composerDockContext}>
        {mode === "single" && smartMode ? (
          <span className={`${styles.dockContextChip} ${styles.dockSmartChip}`}>
            <span className={styles.dockContextIcon} aria-hidden="true">
              <CortexIcon name="smart" size={14} />
            </span>
            <span>Smart</span>
          </span>
        ) : mode === "single" ? (
          <ModelDockChip modelKey={selectedModelKey} models={availableModels} />
        ) : (
          <CompareDockChip modelKeys={compareModelKeys} models={availableModels} />
        )}
      </span>
      <span className={styles.composerDockPlaceholder}>Ask a follow-up…</span>
      <span className={styles.composerDockSend} aria-hidden="true">
        <CortexIcon name="send" size={18} />
      </span>
    </button>
  );
}

function ModelDockChip({ modelKey, models }: { modelKey: string; models: ModelCatalogItem[] }) {
  const model = resolveDockModel(modelKey, models, 0);
  const meta = getModelPresentation(model.provider, model.model);

  return (
    <span className={styles.dockContextChip}>
      <span className={styles.dockModelIcon}>
        <ProviderLogo
          provider={model.provider}
          logoUrl={meta.logoUrl}
          color={meta.color}
          size={14}
        />
      </span>
      <span>{meta.label}</span>
    </span>
  );
}

function CompareDockChip({
  modelKeys,
  models,
}: {
  modelKeys: [string, string, string];
  models: ModelCatalogItem[];
}) {
  const stackModels = modelKeys
    .filter(Boolean)
    .slice(0, 2)
    .map((key, index) => resolveDockModel(key, models, index));

  while (stackModels.length < 2 && models[stackModels.length]) {
    stackModels.push(models[stackModels.length]!);
  }

  return (
    <span className={styles.dockContextChip}>
      <span className={styles.dockAvatarStack} aria-hidden="true">
        {stackModels.map((model, index) => {
          const meta = getModelPresentation(model.provider, model.model);
          return (
            <span key={`${model.provider}:${model.model}:${index}`} className={styles.dockAvatar}>
              <ProviderLogo
                provider={model.provider}
                logoUrl={meta.logoUrl}
                color={meta.color}
                size={16}
              />
            </span>
          );
        })}
      </span>
      <span>Compare</span>
    </span>
  );
}

function resolveDockModel(
  key: string,
  models: ModelCatalogItem[],
  fallbackIndex: number,
): Pick<ModelCatalogItem, "provider" | "model"> {
  const found = models.find((candidate) => `${candidate.provider}:${candidate.model}` === key);
  if (found) return found;

  const separator = key.indexOf(":");
  if (separator >= 0) {
    return {
      provider: key.slice(0, separator),
      model: key.slice(separator + 1),
    };
  }

  return models[fallbackIndex] ?? models[0] ?? DEFAULT_MODELS[0]!;
}

function MobileHistory({
  onSelectThread,
  onDeleteThread,
}: {
  onSelectThread: (thread: HistoryThread) => void;
  onDeleteThread: (thread: HistoryThread) => void;
}) {
  const [confirmingDeleteKey, setConfirmingDeleteKey] = useState<string | null>(null);
  const deleteConfirmTimerRef = useRef<number | null>(null);
  const history = useChatStore((s) => s.history);
  const historySearch = useChatStore((s) => s.historySearch);
  const setHistorySearch = useChatStore((s) => s.setHistorySearch);
  const sessionId = useChatStore((s) => s.sessionId);
  const filteredThreads = useMemo(() => {
    return filterHistoryThreads(buildHistoryThreads(history), historySearch);
  }, [history, historySearch]);
  const historyGroups = useMemo(() => {
    return groupMobileHistoryThreads(filteredThreads);
  }, [filteredThreads]);

  useEffect(() => {
    return () => {
      if (deleteConfirmTimerRef.current !== null) {
        window.clearTimeout(deleteConfirmTimerRef.current);
      }
    };
  }, []);

  const showDeleteConfirm = (threadKey: string) => {
    if (deleteConfirmTimerRef.current !== null) {
      window.clearTimeout(deleteConfirmTimerRef.current);
    }
    setConfirmingDeleteKey(threadKey);
    deleteConfirmTimerRef.current = window.setTimeout(() => {
      setConfirmingDeleteKey((current) => (current === threadKey ? null : current));
      deleteConfirmTimerRef.current = null;
    }, 3000);
  };

  const clearDeleteConfirm = () => {
    if (deleteConfirmTimerRef.current !== null) {
      window.clearTimeout(deleteConfirmTimerRef.current);
      deleteConfirmTimerRef.current = null;
    }
    setConfirmingDeleteKey(null);
  };

  const handleDeleteClick = (event: MouseEvent<HTMLButtonElement>, thread: HistoryThread) => {
    event.stopPropagation();
    showDeleteConfirm(thread.key);
  };

  const handleConfirmDelete = (event: MouseEvent<HTMLButtonElement>, thread: HistoryThread) => {
    event.stopPropagation();
    clearDeleteConfirm();
    onDeleteThread(thread);
  };

  const handleCancelDelete = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    clearDeleteConfirm();
  };

  return (
    <section className={styles.mobileHistory} aria-label="History">
      <div className={styles.mobileHistorySearch}>
        <CortexIcon name="search" />
        <input
          id="mobileHistorySearch"
          value={historySearch}
          onChange={(event) => setHistorySearch(event.target.value)}
          placeholder="Search history"
          aria-label="Search history"
        />
      </div>
      <ul className={styles.mobileHistoryGroups}>
        {historyGroups.map((group) => (
          <li key={group.key} className={styles.mobileHistoryGroup}>
            <span className={styles.mobileHistoryGroupLabel}>{group.label}</span>
            <ul className={styles.mobileHistoryList}>
              {group.threads.map((thread) => {
                const isConfirmingDelete = confirmingDeleteKey === thread.key;
                const dateTimeLabel = formatHistoryDateTime(thread.latestTimestamp);

                return (
                  <li key={thread.key} className={styles.mobileHistoryItem}>
                    {isConfirmingDelete ? (
                      <div
                        className={styles.mobileHistoryDeleteConfirm}
                        role="group"
                        aria-label="Confirm delete chat"
                      >
                        <span className={styles.mobileHistoryDeleteConfirmText}>Delete?</span>
                        <button
                          type="button"
                          className={styles.mobileHistoryDeleteConfirmButton}
                          onClick={(event) => handleConfirmDelete(event, thread)}
                          aria-label="Confirm delete chat"
                        >
                          Delete
                        </button>
                        <button
                          type="button"
                          className={styles.mobileHistoryDeleteCancelButton}
                          onClick={handleCancelDelete}
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <div
                        className={
                          thread.sessionId === sessionId
                            ? `${styles.mobileHistorySurface} ${styles.mobileHistoryActive}`
                            : styles.mobileHistorySurface
                        }
                        onClick={() => onSelectThread(thread)}
                      >
                        <span className={styles.mobileHistoryTop}>
                          <span className={styles.mobileHistoryMode} data-mode={thread.mode}>
                            {formatHistoryMode(thread.mode)}
                          </span>
                          <time dateTime={thread.latestTimestamp}>
                            {dateTimeLabel || "Date unavailable"}
                          </time>
                        </span>
                        <span className={styles.mobileHistoryTitleRow}>
                          <button
                            type="button"
                            className={styles.mobileHistoryTitleButton}
                            aria-label={`${thread.title}. ${formatHistoryMode(thread.mode)}, ${
                              dateTimeLabel || "Date unavailable"
                            }`}
                            aria-current={thread.sessionId === sessionId ? "page" : undefined}
                          >
                            <span className={styles.mobileHistoryTitle}>{thread.title}</span>
                          </button>
                          <button
                            type="button"
                            className={styles.mobileHistoryDeleteButton}
                            onClick={(event) => handleDeleteClick(event, thread)}
                            aria-label="Delete chat"
                            title="Delete chat"
                          >
                            <CortexIcon name="trash" size={15} strokeWidth={1.8} />
                          </button>
                        </span>
                        <small className={styles.mobileHistoryMeta}>
                          <span>
                            {thread.turnCount} {thread.turnCount === 1 ? "turn" : "turns"}
                          </span>
                          <span aria-hidden="true">·</span>
                          <span className={styles.mobileHistoryModel}>{thread.modelLabel}</span>
                        </small>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}

function formatHistoryMode(mode: HistoryThread["mode"]): string {
  if (mode === "single") return "Ask";
  if (mode === "compare") return "Compare";
  return "Mixed";
}

function groupMobileHistoryThreads(threads: HistoryThread[]): MobileHistoryDateGroup[] {
  const groups: MobileHistoryDateGroup[] = [];

  for (const thread of threads) {
    const label = formatMobileHistoryGroupLabel(thread.latestTimestamp);
    const groupKey = label;
    const group = groups.find((candidate) => candidate.key === groupKey);

    if (group) {
      group.threads.push(thread);
    } else {
      groups.push({
        key: groupKey,
        label,
        threads: [thread],
      });
    }
  }

  return groups;
}

function formatMobileHistoryGroupLabel(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "Date unavailable";

  const today = new Date();
  if (isSameLocalDate(date, today)) return "Today";

  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (isSameLocalDate(date, yesterday)) return "Yesterday";

  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

function isSameLocalDate(left: Date, right: Date): boolean {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
}
