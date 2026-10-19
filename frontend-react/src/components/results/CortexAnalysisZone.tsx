import { useEffect, useState } from "react";
import type { AnalysisPassage, ChatResponse, ChatTurn } from "../../types";
import type { AnalysisState } from "../../api/cortexAnalysis";
import { activityDisplayMessage } from "../../streaming/activityPresentation";
import { isCortexAnalysisRunStale } from "../../analysis/cortexAnalysisStaleness";
import { getModelPresentation } from "../../config/modelPresentation";
import { useResponseUiStore } from "../../store/responseUiStore";
import { formatAiCredits } from "../../utils/aiCredits";
import { ResponseMarkdown } from "./ResponseCard";
import styles from "./CortexAnalysisZone.module.css";
import { CortexIcon } from "../shared/CortexIcon";
import { ProviderLogo } from "../shared/ProviderLogo";

interface Props {
  turn: ChatTurn;
  onAnalyze: (turnId: string) => Promise<void>;
  estimatedCredits?: number | null;
  selectedRunId?: string;
  onSelectRun?: (id: string) => void;
  onReadPassage?: (passage: AnalysisPassage) => void;
  onSources?: () => void;
  linkedSourceCount?: number;
  analysisState?: AnalysisState | null;
  onResponses?: () => void;
  onRetryResponse?: (index: number) => Promise<void>;
  responseEstimates?: Record<string, number>;
}

