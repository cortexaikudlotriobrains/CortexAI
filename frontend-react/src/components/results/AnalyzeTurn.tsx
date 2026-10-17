import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  fetchCortexAnalysisRuns,
  fetchAnalysisState,
  type AnalysisState,
} from "../../api/cortexAnalysis";
import { post } from "../../api/client";
import {
  generationForReasoningLevel,
  reasoningLevelFromResponse,
} from "../../reasoning/reasoningLevels";
import { useChatStore } from "../../store/chatStore";
import { getModelPresentation } from "../../config/modelPresentation";
import type { ChatResponse, ChatTurn } from "../../types";
import { collectAnalysisSources } from "../../analysis/analysisSources";
import { CortexAnalysisZone } from "./CortexAnalysisZone";
import { AnalysisSources, type SourceContext } from "./AnalysisSources";
import { ResponseCard } from "./ResponseCard";
import { CompareResponses } from "./CompareResponses";
import styles from "./AnalyzeTurn.module.css";
import { CortexIcon } from "../shared/CortexIcon";

type View = "analysis" | "responses" | "sources";
const VIEWS: View[] = ["analysis", "responses", "sources"];
const LABELS = { analysis: "Cortex Analysis", responses: "Model responses", sources: "Sources" };

export function AnalyzeTurn({
  turn,
  children,
  onAnalyze,
  onRegenerate,
  onRetryWithMoreRoom,
  responseStartIndex,
}: {
  turn: ChatTurn;
  children: ReactNode;
  onAnalyze: (id: string) => Promise<void>;
  onRegenerate: (id: string, index: number) => Promise<void>;
  onRetryWithMoreRoom: (id: string, index: number) => Promise<void>;
  responseStartIndex: number;
}) {
  const [view, setView] = useState<View>("analysis");
  const workspaceMode = useChatStore((state) => state.mode);
  useEffect(() => {
    if (workspaceMode === "compare") setView("analysis");
  }, [workspaceMode]);
  const [selected, setSelected] = useState(0);
  const [evidence, setEvidence] = useState<SourceContext | null>(null);
  const [evidenceReturnView, setEvidenceReturnView] = useState<View>("analysis");
  const [selectedRunId, setSelectedRunId] = useState("");
  const [estimate, setEstimate] = useState<number | null>(null);
  const [analysisState, setAnalysisState] = useState<AnalysisState | null>(null);
  const [responseEstimates, setResponseEstimates] = useState<Record<string, number>>({});
  const contextRef = useRef<HTMLDivElement>(null);
  const runs = turn.analysisRuns ?? [];
  const run = runs.find((item) => item.analysisId === selectedRunId) ?? runs[0];
  const pollGroup = turn.requestGroupId;
  const analysisActive = turn.analysisStatus === "processing";
  const readingPositions = useRef<Partial<Record<View, number>>>({});
  const workspaceRef = useRef<HTMLDivElement>(null);
  const selectResponse = useCallback((index: number) => {
    setSelected(index);
    setEvidence(null);
  }, []);
  const switchView = (next: View) => {
    const scrollRoot = workspaceRef.current?.closest('section[aria-label="Chat transcript"]');
    const currentPosition = scrollRoot?.scrollTop ?? 0;
    if (scrollRoot) readingPositions.current[view] = currentPosition;
    setView(next);
    requestAnimationFrame(() => {
      if (scrollRoot) scrollRoot.scrollTop = readingPositions.current[next] ?? currentPosition;
    });
  };

  useEffect(() => {
    if (turn.status === "streaming" || turn.status === "optimizing") return;
    const controller = new AbortController();
    const store = useChatStore.getState();
    const position = store.turns.findIndex((item) => item.id === turn.id);
    const context = store.turns
      .slice(0, Math.max(0, position))
      .flatMap((item) => [
        { role: "user", content: item.submittedPrompt || item.prompt },
        {
          role: "assistant",
          content: item.responses.map((response) => response.text).join("\n\n"),
        },
      ])
      .slice(-10);
    for (const response of turn.responses) {
      void post<{ estimated_max_ai_credits: number }>(
        "/v1/billing/estimate-generation",
        {
          prompt: turn.submittedPrompt || turn.prompt,
          targets: [{ provider: response.provider, model: response.model }],
          generation: generationForReasoningLevel(
            reasoningLevelFromResponse(response.generation_budget?.requested_reasoning_effort) ??
              "low",
            "auto",
          ),
          research_enabled: !!turn.researchEnabled,
          include_native_search: true,
          context: { conversation_history: context },
        },
        controller.signal,
      )
        .then((value) => {
          if (!controller.signal.aborted)
            setResponseEstimates((current) => ({
              ...current,
              [response.request_id]: value.estimated_max_ai_credits,
            }));
        })
        .catch(() => {
          /* Keep missing estimates distinct from a zero-credit charge. */
        });
    }
    return () => controller.abort();
  }, [
    turn.id,
    turn.status,
    turn.responses,
    turn.prompt,
    turn.submittedPrompt,
    turn.researchEnabled,
  ]);

  // Read-only polling can resume after history navigation. It never authorizes work.
  useEffect(() => {
    if (!pollGroup || turn.status === "streaming" || turn.status === "optimizing") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const state = await fetchAnalysisState(pollGroup, controller.signal);
        if (controller.signal.aborted) return;
        setAnalysisState(state);
        setEstimate(state.estimatedAiCredits);
        const store = useChatStore.getState();
        const current = store.turns.find((item) => item.id === turn.id);
        // A manual POST may still be accepting the job while this read sees
        // the preceding idle/completed intent. Keep polling for its new run.
        if (
          analysisActive &&
          (state.state === "idle" || state.state === "complete") &&
          (!state.analysisId ||
            current?.analysisRuns?.some((run) => run.analysisId === state.analysisId))
        ) {
          timer = setTimeout(() => void refresh(), 1500);
          return;
        }
        if (
          state.state === "queued" ||
          state.state === "processing" ||
          (state.state === "waiting" && !state.errorCode)
        ) {
          store.setTurnAnalysisStatus(turn.id, "processing");
          timer = setTimeout(() => void refresh(), 1500);
        } else {
          const saved = await fetchCortexAnalysisRuns(
            { requestGroupId: pollGroup },
            controller.signal,
          );
          if (controller.signal.aborted) return;
          for (const item of [...saved].reverse()) store.addTurnAnalysisRun(turn.id, item);
          store.setTurnAnalysisStatus(turn.id, state.state === "failed" ? "failed" : "idle");
        }
      } catch {
        // Preserve saved results if status retrieval is temporarily unavailable.
        if (
          !controller.signal.aborted &&
          useChatStore.getState().turns.find((item) => item.id === turn.id)?.analysisStatus ===
            "processing"
        )
          timer = setTimeout(() => void refresh(), 1500);
      }
    };
    void refresh();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [pollGroup, turn.id, turn.status, analysisActive]);

  const openEvidence = (passage: SourceContext) => {
    let index = turn.responses.findIndex((response) => response.request_id === passage.requestId);
    if (index < 0)
      index = turn.responses.findIndex(
        (response) => response.provider === passage.provider && response.model === passage.model,
      );
    if (index >= 0) setSelected(index);
    setEvidenceReturnView(view);
    setEvidence(passage);
    switchView("responses");
    requestAnimationFrame(() => {
      contextRef.current?.focus();
      contextRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  };
  const websites = useMemo(
    () => collectAnalysisSources(turn.responses, run, view === "sources"),
    [turn.responses, run, view],
  );
  const snapshot = evidence
    ? run?.sourceResponses.find(
        (source) =>
          source.requestId === evidence.requestId &&
          source.responseVersion === evidence.responseVersion,
      )
    : undefined;
  const currentEvidenceResponse = evidence
    ? turn.responses.find(
        (response) =>
          response.request_id === evidence.requestId &&
          (response.response_version ?? 1) === evidence.responseVersion,
      )
    : undefined;
  const earlierResponse: ChatResponse | null =
    evidence && !currentEvidenceResponse && snapshot?.content
      ? {
          request_id: evidence.requestId,
          provider: evidence.provider,
          model: evidence.model,
          response_version: evidence.responseVersion,
          text: snapshot.content,
          latency_ms: null,
          estimated_cost: 0,
          cost_currency: "USD",
          token_usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
          error: undefined,
          timestamp: run?.createdAt ?? "",
          ui_status: "complete",
          web_source_items: (snapshot.webSources ?? []).map((source) => ({
            title: source.title,
            url: source.url,
          })),
        }
      : null;
  const earlierUnavailable = !!evidence && !currentEvidenceResponse && !earlierResponse;

  return (
    <div ref={workspaceRef} className={styles.workspace}>
      {children}
      <div className={styles.tabs} role="tablist" aria-label="Read the analysis">
        {VIEWS.map((item, index) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-label={
              item === "analysis"
                ? "Analysis"
                : item === "responses"
                  ? `Model responses ${turn.responses.length}`
                  : `Sources ${websites.length}`
            }
            id={`${turn.id}-${item}-tab`}
            aria-controls={`${turn.id}-${item}-panel`}
            aria-selected={view === item}
            tabIndex={view === item ? 0 : -1}
            onClick={() => switchView(item)}
            onKeyDown={(event) => {
              const next =
                event.key === "ArrowRight"
                  ? (index + 1) % 3
                  : event.key === "ArrowLeft"
                    ? (index + 2) % 3
                    : event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? 2
                        : null;
              if (next === null) return;
              event.preventDefault();
              switchView(VIEWS[next]!);
              document.getElementById(`${turn.id}-${VIEWS[next]}-tab`)?.focus();
            }}
          >
            <CortexIcon
              name={item === "analysis" ? "layers" : item === "responses" ? "compare" : "book-open"}
              size={16}
            />
            {item === "analysis" ? (
              <>
                <span className={styles.desktopLabel}>{LABELS[item]}</span>
                <span className={styles.mobileLabel}>Analysis</span>
              </>
            ) : (
              LABELS[item]
            )}{" "}
            {item === "responses" ? (
              <span className={styles.count}>{turn.responses.length}</span>
            ) : item === "sources" ? (
              <span className={styles.count}>{websites.length}</span>
            ) : null}
          </button>
        ))}
      </div>
      <section
        id={`${turn.id}-analysis-panel`}
        role="tabpanel"
        hidden={view !== "analysis"}
        aria-labelledby={`${turn.id}-analysis-tab`}
        className={styles.panel}
      >
        <CortexAnalysisZone
          turn={turn}
          onAnalyze={onAnalyze}
          estimatedCredits={estimate}
          analysisState={analysisState}
          onResponses={() => switchView("responses")}
          onRetryResponse={(index) => onRegenerate(turn.id, index)}
          responseEstimates={responseEstimates}
          linkedSourceCount={websites.length}
          selectedRunId={selectedRunId}
          onSelectRun={setSelectedRunId}
          onReadPassage={openEvidence}
          onSources={() => {
            switchView("sources");
            document.getElementById(`${turn.id}-sources-tab`)?.focus();
          }}
        />
      </section>
      <section
        id={`${turn.id}-responses-panel`}
        role="tabpanel"
        hidden={view !== "responses"}
        aria-labelledby={`${turn.id}-responses-tab`}
        className={`${styles.panel} ${styles.responsePanel}`}
      >
        {evidence && (
          <div ref={contextRef} tabIndex={-1} className={styles.context}>
            <strong>
              {snapshot?.responseName ??
                getModelPresentation(evidence.provider, evidence.model).label}{" "}
              · Revision {evidence.responseVersion}
            </strong>
            {evidence.quote && (
              <blockquote>
                <mark>{evidence.quote}</mark>
              </blockquote>
            )}
            {earlierResponse && (
              <p>
                Earlier revision used by this analysis.{" "}
                <button type="button" onClick={() => setEvidence(null)}>
                  Return to latest responses
                </button>
              </p>
            )}
            {!earlierResponse && (
              <button type="button" onClick={() => setEvidence(null)}>
                Close passage
              </button>
            )}
            {evidenceReturnView === "sources" && (
              <button
                type="button"
                onClick={() => {
                  setEvidence(null);
                  switchView("sources");
                  document.getElementById(`${turn.id}-sources-tab`)?.focus();
                }}
              >
                Back to Sources
              </button>
            )}
          </div>
        )}
        {earlierUnavailable ? (
          <div className={styles.empty}>
            <p>The full text of this earlier response wasn't stored.</p>
            <button type="button" onClick={() => setEvidence(null)}>
              Return to latest responses
            </button>
          </div>
        ) : earlierResponse ? (
          <ResponseCard response={earlierResponse} loadingMode="compare" />
        ) : (
          <CompareResponses
            turn={turn}
            responseStartIndex={responseStartIndex}
            activeResponseIndex={selected}
            onSelectResponse={selectResponse}
            onRegenerate={onRegenerate}
            onRetryWithMoreRoom={onRetryWithMoreRoom}
            responseEstimates={responseEstimates}
            visible={view === "responses"}
          />
        )}
      </section>
      <section
        id={`${turn.id}-sources-panel`}
        role="tabpanel"
        hidden={view !== "sources"}
        aria-labelledby={`${turn.id}-sources-tab`}
        className={styles.panel}
      >
        <AnalysisSources
          sources={websites}
          passages={run?.structuredResult?.passages ?? []}
          onRead={openEvidence}
        />
      </section>
    </div>
  );
}
