import { type CSSProperties, useEffect, useRef, useState } from "react";
import { getModelPresentation } from "../../config/modelPresentation";
import type { ChatTurn } from "../../types";
import { formatAiCredits } from "../../utils/aiCredits";
import { ProviderLogo } from "../shared/ProviderLogo";
import { ResponseCard } from "./ResponseCard";
import styles from "./ResultsSection.module.css";

interface Props {
  turn: ChatTurn;
  responseStartIndex: number;
  activeResponseIndex: number;
  onSelectResponse: (index: number) => void;
  onRegenerate: (turnId: string, index: number) => Promise<void>;
  onRetryWithMoreRoom: (turnId: string, index: number) => Promise<void>;
  responseEstimates: Record<string, number>;
  visible: boolean;
}

// Preserve the original Compare card grid and phone provider tabs. Analysis has
// its own view; it must not change how the underlying model answers are read.
export function CompareResponses({
  turn,
  responseStartIndex,
  activeResponseIndex,
  onSelectResponse,
  onRegenerate,
  onRetryWithMoreRoom,
  responseEstimates,
  visible,
}: Props) {
  const [responseTabsStuck, setResponseTabsStuck] = useState(false);
  const responseTabsRef = useRef<HTMLDivElement | null>(null);
  const responseTabsSentinelRef = useRef<HTMLDivElement | null>(null);
  const hasResponseTabs = turn.responses.length > 1;
  const compareGridClass =
    turn.responses.length >= 3
      ? styles.compareGridThree
      : turn.responses.length === 2
        ? styles.compareGridTwo
        : styles.compareGridOne;

  useEffect(() => {
    if (activeResponseIndex >= turn.responses.length) {
      onSelectResponse(Math.max(0, turn.responses.length - 1));
    }
  }, [activeResponseIndex, turn.responses.length, onSelectResponse]);

  useEffect(() => {
    if (!hasResponseTabs || !visible) {
      setResponseTabsStuck(false);
      return;
    }
    const tabs = responseTabsRef.current;
    const sentinel = responseTabsSentinelRef.current;
    const scrollRoot = tabs?.closest('section[aria-label="Chat transcript"]') as HTMLElement | null;
    if (!tabs || !sentinel || !scrollRoot) return;
    let frameId: number | null = null;
    const updateStuckState = () => {
      frameId = null;
      const rootTop = scrollRoot.getBoundingClientRect().top;
      const tabsTop = tabs.getBoundingClientRect().top;
      const sentinelTop = sentinel.getBoundingClientRect().top;
      setResponseTabsStuck(sentinelTop < rootTop + 8 && tabsTop <= rootTop + 10);
    };
    const scheduleUpdate = () => {
      if (frameId !== null) return;
      frameId = window.requestAnimationFrame(updateStuckState);
    };
    scheduleUpdate();
    scrollRoot.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate);
    return () => {
      if (frameId !== null) window.cancelAnimationFrame(frameId);
      scrollRoot.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
    };
  }, [hasResponseTabs, turn.id, visible]);

  return (
    <>
      {turn.compareSummary && (
        <div className={`${styles.compareSummary} compare-summary-card`}>
          <span className={`${styles.summaryPill} ${styles.summarySuccess}`}>
            {turn.compareSummary.success_count} succeeded
          </span>
          <span className={`${styles.summaryPill} ${styles.summaryNeutral}`}>
            {turn.compareSummary.error_count} errors
          </span>
          {(turn.compareSummary.total_ai_credits ?? 0) > 0 && (
            <span className={`${styles.summaryPill} ${styles.summaryMono}`}>
              {formatAiCredits(turn.compareSummary.total_ai_credits ?? 0)} credits
            </span>
          )}
        </div>
      )}
      {turn.responses.length > 0 && (
        <>
          <div className={styles.responseHeadingRow}>
            <span>Model responses · {turn.responses.length} models</span>
          </div>
          {hasResponseTabs && (
            <>
              <div
                ref={responseTabsSentinelRef}
                className={styles.mobileResponseTabsSentinel}
                aria-hidden="true"
              />
              <div
                ref={responseTabsRef}
                className={`${styles.mobileResponseTabs} ${responseTabsStuck ? styles.mobileResponseTabsStuck : ""}`}
                role="tablist"
                aria-label="Compare model responses"
              >
                {turn.responses.map((response, index) => {
                  const presentation = getModelPresentation(response.provider, response.model);
                  const selected = index === activeResponseIndex;
                  const tone = responseSwitcherTone(response.provider, presentation.color);
                  const tabStyle = {
                    "--response-tab-accent": tone.accent,
                    "--response-tab-soft": tone.soft,
                  } as CSSProperties;
                  return (
                    <button
                      key={`${turn.id}-tab-${response.request_id}`}
                      type="button"
                      role="tab"
                      id={`${turn.id}-response-tab-${index}`}
                      aria-selected={selected}
                      aria-controls={`${turn.id}-response-panel-${index}`}
                      tabIndex={selected ? 0 : -1}
                      className={selected ? styles.mobileResponseTabActive : undefined}
                      style={tabStyle}
                      onClick={() => onSelectResponse(index)}
                      onKeyDown={(event) => {
                        const next = nextResponseTabIndex(event.key, index, turn.responses.length);
                        if (next === null) return;
                        event.preventDefault();
                        onSelectResponse(next);
                        window.requestAnimationFrame(() =>
                          document.getElementById(`${turn.id}-response-tab-${next}`)?.focus(),
                        );
                      }}
                    >
                      <span className={styles.mobileResponseTabIcon}>
                        <ProviderLogo
                          provider={response.provider}
                          logoUrl={presentation.logoUrl}
                          color={presentation.color}
                          size={13}
                        />
                      </span>
                      <span className={styles.mobileResponseTabLabel}>{presentation.label}</span>
                      {selected && (
                        <span className={styles.mobileResponseTabDot} aria-hidden="true" />
                      )}
                    </button>
                  );
                })}
              </div>
            </>
          )}
          <div
            className={`${styles.compareGrid} ${styles.compareGridTranscript} ${compareGridClass}`}
          >
            {turn.responses.map((response, index) => {
              const presentation = getModelPresentation(response.provider, response.model);
              return (
                <div
                  key={`${turn.id}-${index}-${response.request_id}`}
                  id={`${turn.id}-response-panel-${index}`}
                  className={`${styles.compareResponsePanel} ${index === activeResponseIndex ? styles.mobileResponsePanelActive : ""}`}
                  role={hasResponseTabs ? "tabpanel" : "region"}
                  aria-labelledby={hasResponseTabs ? `${turn.id}-response-tab-${index}` : undefined}
                  aria-label={hasResponseTabs ? undefined : `${presentation.label} response`}
                  data-response-panel
                >
                  <ResponseCard
                    response={response}
                    slotIndex={responseStartIndex + index}
                    compact
                    loadingMode="compare"
                    isStreaming={isResponseLoading(turn, response)}
                    researchEnabled={turn.researchEnabled}
                    optimizeEnabled={turn.optimizeEnabled ?? !!turn.optimization}
                    regenerationEstimate={responseEstimates[response.request_id] ?? null}
                    onRegenerate={() => void onRegenerate(turn.id, index)}
                    onRetryWithMoreRoom={() => void onRetryWithMoreRoom(turn.id, index)}
                  />
                </div>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}

function nextResponseTabIndex(key: string, index: number, count: number): number | null {
  if (count < 1) return null;
  if (key === "ArrowRight") return (index + 1) % count;
  if (key === "ArrowLeft") return (index + count - 1) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

function responseSwitcherTone(providerRaw: string, fallbackColor: string) {
  const provider = providerRaw.trim().toLowerCase();
  if (provider === "claude") return { accent: "#e07a4d", soft: "#fbeee6" };
  if (provider === "openai") return { accent: "#5b5bd6", soft: "#eef0fb" };
  return { accent: fallbackColor || "#5b5bd6", soft: "#eef0f2" };
}

function isResponseLoading(turn: ChatTurn, response: ChatTurn["responses"][number]) {
  if (response.error || ["complete", "failed", "cancelled"].includes(response.ui_status ?? ""))
    return false;
  return turn.status === "optimizing" || turn.status === "streaming";
}