export function CortexAnalysisZone({
  turn,
  onAnalyze,
  estimatedCredits,
  selectedRunId,
  onSelectRun,
  onReadPassage,
  onSources,
  linkedSourceCount,
  analysisState,
  onResponses,
  onRetryResponse,
  responseEstimates,
}: Props) {
  const runs = turn.analysisRuns ?? [];
  const [localRunId, setLocalRunId] = useState("");
  const [copied, setCopied] = useState(false);
  const run = runs.find((item) => item.analysisId === (selectedRunId || localRunId)) ?? runs[0];
  const feedbackKey = `analysis:${run?.analysisId ?? turn.id}`;
  const feedback = useResponseUiStore((state) => state.feedback[feedbackKey] ?? null);
  const processing = turn.analysisStatus === "processing";
  const streaming = ["streaming", "optimizing"].includes(turn.status);
  const successes = turn.responses.filter(
    (response) => modelProgress(response, turn.status).kind === "done",
  ).length;
  const stale = run ? isCortexAnalysisRunStale(run, turn.responses) : false;
  const result = run?.structuredResult;
  const contributors: { provider: string; model: string; label?: string }[] =
    result?.passages ??
    run?.sourceResponses.map((source) => {
      const response = turn.responses.find((item) => item.request_id === source.requestId);
      return {
        provider: source.provider || response?.provider || "",
        model: source.model || response?.model || "",
        label: source.responseName,
      };
    }) ??
    [];
  const sourceCount = linkedSourceCount ?? result?.linkedSources.length ?? 0;
  const price =
    estimatedCredits == null ? null : `${formatAiCredits(estimatedCredits)} AI credits estimated`;
  const canRun = successes >= 2 && !processing && !streaming && price !== null;
  const waiting =
    streaming ||
    (!run && successes < 2) ||
    (!run && analysisState?.state === "waiting" && !analysisState.errorCode);
  const showProgress = waiting || processing;

  useEffect(() => {
    setCopied(false);
  }, [run?.analysisId]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(timer);
  }, [copied]);

  const copy = async () => {
    if (!run) return;
    const text = [
      result?.headline,
      result?.introduction,
      result?.takeaway ?? run.recommendedAnswer,
      ...(result?.points ?? []).map((point) => point.text),
      ...(result?.differences ?? []).map((item) => item.text),
      ...(result?.limitations ?? []),
      ...(result?.passages ?? []).map(
        (item) =>
          `${getModelPresentation(item.provider, item.model).label} (${item.model}), revision ${item.responseVersion}: “${item.quote}”`,
      ),
    ]
      .filter(Boolean)
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className={styles.zone}>
      {(!showProgress || run) && (
        <header className={styles.header}>
          <div>
            <span className={styles.eyebrow}>
              <CortexIcon name="sparkles" size={16} />
              Cortex synthesis · {run?.combinedResponseCount ?? successes} model responses
            </span>
            {contributors.length > 0 && (
              <div className={styles.byline}>
                <span>From</span>
                {contributors.map((contributor, index) => (
                  <AnalysisModelName
                    key={`${contributor.provider}:${contributor.model}:${index}`}
                    provider={contributor.provider}
                    model={contributor.model}
                    label={contributor.label}
                  />
                ))}
              </div>
            )}
          </div>
          {runs.length > 1 && (
            <label className={styles.runPicker}>
              Saved analysis
              <select
                aria-label="Saved analysis"
                value={run?.analysisId ?? ""}
                onChange={(event) => {
                  setLocalRunId(event.target.value);
                  onSelectRun?.(event.target.value);
                }}
              >
                {runs.map((item, index) => (
                  <option key={item.analysisId} value={item.analysisId}>
                    Run {runs.length - index} · {new Date(item.createdAt).toLocaleString()}
                  </option>
                ))}
              </select>
            </label>
          )}
        </header>
      )}
      {showProgress && (
        <AnalysisProgress
          turn={turn}
          waiting={waiting}
          state={analysisState}
          previousResult={!!run}
          onResponses={onResponses}
          onRetryResponse={onRetryResponse}
          responseEstimates={responseEstimates}
        />
      )}
      {turn.analysisStatus === "failed" && (
        <div role="status" className={styles.notice}>
          <span>
            Analysis couldn't finish. Your model responses
            {run ? " and previous analysis are" : " are"} still available.
          </span>
          <button type="button" disabled={!canRun} onClick={() => void onAnalyze(turn.id)}>
            Retry analysis{price ? ` · ${price}` : ""}
          </button>
        </div>
      )}
      {run && stale && !showProgress && (
        <div role="status" className={styles.notice}>
          <span>An answer changed. This analysis uses earlier revisions.</span>
          <button type="button" disabled={!canRun} onClick={() => void onAnalyze(turn.id)}>
            Update analysis{price ? ` · ${price}` : ""}
          </button>
        </div>
      )}
      {!run && !showProgress && turn.analysisStatus !== "failed" && (
        <div className={styles.notice}>
          <p>
            {successes >= 2
              ? "Bring the model answers together in one clear explanation."
              : "At least two successful answers are needed. Retry a failed response from Model responses."}
          </p>
          {successes >= 2 && (
            <button type="button" disabled={!canRun} onClick={() => void onAnalyze(turn.id)}>
              Create analysis{price ? ` · ${price}` : " · estimate unavailable"}
            </button>
          )}
        </div>
      )}
      {run && (
        <>
          <div className={styles.primary}>
            {(result?.headline || result?.introduction) && (
              <section className={styles.hero} aria-label="Analysis overview">
                {result.headline && <h3>{result.headline}</h3>}
                {result.introduction && (
                  <ResponseMarkdown text={result.introduction} sources={[]} />
                )}
              </section>
            )}
            <section className={styles.takeaway} aria-label="The takeaway">
              <h3>
                <CortexIcon name="layers" size={16} />
                The takeaway
              </h3>
              <ResponseMarkdown text={result?.takeaway ?? run.recommendedAnswer} sources={[]} />
            </section>
            {result?.differences.map((difference, index) => (
              <section key={index} className={styles.difference} aria-label="Meaningful difference">
                <div className={styles.differenceKicker}>
                  <CortexIcon name="split" size={16} />
                  Meaningful difference
                </div>
                {difference.title && <h3>{difference.title}</h3>}
                <ResponseMarkdown text={difference.text} sources={[]} />
                <details>
                  <summary>See both passages</summary>
                  {difference.positions.map((position, positionIndex) => (
                    <div key={positionIndex} className={styles.position}>
                      <ResponseMarkdown text={position.text} sources={[]} />
                      <AnalysisPassages passages={position.evidence} onRead={onReadPassage} />
                    </div>
                  ))}
                </details>
              </section>
            ))}
            {result && (
              <section className={styles.quotes} aria-label="In their words">
                <h3>In their words</h3>
                <AnalysisPassages passages={result.passages} onRead={onReadPassage} />
              </section>
            )}
            {!result && (
              <div className={styles.legacy}>
                <p>Saved with an earlier analysis format. No evidence quotations were stored.</p>
                {run.agreements.map((text, index) => (
                  <p key={`a-${index}`}>{text}</p>
                ))}
                {run.disagreements.map((item, index) => (
                  <p key={`d-${index}`}>
                    <strong>{item.who}:</strong> {item.text}
                  </p>
                ))}
                {run.uniqueInsights.map((item, index) => (
                  <p key={`i-${index}`}>
                    <strong>{item.responseName}:</strong> {item.text}
                  </p>
                ))}
                {run.verify.map((text, index) => (
                  <p key={`v-${index}`}>{text}</p>
                ))}
              </div>
            )}
            <div className={styles.sourceStrip}>
              <button type="button" className={styles.sourcesEntry} onClick={onSources}>
                <CortexIcon name="book-open" size={16} />
                Sources &amp; model passages
              </button>
              <span>
                {sourceCount
                  ? `${sourceCount} linked source${sourceCount === 1 ? "" : "s"}`
                  : "No linked sources supplied"}
              </span>
            </div>
            {!!result?.limitations.length && (
              <section className={styles.limitations}>
                <h4>Limits &amp; assumptions</h4>
                {result.limitations.map((text, index) => (
                  <p key={index}>{text}</p>
                ))}
              </section>
            )}
            <details className={styles.method}>
              <summary>How Cortex made this</summary>
              <p>
                Cortex interpreted the supplied model responses and relevant conversation context.
                Model agreement does not establish independent verification. Linked websites are
                attached metadata; Cortex didn't retrieve their contents for this analysis.
              </p>
            </details>
          </div>
          <footer className={styles.actions}>
            <button
              type="button"
              onClick={() => void copy()}
              aria-label={copied ? "Copied analysis" : "Copy analysis"}
            >
              <CortexIcon name={copied ? "check" : "copy"} size={16} />
              {copied ? "Copied" : "Copy analysis"}
            </button>
            {(["up", "down"] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-label={value === "up" ? "Good analysis" : "Poor analysis"}
                aria-pressed={feedback === value}
                onClick={() =>
                  useResponseUiStore
                    .getState()
                    .setFeedback(feedbackKey, feedback === value ? null : value)
                }
              >
                <CortexIcon name={value === "up" ? "thumb-up" : "thumb-down"} size={16} />
              </button>
            ))}
            <span className={styles.credits}>
              <CortexIcon name="credits" size={16} />
              {formatAiCredits(run.aiCredits)} AI credits
              {run.creditUsageEstimated ? " estimated" : ""}
            </span>
          </footer>
        </>
      )}
    </div>
  );
}

