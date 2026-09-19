import { useEffect, useMemo, useRef, useState, type MouseEvent, type RefObject } from "react";
import type { ChatResponse, ChatTurn, CortexAnalysisRun } from "../../types";
import {
  currentSuccessfulResponses,
  isCortexAnalysisRunStale,
} from "../../analysis/cortexAnalysisStaleness";
import { formatAiCredits } from "../../utils/aiCredits";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./CortexAnalysisZone.module.css";

interface CortexAnalysisZoneProps {
  turn: ChatTurn;
  onAnalyze: (turnId: string) => Promise<void>;
}

const PROCESSING_STEPS = [
  "Reading the responses",
  "Comparing agreements and differences",
  "Building your combined answer",
];

export function CortexAnalysisZone({ turn, onAnalyze }: CortexAnalysisZoneProps) {
  const successfulResponses = useMemo(
    () => currentSuccessfulResponses(turn.responses),
    [turn.responses],
  );
  const runs = turn.analysisRuns ?? [];
  const newestRunId = runs[0]?.analysisId ?? "";
  const [selectedRunId, setSelectedRunId] = useState(newestRunId);
  const resultHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousStatusRef = useRef(turn.analysisStatus ?? "idle");

  useEffect(() => {
    if (newestRunId) setSelectedRunId(newestRunId);
  }, [newestRunId]);

  useEffect(() => {
    const previous = previousStatusRef.current;
    const current = turn.analysisStatus ?? "idle";
    if (previous === "processing" && current === "idle" && newestRunId) {
      resultHeadingRef.current?.focus();
    }
    previousStatusRef.current = current;
  }, [newestRunId, turn.analysisStatus]);

  if (
    successfulResponses.length < 2 &&
    runs.length === 0 &&
    turn.analysisStatus !== "processing" &&
    turn.analysisStatus !== "failed"
  ) {
    return null;
  }

  const selectedRun = runs.find((run) => run.analysisId === selectedRunId) ?? runs[0];
  const savedResult = selectedRun ? (
    <AnalysisResult
      run={selectedRun}
      runs={runs}
      responses={turn.responses}
      headingRef={resultHeadingRef}
      onSelectRun={setSelectedRunId}
      onAnalyze={() => void onAnalyze(turn.id)}
      announceReady={turn.analysisStatus !== "processing" && turn.analysisStatus !== "failed"}
    />
  ) : null;

  if (turn.analysisStatus === "processing") {
    return <ProcessingState responseCount={successfulResponses.length} />;
  }

  if (turn.analysisStatus === "failed") {
    return (
      <>
        <FailureState message={turn.analysisError} onRetry={() => void onAnalyze(turn.id)} />
        {savedResult}
      </>
    );
  }

  if (savedResult) return savedResult;

  return (
    <ReadyState
      responseCount={successfulResponses.length}
      onAnalyze={() => void onAnalyze(turn.id)}
    />
  );
}

function ReadyState({
  responseCount,
  onAnalyze,
}: {
  responseCount: number;
  onAnalyze: () => void;
}) {
  return (
    <section className={`${styles.zoneCard} ${styles.readyCard}`}>
      <TriColourSeam />
      <div className={styles.readyContent}>
        <ConvergenceMark />
        <div className={styles.readyCopy}>
          <h3>Get one better-informed answer</h3>
          <p>
            Cortex will analyze what the models agree on, where they differ, and what each response
            may have missed.
          </p>
        </div>
        <div className={styles.readyAction}>
          <button type="button" className={styles.primaryButton} onClick={onAnalyze}>
            Combine these answers
          </button>
          <span className={styles.modelLine}>From your {responseCount} answers</span>
        </div>
      </div>
    </section>
  );
}

