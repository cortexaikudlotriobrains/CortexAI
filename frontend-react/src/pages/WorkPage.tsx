import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useNavigate, useParams } from "react-router-dom";
import {
  beginToolOAuth,
  cancelWorkRun,
  createToolConnection,
  createWorkSession,
  decideWorkApproval,
  getWorkApproval,
  getWorkEvents,
  getWorkRun,
  getWorkSession,
  listToolCatalog,
  listToolConnections,
  listWorkArtifacts,
  listWorkRuns,
  listWorkSessions,
  sendWorkInstruction,
  startWorkRun,
  streamWorkEvents,
  testToolConnection,
} from "../api/work";
import { ApiClientError, makeRequestId } from "../api/client";
import { AccountMenu } from "../components/layout/AccountMenu";
import { Sidebar } from "../components/layout/Sidebar";
import { SubscriptionBanner } from "../components/subscription/SubscriptionBanner";
import { CortexIcon } from "../components/shared/CortexIcon";
import { WorkApproval } from "../components/work/WorkApproval";
import { WorkArtifacts } from "../components/work/WorkArtifacts";
import { WorkComposer } from "../components/work/WorkComposer";
import { WorkRail } from "../components/work/WorkRail";
import { WorkStatusPill } from "../components/work/WorkStatusPill";
import { getRuntimeConfig } from "../config/runtimeConfig";
import { useAuth } from "../hooks/useAuth";
import { useSubscription } from "../hooks/useSubscription";
import { useTheme } from "../hooks/useTheme";
import { useAttachmentUploadStore } from "../store/attachmentUploadStore";
import { useWorkStore, type WorkRunHistoryItem } from "../store/workStore";
import {
  beginAttachmentUploads,
  removeAttachmentUpload,
  retryAttachmentUpload,
} from "../uploads/attachmentUploadQueue";
import { getAccountMenuSubscriptionPresentation } from "../subscription/accountMenuPresentation";
import type { WorkArtifact, WorkEvent, WorkRun, WorkSession } from "../types";
import { formatAiCredits } from "../utils/aiCredits";
import brandMarkUrl from "../assets/brand/brand-mark.svg";
import styles from "../components/work/Work.module.css";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "budget_exhausted", "output_limit_reached"]);
const EXAMPLES = [
  "Analyze these files and create a report",
  "Research this topic and prepare a summary",
  "Review this GitHub repository and identify problems",
  "Turn these files into an Excel analysis",
];