function AnalysisProgress({
  turn,
  waiting,
  state,
  previousResult,
  onResponses,
  onRetryResponse,
  responseEstimates,
}: {
  turn: ChatTurn;
  waiting: boolean;
  state?: AnalysisState | null;
  previousResult: boolean;
  onResponses?: () => void;
  onRetryResponse?: (index: number) => Promise<void>;
  responseEstimates?: Record<string, number>;
}) {
  const statuses = turn.responses.map((response) => modelProgress(response, turn.status));
  const answered = statuses.filter((status) => status.kind === "done").length;
  const hasFailure = statuses.some((status) => status.kind === "failed");
  const modelsRunning = statuses.some((status) => status.kind === "working");
  const needsAnswers = waiting && !modelsRunning && answered < 2;
  const stage = state?.state === "processing" ? state.progressStage : null;
  const activeStep =
    stage === "reading" ? 1 : stage === "synthesizing" || stage === "validating" ? 2 : 0;
  const steps = [
    ["Comparing the answers", "With the model names hidden"],
    ["Reading the supporting passages", "Keeping each model’s words in context"],
    [
      "Bringing the main points together",
      stage === "validating"
        ? "Checking the attributed quotes before saving"
        : "A clear takeaway with attributed quotes",
    ],
  ];
  const heading = needsAnswers
    ? answered === 1
      ? "Only one model answered"
      : "Your models couldn’t finish"
    : waiting
      ? hasFailure
        ? "Cortex will write the analysis from the answers that arrive"
        : `Cortex writes one analysis when ${turn.responses.length === 2 ? "both" : "all three"} answers are in`
      : previousResult
        ? "Updating your analysis"
        : `Writing your analysis from ${answered} answers`;
  const announcement = waiting
    ? `${heading}. ${turn.responses.map((response, index) => `${getModelPresentation(response.provider, response.model).label}: ${statuses[index]!.message}`).join(" ")}`
    : `${heading}. ${steps[activeStep]![0]}. ${steps[activeStep]![1]}.`;

  return (
    <>
      <span
        className={styles.screenReaderStatus}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {announcement}
      </span>
      <section
        className={styles.progress}
        aria-label="Cortex Analysis progress"
        aria-busy={!needsAnswers}
      >
        <div className={styles.progressHeading}>
          <CortexIcon name="sparkles" size={16} />
          <span>{heading}</span>
        </div>
        {waiting ? (
          <>
            <ul className={styles.modelProgress} aria-label="Model response progress">
              {turn.responses.map((response, index) => {
                const status = statuses[index]!;
                const estimate = responseEstimates?.[response.request_id];
                return (
                  <li key={response.request_id} data-state={status.kind}>
                    <div className={styles.modelProgressText}>
                      <strong>
                        <AnalysisModelName provider={response.provider} model={response.model} />
                      </strong>
                      <span>{status.message}</span>
                    </div>
                    <div className={styles.modelProgressIndicator}>
                      {status.kind === "working" ? (
                        <ProgressSpinner />
                      ) : (
                        <CortexIcon
                          name={status.kind === "done" ? "check" : "chip-close"}
                          size={17}
                        />
                      )}
                      {status.kind === "failed" && onRetryResponse && (
                        <button
                          type="button"
                          disabled={
                            estimate == null ||
                            turn.status === "streaming" ||
                            turn.status === "optimizing"
                          }
                          onClick={() => void onRetryResponse(index)}
                        >
                          Retry
                          {estimate != null
                            ? ` · ${formatAiCredits(estimate)} AI credits estimated`
                            : " · estimate unavailable"}
                        </button>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
            <p className={styles.waitNote}>
              {needsAnswers && "Cortex needs at least two answers. Retry a model to continue. "}
              You can read each answer in{" "}
              {onResponses ? (
                <button type="button" onClick={onResponses}>
                  Model responses
                </button>
              ) : (
                "Model responses"
              )}{" "}
              as it arrives.
            </p>
          </>
        ) : (
          <ol className={styles.steps} aria-label="Analysis workflow">
            {steps.map(([title, description], index) => {
              const status =
                index < activeStep ? "done" : index === activeStep ? "active" : "pending";
              return (
                <li
                  key={title}
                  data-state={status}
                  aria-current={status === "active" ? "step" : undefined}
                >
                  <span className={styles.stepNumber} aria-hidden="true">
                    {status === "done" ? (
                      <CortexIcon name="check" size={13} />
                    ) : status === "active" ? (
                      <ProgressSpinner />
                    ) : (
                      index + 1
                    )}
                  </span>
                  <div>
                    <strong>{title}</strong>
                    <p>{description}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
        {previousResult && <p className={styles.waitNote}>Your previous analysis remains below.</p>}
      </section>
    </>
  );
}

function ProgressSpinner() {
  return <span className={styles.spinner} aria-hidden="true" />;
}

function modelProgress(
  response: ChatResponse,
  turnStatus: ChatTurn["status"],
): {
  kind: "done" | "failed" | "working";
  message: string;
} {
  if (response.error || response.ui_status === "failed")
    return { kind: "failed", message: "Didn’t respond" };
  if (response.ui_status === "cancelled") return { kind: "failed", message: "Stopped" };
  if (!["streaming", "optimizing"].includes(turnStatus) && !response.text.trim())
    return { kind: "failed", message: "No answer received" };
  if (response.ui_status === "incomplete")
    return { kind: "done", message: "Partial answer available" };
  if (
    response.ui_status === "complete" ||
    (!response.ui_status &&
      !["streaming", "optimizing"].includes(turnStatus) &&
      response.text.trim())
  ) {
    return {
      kind: "done",
      message:
        response.latency_ms == null
          ? "Answered"
          : `Answered in ${(response.latency_ms / 1000).toFixed(1)}s`,
    };
  }
  if (response.ui_status === "queued") return { kind: "working", message: "Waiting to start" };
  if (response.ui_status === "optimizing" || turnStatus === "optimizing")
    return { kind: "working", message: "Refining your question…" };
  if (response.ui_status === "finalizing")
    return { kind: "working", message: "Finalizing its answer…" };
  return {
    kind: "working",
    message:
      activityDisplayMessage(response.activity) ||
      (response.text.trim() ? "Writing its answer…" : "Connecting to model…"),
  };
}

export function AnalysisPassages({
  passages,
  onRead,
}: {
  passages: AnalysisPassage[];
  onRead?: (passage: AnalysisPassage) => void;
}) {
  return (
    <div className={styles.passages}>
      {passages.map((passage) => {
        return (
          <blockquote
            key={`${passage.requestId}:${passage.responseVersion}:${passage.quote}`}
            className={styles.passage}
          >
            <cite>
              <AnalysisModelName provider={passage.provider} model={passage.model} />
              <span className={styles.revision} aria-label={`Revision ${passage.responseVersion}`}>
                v{passage.responseVersion}
              </span>
            </cite>
            <p>“{passage.quote}”</p>
            <button type="button" onClick={() => onRead?.(passage)}>
              Read in context
              <CortexIcon name="external-link" size={14} />
            </button>
          </blockquote>
        );
      })}
    </div>
  );
}

export function AnalysisModelName({
  provider,
  model,
  label,
}: {
  provider: string;
  model: string;
  label?: string;
}) {
  const presentation = getModelPresentation(provider, model);
  return (
    <span className={styles.modelName} title={`${presentation.providerLabel} · ${model}`}>
      {provider && (
        <ProviderLogo
          provider={provider}
          logoUrl={presentation.logoUrl}
          color={presentation.color}
          size={16}
        />
      )}
      <span>{model ? presentation.label : label || presentation.providerLabel}</span>
    </span>
  );
}