function ProcessingState({ responseCount }: { responseCount: number }) {
  const [activeStep, setActiveStep] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setActiveStep((current) => Math.min(current + 1, PROCESSING_STEPS.length - 1));
    }, 900);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <section
      className={`${styles.zoneCard} ${styles.processingCard}`}
      aria-live="polite"
      aria-atomic="true"
    >
      <TriColourSeam />
      <div className={styles.processingHeader}>
        <span className={styles.spinner} aria-hidden="true" />
        <div>
          <h3>Analyzing responses…</h3>
          <p>Comparing {responseCount} answers · usually a few seconds</p>
        </div>
      </div>
      <ol className={styles.processingSteps}>
        {PROCESSING_STEPS.map((step, index) => {
          const state = index < activeStep ? "done" : index === activeStep ? "active" : "pending";
          return (
            <li key={step} className={styles[`step${capitalize(state)}`]}>
              <span className={styles.stepDot} aria-hidden="true">
                {state === "done" ? "✓" : ""}
              </span>
              <span>{step}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function FailureState({ message, onRetry }: { message?: string; onRetry: () => void }) {
  return (
    <section className={`${styles.zoneCard} ${styles.failureCard}`} role="alert">
      <TriColourSeam />
      <span className={styles.failureIcon} aria-hidden="true">
        !
      </span>
      <div>
        <h3>Cortex couldn&apos;t combine these answers</h3>
        <p>{message ?? "Your model responses are safe above. Nothing was lost."}</p>
      </div>
      <button type="button" className={styles.secondaryButton} onClick={onRetry}>
        Try again
      </button>
    </section>
  );
}

function AnalysisResult({
  run,
  runs,
  responses,
  headingRef,
  onSelectRun,
  onAnalyze,
  announceReady,
}: {
  run: CortexAnalysisRun;
  runs: CortexAnalysisRun[];
  responses: ChatResponse[];
  headingRef: RefObject<HTMLHeadingElement>;
  onSelectRun: (analysisId: string) => void;
  onAnalyze: () => void;
  announceReady: boolean;
}) {
  const [explanationOpen, setExplanationOpen] = useState(false);
  const stale = run.isStale || isCortexAnalysisRunStale(run, responses);
  const modelsConflict = run.disagreements.length > 0 && run.confidence.level === "limited";
  const headingId = `cx-analysis-title-${run.analysisId}`;
  const evidenceOrder = modelsConflict
    ? (["differ", "agree", "insights"] as const)
    : (["agree", "differ", "insights"] as const);
  const visibleEvidence = evidenceOrder.filter((section) => {
    if (section === "agree") return run.agreements.length > 0;
    if (section === "differ") return run.disagreements.length > 0;
    return run.uniqueInsights.length > 0;
  });
  const evidencePosition = (section: (typeof evidenceOrder)[number]) => {
    const index = visibleEvidence.indexOf(section);
    if (index === 0) return styles.evidenceFirst;
    if (index === 1) return styles.evidenceSecond;
    return styles.evidenceThird;
  };
  const openExplanation = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    setExplanationOpen(true);
  };

  return (
    <>
      {runs.length > 1 && (
        <div className={styles.runHistory}>
          <label htmlFor={`analysis-history-${run.requestGroupId}`}>Analysis history</label>
          <select
            id={`analysis-history-${run.requestGroupId}`}
            value={run.analysisId}
            onChange={(event) => onSelectRun(event.target.value)}
          >
            {runs.map((item, index) => (
              <option key={item.analysisId} value={item.analysisId}>
                {index === 0 ? "Latest · " : ""}
                {formatRunDate(item.createdAt)}
                {item.isStale || isCortexAnalysisRunStale(item, responses) ? " · out of date" : ""}
              </option>
            ))}
          </select>
        </div>
      )}
      <section
        className={`${styles.zoneCard} ${styles.resultCard} ${
          modelsConflict ? styles.resultCardDivergent : ""
        }`}
        aria-labelledby={headingId}
      >
        {announceReady && (
          <span className={styles.screenReaderStatus} role="status" aria-live="polite">
            Combined answer ready
          </span>
        )}
        {stale && (
          <span className={styles.screenReaderStatus} role="status" aria-live="polite">
            This combined answer may be out of date
          </span>
        )}
        <TriColourSeam />
        <span className={styles.resultGlow} aria-hidden="true" />
        {stale && (
          <div className={styles.staleBanner}>
            <StaleWarningIcon />
            <p>
              <strong>One answer changed after this analysis</strong>
              <span> — the combined answer below may be out of date.</span>
              <span className={styles.staleMessageMobile}>
                An answer changed — may be out of date
              </span>
            </p>
            <button type="button" aria-label="Update combined answer" onClick={onAnalyze}>
              <UpdateArrowIcon />
              <span className={styles.updateLabelDesktop}>Update combined answer</span>
              <span className={styles.updateLabelMobile}>Update</span>
            </button>
          </div>
        )}

        <header className={styles.resultHeader}>
          <div className={styles.resultIdentity}>
            <CortexResultMark />
            <div className={styles.resultTitleGroup}>
              <h3 id={headingId} ref={headingRef} tabIndex={-1}>
                Cortex Analysis
              </h3>
              <p className={styles.resultMeta}>
                <span>
                  Combined from your {run.combinedResponseCount} answers · not a fourth model
                </span>
                {(run.aiCredits ?? 0) > 0 && (
                  <span className={styles.creditUsage}>
                    <CortexIcon name="cost" size={14} />
                    <span>
                      {formatAiCredits(run.aiCredits)} credits
                      {run.creditUsageEstimated ? " estimated" : ""}
                    </span>
                  </span>
                )}
              </p>
            </div>
          </div>
          <div className={styles.resultActions}>
            <a href="#cortex-analysis-explanation" onClick={openExplanation}>
              How Cortex made this
            </a>
            <button
              type="button"
              className={styles.regenerateButton}
              aria-label="Regenerate analysis"
              title="Regenerate analysis"
              onClick={onAnalyze}
            >
              <RegenerateIcon />
            </button>
          </div>
        </header>

        <div className={styles.answerBlock} data-cx-stagger>
          <div className={`${styles.microLabel} ${styles.answerLabel}`}>The combined answer</div>
          <p className={styles.recommendedAnswer}>{run.recommendedAnswer}</p>
          <ConfidenceLine run={run} />
        </div>

        {visibleEvidence.length > 0 && (
          <div className={styles.evidenceRow} data-cx-stagger>
            {run.agreements.length > 0 && (
              <section
                className={`${styles.evidenceColumn} ${evidencePosition("agree")}`}
                aria-labelledby={`${headingId}-agree`}
                data-cx-order={visibleEvidence.indexOf("agree")}
              >
                <div className={styles.evidenceHeading}>
                  <AgreeIcon />
                  <h4 id={`${headingId}-agree`} className={styles.microLabel}>
                    What they agree on
                  </h4>
                </div>
                <div className={styles.agreementItems}>
                  {run.agreements.map((agreement, index) => (
                    <p key={`${index}-${agreement}`}>{agreement}</p>
                  ))}
                </div>
              </section>
            )}

            {run.disagreements.length > 0 && (
              <section
                className={`${styles.evidenceColumn} ${styles.differColumn} ${evidencePosition("differ")}`}
                aria-labelledby={`${headingId}-differ`}
                data-cx-order={visibleEvidence.indexOf("differ")}
              >
                <div className={styles.evidenceHeading}>
                  <DifferIcon />
                  <h4 id={`${headingId}-differ`} className={styles.microLabel}>
                    Where they differ
                  </h4>
                </div>
                <div className={styles.attributedItems}>
                  {run.disagreements.map((disagreement, index) => (
                    <AttributedItem
                      key={`${disagreement.who}-${index}`}
                      who={disagreement.who}
                      text={disagreement.text}
                    />
                  ))}
                  {run.disagreementNote && (
                    <p className={styles.disagreementNote}>{run.disagreementNote}</p>
                  )}
                </div>
              </section>
            )}

            {run.uniqueInsights.length > 0 && (
              <section
                className={`${styles.evidenceColumn} ${evidencePosition("insights")}`}
                aria-labelledby={`${headingId}-insights`}
                data-cx-order={visibleEvidence.indexOf("insights")}
              >
                <div className={styles.evidenceHeading}>
                  <InsightIcon />
                  <h4 id={`${headingId}-insights`} className={styles.microLabel}>
                    Only one model raised
                  </h4>
                </div>
                <div className={styles.attributedItems}>
                  {run.uniqueInsights.map((insight, index) => (
                    <AttributedItem
                      key={`${insight.responseName}-${index}`}
                      who={insight.responseName}
                      text={insight.text}
                    />
                  ))}
                </div>
              </section>
            )}
          </div>
        )}

        {run.verify.length > 0 && <VerifyBand run={run} />}

        <div className={styles.mobileExplanationFooter}>
          <a href="#cortex-analysis-explanation" onClick={openExplanation}>
            How Cortex made this
          </a>
        </div>
      </section>
      {explanationOpen && <ExplanationDialog onClose={() => setExplanationOpen(false)} />}
    </>
  );
}

function ConfidenceLine({ run }: { run: CortexAnalysisRun }) {
  const filledBars =
    run.confidence.level === "high" ? 3 : run.confidence.level === "moderate" ? 2 : 1;
  return (
    <div className={styles.confidenceLine}>
      <span className={styles.confidenceLead}>
        <span className={styles.confidenceBars} aria-hidden="true">
          {[0, 1, 2].map((index) => (
            <i key={index} className={index < filledBars ? styles.barFilled : ""} />
          ))}
        </span>
        <strong>{capitalize(run.confidence.level)} confidence</strong>
      </span>
      <p>{run.confidence.reason}</p>
    </div>
  );
}

function VerifyBand({ run }: { run: CortexAnalysisRun }) {
  const label = `Worth verifying${run.highStakesDomain ? ` · ${run.highStakesDomain}` : ""}`;
  return (
    <section
      className={styles.verifyBand}
      aria-labelledby={`verify-${run.analysisId}`}
      data-cx-stagger
    >
      <ShieldIcon />
      <div>
        <h4 id={`verify-${run.analysisId}`} className={styles.microLabel}>
          {label}
        </h4>
        <div className={styles.verifyItems}>
          {run.verify.map((item, index) => (
            <div key={`${index}-${item}`}>
              <span aria-hidden="true" />
              <p>{item}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function AttributedItem({ who, text }: { who: string; text: string }) {
  return (
    <div className={styles.attributedItem}>
      <div className={`${styles.attributionName} ${attributionTone(who)}`}>{who}</div>
      <p>{text}</p>
    </div>
  );
}

function attributionTone(name: string) {
  const normalized = name.trim().toLowerCase();
  if (normalized.startsWith("chatgpt") || normalized.startsWith("openai")) {
    return styles.attributionChatgpt;
  }
  if (normalized.startsWith("claude") || normalized.startsWith("anthropic")) {
    return styles.attributionClaude;
  }
  if (normalized.startsWith("gemini") || normalized.startsWith("google")) {
    return styles.attributionGemini;
  }
  return styles.attributionFallback;
}

function CortexResultMark() {
  return (
    <span className={styles.resultMark} aria-hidden="true">
      <svg width="21" height="21" viewBox="0 0 32 32" fill="none" aria-hidden="true">
        <line x1="10" y1="8" x2="10" y2="24" stroke="var(--cx-indigo-bright)" strokeWidth="1.7" />
        <line x1="10" y1="8" x2="22" y2="16" stroke="var(--cx-indigo-bright)" strokeWidth="1.7" />
        <line x1="10" y1="16" x2="22" y2="16" stroke="var(--cx-indigo-bright)" strokeWidth="1.7" />
        <line x1="10" y1="24" x2="22" y2="16" stroke="var(--cx-indigo-bright)" strokeWidth="1.7" />
        <circle cx="10" cy="8" r="2.6" fill="var(--cx-indigo-bright)" />
        <circle cx="10" cy="16" r="2.6" fill="var(--cx-indigo-bright)" />
        <circle cx="10" cy="24" r="2.6" fill="var(--cx-indigo-bright)" />
        <circle cx="22" cy="16" r="3" fill="var(--cx-mark-core)" />
      </svg>
    </span>
  );
}

function AgreeIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="var(--cx-agree)"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function DifferIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="var(--cx-differ)"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M18 8l4 4-4 4M6 8l-4 4 4 4M2 12h20" />
    </svg>
  );
}

function InsightIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="var(--cx-insight)"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.2 1 2h6c0-.8.4-1.5 1-2A7 7 0 0 0 12 2z" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg
      className={styles.shieldIcon}
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="var(--cx-amber)"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}

function RegenerateIcon() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M23 4v6h-6M1 20v-6h6" />
      <path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15" />
    </svg>
  );
}

function StaleWarningIcon() {
  return (
    <svg
      className={styles.staleWarningIcon}
      width="17"
      height="17"
      viewBox="0 0 24 24"
      fill="none"
      stroke="var(--cx-amber)"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 9v4M12 17h.01" />
      <path d="M10.3 3.6 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.6a2 2 0 0 0-3.4 0z" />
    </svg>
  );
}

function UpdateArrowIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M23 4v6h-6" />
      <path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10" />
    </svg>
  );
}

function ExplanationDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    dialogRef.current?.focus();
  }, []);
  return (
    <div className={styles.dialogBackdrop} role="presentation" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="cortex-analysis-explanation-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
      >
        <h3 id="cortex-analysis-explanation-title">How Cortex made this</h3>
        <p>
          Cortex shuffled and anonymized the successful responses before identifying agreements,
          differences, unique observations, and points that may require verification.
        </p>
        <p>
          This is a combined analysis based on the responses, not an independent source of truth.
        </p>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}

function ConvergenceMark() {
  return (
    <span className={styles.convergenceMark} aria-hidden="true">
      <svg viewBox="0 0 32 32">
        <path d="M7 7h5l8 9M7 16h13M7 25h5l8-9" />
        <circle cx="6" cy="7" r="2" />
        <circle cx="6" cy="16" r="2" />
        <circle cx="6" cy="25" r="2" />
        <circle cx="23" cy="16" r="3.5" />
      </svg>
    </span>
  );
}

function TriColourSeam() {
  return <span className={styles.triColourSeam} aria-hidden="true" />;
}

function formatRunDate(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Saved analysis";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

function capitalize<T extends string>(value: T) {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}