export function WorkPage() {
  const { workSessionId } = useParams();
  const navigate = useNavigate();
  const { whoAmI, cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const authEnabled = cognitoConfig?.enabled ?? false;
  const signedOut = !authLoading && authEnabled && !loggedIn;
  const workspaceReady = !authLoading && !signedOut;
  const subscription = useSubscription({ authLoading, loggedIn });
  const accountSubscription = getAccountMenuSubscriptionPresentation(subscription.entitlements);
  const { theme, toggleTheme } = useTheme();
  const runtimeWorkEnabled = getRuntimeConfig().workEnabled !== false;
  const planWorkEnabled = subscription.entitlements?.features.work_enabled ?? false;
  const maxPlanBudget = subscription.entitlements?.limits.max_work_credit_budget || 1_000_000;
  const [instruction, setInstruction] = useState("");
  const [workUploadIds, setWorkUploadIds] = useState<string[]>([]);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [startPending, setStartPending] = useState(false);
  const [startingInstruction, setStartingInstruction] = useState("");
  const streamAbortRef = useRef<AbortController | null>(null);
  const allUploadTasks = useAttachmentUploadStore((state) => state.tasks);
  const uploadTasks = useMemo(
    () => allUploadTasks.filter((task) => workUploadIds.includes(task.clientId)),
    [allUploadTasks, workUploadIds],
  );
  const store = useWorkStore();
  const streamRunId = store.run?.id;
  const streamRunStatus = store.run?.status;

  const refreshSessions = useCallback(async () => {
    if (!runtimeWorkEnabled || !workspaceReady) return;
    try {
      useWorkStore.getState().setSessions(await listWorkSessions());
    } catch (error) {
      if (!(error instanceof ApiClientError && error.status === 404)) throw error;
    }
  }, [runtimeWorkEnabled, workspaceReady]);

  const refreshTools = useCallback(async () => {
    if (!runtimeWorkEnabled || !workspaceReady || !planWorkEnabled) return;
    const [catalog, connections] = await Promise.all([listToolCatalog(), listToolConnections()]);
    useWorkStore.getState().setToolCatalog(catalog);
    useWorkStore.getState().setConnections(connections);
  }, [planWorkEnabled, runtimeWorkEnabled, workspaceReady]);

  const fetchRunHistoryItem = useCallback(async (run: WorkRun): Promise<WorkRunHistoryItem> => {
    const [eventResponse, artifacts] = await Promise.all([
      getWorkEvents(run.id),
      listWorkArtifacts(run.id),
    ]);
    return { run, events: eventResponse.items, artifacts };
  }, []);

  const activateHistoryItem = useCallback(async (item: WorkRunHistoryItem) => {
    const state = useWorkStore.getState();
    state.setRun(item.run);
    state.replaceEvents(item.events);
    state.setArtifacts(item.artifacts);
    state.setApproval(await pendingApprovalFromEvents(item.events));
  }, []);

  useEffect(() => {
    if (!workspaceReady || !runtimeWorkEnabled) return;
    let cancelled = false;
    useWorkStore.getState().setLoading(true);
    useWorkStore.getState().setError(null);
    void Promise.all([refreshSessions(), refreshTools()])
      .then(async () => {
        if (!workSessionId) {
          useWorkStore.getState().resetWorkspace();
          return;
        }
        const session = await getWorkSession(workSessionId);
        if (cancelled) return;
        useWorkStore.getState().setSession(session);
        const runs = await listWorkRuns(workSessionId);
        if (cancelled) return;
        const history = await Promise.all(runs.map(fetchRunHistoryItem));
        if (cancelled) return;
        useWorkStore.getState().setHistory(history);
        const latest = history.at(-1);
        if (latest) {
          await activateHistoryItem(latest);
        } else {
          useWorkStore.getState().setRun(null);
          useWorkStore.getState().replaceEvents([]);
          useWorkStore.getState().setArtifacts([]);
          useWorkStore.getState().setApproval(null);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) useWorkStore.getState().setError(errorMessage(error));
      })
      .finally(() => {
        if (!cancelled) useWorkStore.getState().setLoading(false);
      });
    return () => { cancelled = true; };
  }, [activateHistoryItem, fetchRunHistoryItem, refreshSessions, refreshTools, runtimeWorkEnabled, workSessionId, workspaceReady]);

  useEffect(() => {
    const run = useWorkStore.getState().run;
    if (!run || TERMINAL_STATUSES.has(run.status) || !runtimeWorkEnabled) return;
    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;
    useWorkStore.getState().setStreaming(true);
    const after = useWorkStore.getState().events.at(-1)?.sequence ?? 0;
    const finishTerminalSync = async (terminalRun: WorkRun, afterSequence: number) => {
      const [remainingEvents, artifacts] = await Promise.all([
        getWorkEvents(run.id, afterSequence),
        listWorkArtifacts(run.id),
      ]);
      const state = useWorkStore.getState();
      for (const remainingEvent of remainingEvents.items) {
        state.appendEvent(remainingEvent);
      }
      state.setRun(terminalRun);
      state.setArtifacts(artifacts);
      state.setApproval(await pendingApprovalFromEvents(remainingEvents.items));
      const finalState = useWorkStore.getState();
      finalState.upsertHistoryItem({
        run: terminalRun,
        events: finalState.events,
        artifacts,
      });
      await refreshSessions();
    };
    void streamWorkEvents(
      run.id,
      after,
      async (event) => {
        useWorkStore.getState().appendEvent(event);
        if (event.type === "approval_required") {
          useWorkStore.getState().setApproval(await pendingApprovalFromEvents([event]));
        }
        if (event.type === "artifact_created") {
          useWorkStore.getState().setArtifacts(await listWorkArtifacts(run.id));
        }
        if (isTerminalEvent(event)) {
          await finishTerminalSync(await getWorkRun(run.id), event.sequence);
          return true;
        }
        const refreshed = await getWorkRun(run.id);
        if (TERMINAL_STATUSES.has(refreshed.status)) {
          await finishTerminalSync(refreshed, event.sequence);
          return true;
        }
        useWorkStore.getState().setRun(refreshed);
        return false;
      },
      controller.signal,
    ).catch((error: unknown) => {
      if (!controller.signal.aborted) useWorkStore.getState().setError(errorMessage(error));
    }).finally(() => {
      if (!controller.signal.aborted) useWorkStore.getState().setStreaming(false);
    });
    return () => controller.abort();
  }, [refreshSessions, runtimeWorkEnabled, streamRunId, streamRunStatus]);

  useEffect(() => {
    const state = useWorkStore.getState();
    if (state.maxCreditBudget > maxPlanBudget) {
      state.setMaxCreditBudget(Math.min(1_000_000, maxPlanBudget));
    }
  }, [maxPlanBudget]);

  const active = Boolean(store.run && !TERMINAL_STATUSES.has(store.run.status));
  const resultText = useMemo(() => latestAgentMessage(store.events), [store.events]);

  const handleStart = async () => {
    if (!planWorkEnabled) {
      navigate("/pricing");
      return;
    }
    const state = useWorkStore.getState();
    const submittedInstruction = instruction.trim();
    if (!submittedInstruction || active || state.loading || startPending) return;
    setStartingInstruction(submittedInstruction);
    setStartPending(true);
    state.setError(null);
    state.setLoading(true);
    try {
      const session = await useWorkStore.getState().ensureSession(
        () => createWorkSession(submittedInstruction.slice(0, 120)),
      );
      const submitState = useWorkStore.getState();
      const payload = {
        instruction: submittedInstruction,
        input_file_ids: uploadTasks.filter((task) => task.state === "ready" && task.fileId).map((task) => task.fileId!),
        enabled_connection_ids: submitState.enabledConnectionIds,
        web_mode: submitState.webMode,
        max_credit_budget: submitState.maxCreditBudget,
      };
      const requestId = makeRequestId("work-ui");
      if (submitState.run) {
        submitState.upsertHistoryItem({
          run: submitState.run,
          events: submitState.events,
          artifacts: submitState.artifacts,
        });
      }
      const run = submitState.run
        ? await sendWorkInstruction(session.id, payload, requestId)
        : await startWorkRun(session.id, payload, requestId);
      submitState.setRun(run);
      submitState.replaceEvents([]);
      submitState.setArtifacts([]);
      submitState.setApproval(null);
      submitState.upsertHistoryItem({ run, events: [], artifacts: [] });
      setInstruction("");
      for (const id of workUploadIds) useAttachmentUploadStore.getState().removeTask(id);
      setWorkUploadIds([]);
      navigate(`/work/${session.id}`, { replace: true });
      await refreshSessions();
    } catch (error) {
      store.setError(errorMessage(error));
    } finally {
      setStartPending(false);
      store.setLoading(false);
    }
  };

  const handleCancel = async () => {
    if (!store.run) return;
    store.setLoading(true);
    try {
      const cancelledRun = await cancelWorkRun(store.run.id);
      store.setRun(cancelledRun);
      store.upsertHistoryItem({
        run: cancelledRun,
        events: useWorkStore.getState().events,
        artifacts: useWorkStore.getState().artifacts,
      });
      await refreshSessions();
    } catch (error) {
      store.setError(errorMessage(error));
    } finally {
      store.setLoading(false);
    }
  };

  const handleApproval = async (decision: "approve" | "deny", remember = false) => {
    if (!store.approval) return;
    setApprovalBusy(true);
    try {
      await decideWorkApproval(store.approval.id, decision, undefined, remember);
      if (store.run) {
        const [refreshed, eventResponse] = await Promise.all([
          getWorkRun(store.run.id),
          getWorkEvents(store.run.id),
        ]);
        store.setRun(refreshed);
        store.replaceEvents(eventResponse.items);
        store.setApproval(await pendingApprovalFromEvents(eventResponse.items));
      } else {
        store.setApproval(null);
      }
    } catch (error) {
      store.setError(errorMessage(error));
    } finally {
      setApprovalBusy(false);
    }
  };

  const handleFiles = async (files: File[]) => {
    try {
      const ids = await beginAttachmentUploads(files);
      setWorkUploadIds((current) => [...current, ...ids]);
    } catch (error) {
      store.setError(errorMessage(error));
    }
  };

  const handleConnect = async (connectorKey: string) => {
    try {
      const result = await beginToolOAuth(connectorKey, window.location.pathname);
      window.location.assign(result.authorization_url);
    } catch (error) {
      store.setError(errorMessage(error));
    }
  };

  const handleAddMcp = async (name: string, url: string) => {
    try {
      const connection = await createToolConnection({
        display_name: name,
        server_url: url,
        auth_type: "none",
      });
      await testToolConnection(connection.id);
      await refreshTools();
    } catch (error) {
      store.setError(errorMessage(error));
      throw error;
    }
  };

  const handleNewWork = () => {
    streamAbortRef.current?.abort();
    store.resetWorkspace();
    setInstruction("");
    navigate("/work");
  };

  const handleSelectWorkSession = (session: WorkSession) => navigate(`/work/${session.id}`);
  const handleLogout = () => {
    streamAbortRef.current?.abort();
    store.resetWorkspace();
    void logout();
  };

  return (
    <div className={styles.layout} data-theme={theme}>
      <Sidebar
        onSelectThread={() => undefined}
        activeView="work"
        onNavigateChat={(mode) => navigate(mode === "compare" ? "/?mode=compare" : "/")}
        onNavigateWork={runtimeWorkEnabled ? () => navigate("/work") : undefined}
        onNavigateUsage={() => navigate("/usage")}
        onNavigateCredits={() => navigate("/credits")}
        onNavigateModels={() => navigate("/models")}
        newLabel="New work"
        onNew={handleNewWork}
        workSessions={store.sessions}
        activeWorkSessionId={store.session?.id}
        onSelectWorkSession={handleSelectWorkSession}
        whoAmI={whoAmI}
        loggedIn={loggedIn}
        onLogin={authEnabled ? login : undefined}
        signedOut={signedOut}
      />
      <main className={styles.main}>
        <header className={styles.mobileTopbar}>
          <span className={styles.mobileBrand}><img src={brandMarkUrl} alt="" /><span>CortexAI</span></span>
          <div className={styles.mobileHeaderActions}>
            <button type="button" className={styles.iconButton} aria-label="New work" onClick={handleNewWork}><CortexIcon name="new-chat" /></button>
            <AccountMenu authEnabled={authEnabled} loggedIn={loggedIn} onLogin={login} onLogout={handleLogout} planLabel={accountSubscription.planLabel} billingActionLabel={accountSubscription.billingActionLabel} billingPastDue={accountSubscription.billingPastDue} onBilling={accountSubscription.billingDestination ? () => navigate(accountSubscription.billingDestination!) : undefined} onModels={() => navigate("/models")} onUsageInsights={() => navigate("/usage")} onCredits={() => navigate("/credits")} theme={theme} onToggleTheme={toggleTheme} />
          </div>
        </header>
        <header className={styles.topbar}>
          <nav className={styles.tabs} aria-label="Workspace mode">
            <button type="button" onClick={() => navigate("/")}>Ask</button>
            <button type="button" onClick={() => navigate("/?mode=compare")}>Compare</button>
            <button type="button" className={styles.activeTab} aria-current="page">Work</button>
          </nav>
          <div className={styles.topActions}>
            <button type="button" className={styles.iconButton} aria-label="New work" onClick={handleNewWork}><CortexIcon name="plus" /></button>
            <AccountMenu authEnabled={authEnabled} loggedIn={loggedIn} onLogin={login} onLogout={handleLogout} planLabel={accountSubscription.planLabel} billingActionLabel={accountSubscription.billingActionLabel} billingPastDue={accountSubscription.billingPastDue} onBilling={accountSubscription.billingDestination ? () => navigate(accountSubscription.billingDestination!) : undefined} onModels={() => navigate("/models")} onUsageInsights={() => navigate("/usage")} onCredits={() => navigate("/credits")} theme={theme} onToggleTheme={toggleTheme} />
          </div>
        </header>
        <SubscriptionBanner entitlements={subscription.entitlements} onManageBilling={() => navigate("/account/billing")} />
        {authLoading ? <CenteredMessage>Preparing your workspace...</CenteredMessage> : signedOut ? (
          <CenteredMessage><strong>Sign in to use Cortex Work</strong><button className={styles.inkButton} onClick={login}>Sign in</button></CenteredMessage>
        ) : !runtimeWorkEnabled ? (
          <CenteredMessage><strong>Cortex Work is not enabled in this environment.</strong><span>Ask and Compare remain available.</span></CenteredMessage>
        ) : !planWorkEnabled ? (
          <CenteredMessage><CortexIcon name="work" size={28} /><strong>Upgrade to use Cortex Work</strong><span>Delegate multi-step tasks, connected tools, and deliverables on Plus or Pro.</span><button className={styles.startButton} onClick={() => navigate("/pricing")}>View plans</button></CenteredMessage>
        ) : startPending && !store.run ? (
          <WorkStartingView instruction={startingInstruction} />
        ) : store.loading && !store.session && !store.run ? <CenteredMessage>Loading Work...</CenteredMessage> : store.run ? (
          <WorkSessionView
            session={store.session}
            run={store.run}
            history={store.history}
            resultText={resultText}
            onCancel={() => void handleCancel()}
            approval={store.approval ? <WorkApproval approval={store.approval} busy={approvalBusy} onApprove={(remember) => void handleApproval("approve", remember)} onDeny={() => void handleApproval("deny")} /> : null}
            artifacts={store.artifacts}
            rail={<WorkRail run={store.run} events={store.events} connections={store.connections} enabledConnectionIds={store.enabledConnectionIds} />}
            composer={<WorkComposer value={instruction} onChange={setInstruction} onSubmit={() => void handleStart()} onFiles={(files) => void handleFiles(files)} onRemoveFile={(id) => void removeAttachmentUpload(id).then(() => setWorkUploadIds((items) => items.filter((item) => item !== id)))} onRetryFile={(id) => void retryAttachmentUpload(id)} tasks={uploadTasks} connections={store.connections} catalog={store.toolCatalog} enabledConnectionIds={store.enabledConnectionIds} onToggleConnection={store.toggleConnection} onConnect={(key) => void handleConnect(key)} onAddMcp={handleAddMcp} webMode={store.webMode} onWebModeChange={store.setWebMode} maxCreditBudget={store.maxCreditBudget} maxPlanBudget={maxPlanBudget} onBudgetChange={store.setMaxCreditBudget} busy={store.loading} disabled={active} followup />}
          />
        ) : (
          <WorkLanding
            instruction={instruction}
            onInstruction={setInstruction}
            onExample={setInstruction}
            composer={<WorkComposer value={instruction} onChange={setInstruction} onSubmit={() => void handleStart()} onFiles={(files) => void handleFiles(files)} onRemoveFile={(id) => void removeAttachmentUpload(id).then(() => setWorkUploadIds((items) => items.filter((item) => item !== id)))} onRetryFile={(id) => void retryAttachmentUpload(id)} tasks={uploadTasks} connections={store.connections} catalog={store.toolCatalog} enabledConnectionIds={store.enabledConnectionIds} onToggleConnection={store.toggleConnection} onConnect={(key) => void handleConnect(key)} onAddMcp={handleAddMcp} webMode={store.webMode} onWebModeChange={store.setWebMode} maxCreditBudget={store.maxCreditBudget} maxPlanBudget={maxPlanBudget} onBudgetChange={store.setMaxCreditBudget} busy={store.loading} />}
          />
        )}
        {store.error && <div className={styles.errorBanner} role="alert"><span>{store.error}</span><button type="button" onClick={() => store.setError(null)}>Dismiss</button></div>}
        {workspaceReady && <nav className={styles.mobileNav} aria-label="Mobile navigation"><button onClick={() => navigate("/")}><CortexIcon name="ask" /><span>Ask</span></button><button onClick={() => navigate("/?mode=compare")}><CortexIcon name="compare" /><span>Compare</span></button><button className={styles.mobileNavActive} aria-current="page"><CortexIcon name="work" /><span>Work</span></button><button onClick={() => navigate("/?panel=history")}><CortexIcon name="history" /><span>History</span></button></nav>}
      </main>
    </div>
  );
}

function WorkLanding({ instruction, onInstruction, onExample, composer }: { instruction: string; onInstruction: (value: string) => void; onExample: (value: string) => void; composer: React.ReactNode }) {
  void instruction;
  void onInstruction;
  return <section className={styles.landing}><span className={styles.landingMark}><CortexIcon name="sparkle" size={22} /></span><p className={styles.eyebrow}>Work mode</p><h1>What should I work on?</h1><p className={styles.landingCopy}>Give Cortex a task and it can research, analyze your files, use connected tools and create deliverables.</p><div className={styles.exampleList}>{EXAMPLES.map((example) => <button type="button" key={example} onClick={() => onExample(example)}>{example}</button>)}</div><div className={styles.landingComposer}>{composer}</div></section>;
}

function WorkStartingView({ instruction }: { instruction: string }) {
  return (
    <div className={styles.workArea} aria-busy="true">
      <header className={styles.taskHeader}>
        <div>
          <h1 title={instruction}>{instruction}</h1>
          <p>Preparing a durable Work run</p>
        </div>
        <div className={styles.taskHeaderActions}>
          <WorkStatusPill status="starting" />
        </div>
      </header>
      <div className={styles.startingStage} role="status" aria-label="Starting work" aria-live="polite">
        <span className={styles.startingSpinner} aria-hidden="true" />
        <strong>Starting work...</strong>
        <span>Securing the credit budget and connecting the managed agent. This usually takes a few seconds.</span>
      </div>
    </div>
  );
}

function WorkSessionView({ session, run, history, resultText, onCancel, approval, artifacts, rail, composer }: { session: WorkSession | null; run: WorkRun; history: WorkRunHistoryItem[]; resultText?: string | null; onCancel: () => void; approval: React.ReactNode; artifacts: WorkArtifact[]; rail: React.ReactNode; composer: React.ReactNode }) {
  const terminal = TERMINAL_STATUSES.has(run.status);
  const previous = history.filter((item) => item.run.id !== run.id);
  return <div className={styles.workArea}><header className={styles.taskHeader}><div><h1 title={session?.title || run.instruction}>{session?.title || run.instruction}</h1><p>{run.started_at ? `Started ${formatRelative(run.started_at)}` : "Preparing"} · {formatAiCredits(run.actual_credits)} credits</p></div><div className={styles.taskHeaderActions}><WorkStatusPill status={run.status} />{!terminal && <button type="button" className={styles.stopButton} onClick={onCancel}><CortexIcon name="stop" size={15} /> Stop</button>}</div></header><div className={styles.workColumns}><section className={styles.stream}><div className={styles.streamInner}><div className={styles.transcript} aria-label="Work conversation">{previous.map((item) => <WorkTurn key={item.run.id} run={item.run} events={item.events} artifacts={item.artifacts} />)}<WorkTurn run={run} events={[]} artifacts={artifacts} resultText={resultText} current approval={approval} rail={rail} /></div>{!terminal && <button type="button" className={styles.mobileStop} onClick={onCancel}><CortexIcon name="stop" size={15} /> Stop work</button>}</div>{terminal && <div className={styles.composerDock}>{composer}</div>}</section><div className={styles.desktopRail}>{rail}</div></div></div>;
}

function WorkTurn({ run, events, artifacts, resultText, current = false, approval = null, rail = null }: { run: WorkRun; events: WorkEvent[]; artifacts: WorkArtifact[]; resultText?: string | null; current?: boolean; approval?: React.ReactNode; rail?: React.ReactNode }) {
  const terminal = TERMINAL_STATUSES.has(run.status);
  const visibleResult = resultText ?? latestAgentMessage(events);
  return <article className={styles.workTurn} data-work-run-id={run.id} data-current={current || undefined}><div className={styles.turnPrompt}><span>You asked</span><p>{run.instruction}</p></div>{current && !terminal && <p className={styles.narration}>{narration(run.status)}</p>}{current && approval}{terminal && <ResultHeader run={run} resultText={visibleResult} />}<WorkArtifacts runId={run.id} artifacts={artifacts} />{current && <div className={styles.mobileRail}>{rail}</div>}</article>;
}

function ResultHeader({ run, resultText }: { run: WorkRun; resultText?: string | null }) {
  const completed = run.status === "completed";
  const label = completed ? "Work completed" : run.status === "budget_exhausted" ? "Budget reached" : run.status === "output_limit_reached" ? "Output limit reached" : run.status === "cancelled" ? "Work stopped" : "Work failed";
  return <div className={styles.result}><div className={styles.resultMeta}><span className={completed ? styles.resultMarkSuccess : styles.resultMarkWarn}><CortexIcon name={completed ? "check" : "alert"} size={15} /></span><strong>{label}</strong><span>·</span><span>{formatAiCredits(run.actual_credits)} credits</span></div>{resultText ? <div className={styles.resultBody}><ReactMarkdown remarkPlugins={[remarkGfm]}>{resultText}</ReactMarkdown></div> : <p>{run.error_message || (completed ? "Cortex completed the requested work." : "The work ended before a final written outcome was produced.")}</p>}</div>;
}

function CenteredMessage({ children }: { children: React.ReactNode }) { return <div className={styles.centeredMessage}>{children}</div>; }
function latestAgentMessage(events: WorkEvent[]): string | null {
  return [...events].reverse().find((event) => event.type === "agent_message")?.display_message ?? null;
}
function narration(status: string): string { if (status === "waiting_for_approval") return "I need your approval before I can continue with the next action."; if (status === "created" || status === "planning") return "Here’s how I’ll approach this. I’ll start on the first step now — you can change the goal any time."; return "I’m carrying out the plan and recording each verifiable action as it completes."; }
async function pendingApprovalFromEvents(events: WorkEvent[]) {
  const ids = [...events]
    .reverse()
    .flatMap((event) => Array.isArray(event.payload.approval_ids) ? event.payload.approval_ids : [])
    .filter((value): value is string => typeof value === "string");
  for (const id of [...new Set(ids)]) {
    const approval = await getWorkApproval(id).catch(() => null);
    if (approval?.status === "pending") return approval;
  }
  return null;
}
function isTerminalEvent(event: WorkEvent): boolean { return ["run_completed", "run_failed", "run_cancelled", "budget_exhausted", "output_limit_reached"].includes(event.type); }
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : "Cortex Work could not complete that request."; }
function formatRelative(value: string): string { const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000)); if (seconds < 60) return "moments ago"; const minutes = Math.floor(seconds / 60); return `${minutes} min ago`; }
