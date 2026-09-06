import {
  Children,
  cloneElement,
  isValidElement,
  memo,
  useEffect,
  useRef,
  useState,
} from "react";
import type {
  ComponentPropsWithoutRef,
  CSSProperties,
  HTMLAttributes,
  ReactElement,
  ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getModelPresentation } from "../../config/modelPresentation";
import { remarkCitations } from "../../markdown/remarkCitations";
import type { ChatResponse, ResponseRunStatus } from "../../types";
import { formatAiCredits } from "../../utils/aiCredits";
import { CortexIcon } from "../shared/CortexIcon";
import { ProviderLogo } from "../shared/ProviderLogo";
import { Citation } from "./Citation";
import {
  ResponseLoadingState,
  type ResponseLoadingMode,
} from "./ResponseLoadingState";
import { SuggestedFollowUps } from "./SuggestedFollowUps";
import styles from "./ResponseCard.module.css";

interface ResponseCardProps {
  response: ChatResponse;
  isStreaming?: boolean;
  slotIndex?: number;
  compact?: boolean;
  loadingMode?: ResponseLoadingMode;
  researchEnabled?: boolean;
  optimizeEnabled?: boolean;
  suggestedFollowUps?: string[];
  onSuggestedFollowUp?: (suggestion: string) => void | Promise<void>;
  onRegenerate?: () => void;
  onRetryWithMoreRoom?: () => void;
  compareHighlights?: {
    fastest?: boolean;
  };
}

export function ResponseCard({
  response,
  isStreaming,
  slotIndex = 0,
  compact = false,
  loadingMode = "ask",
  researchEnabled = false,
  optimizeEnabled = false,
  suggestedFollowUps = [],
  onSuggestedFollowUp,
  onRegenerate,
  onRetryWithMoreRoom,
  compareHighlights,
}: ResponseCardProps) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  const hasError = !!response.error;
  const softError = response.error?.details?.kind === "transient_capacity";
  const badge = getModelBadge(response.provider, response.model);
  const modelPresentation = getModelPresentation(response.provider, response.model);
  const accent = resolveProviderAccent(response.provider, slotIndex);
  const loadingStatus = resolveLoadingStatus(response, !!isStreaming, hasError);
  const responseText = hasError
    ? errorMessage(response)
    : response.text || (loadingStatus ? "" : "(empty response)");
  const elapsedMs = useElapsedMs(response.started_at, !!loadingStatus);
  const durationMs = resolveDisplayDurationMs(response);
  const failedDurationMs = resolveFailedDurationMs(response, elapsedMs);
  const isFailed = hasError || response.ui_status === "failed";
  const aiCredits = response.ai_credits ?? 0;
  const hasCredits = !loadingStatus && !isFailed && aiCredits > 0;
  const cacheSavings = Math.max(0, response.cache_savings_ai_credits ?? 0);
  const hasCompletedMetrics = durationMs !== null;
  const hasMetaContent =
    !!loadingStatus || isFailed || hasCompletedMetrics || hasCredits;
  const metaPinned = !!loadingStatus || isFailed;
  const showLoading = !!loadingStatus && !responseText;
  const showRegenerate = !!onRegenerate && !loadingStatus;
  const isIncomplete = response.completion_status === "incomplete";
  const canRetryWithMoreRoom =
    isIncomplete && response.retry_with_more_room?.available && !!onRetryWithMoreRoom;
  const showSuggestedFollowUps =
    suggestedFollowUps.length > 0 && !!onSuggestedFollowUp && !hasError && !showLoading;
  const responseId = String(response.request_id || `response-${slotIndex}`);
  const statsId = `response-stats-${responseId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
    };
  }, []);

  const copyResponse = async () => {
    const copiedToClipboard = await writeClipboardText(responseText || "");
    if (!copiedToClipboard) return;

    if (copyTimerRef.current !== null) {
      window.clearTimeout(copyTimerRef.current);
    }
    setCopied(true);
    copyTimerRef.current = window.setTimeout(() => {
      setCopied(false);
      copyTimerRef.current = null;
    }, 1800);
  };

  return (
    <article
      style={responseCardStyle(accent)}
      className={`${styles.card} ${compact ? styles.compact : ""} ${
        hasError ? styles.errorCard : ""
      } ${
        softError ? styles.softErrorCard : ""
      }`}
      data-response-index={slotIndex}
      data-response-error={hasError ? "true" : "false"}
      data-response-streaming={isStreaming ? "true" : "false"}
    >
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <div className={styles.modelHeader}>
            <span className={styles.modelTile}>
              <ProviderLogo
                provider={response.provider}
                logoUrl={modelPresentation.logoUrl}
                color={modelPresentation.color}
                size={24}
                className={styles.modelLogo}
              />
            </span>
            <div className={styles.modelIdentity} data-response-provider-summary>
              <h2>{modelPresentation.label}</h2>
              <span>{response.model || response.provider}</span>
            </div>
          </div>
          <div className={styles.headerActions}>
            <span className={`${styles.badge} ${styles[badge.tone]}`}>{badge.label}</span>
          </div>
        </div>
        {hasMetaContent && (
          <div
            id={statsId}
            className={`${styles.metaRow} ${metaPinned ? styles.metaRowPinned : ""} ${
              loadingStatus ? styles.loadingMetaRow : ""
            } ${
              isFailed ? styles.failedMetaRow : ""
            }`}
          >
            {loadingStatus ? (
              <span className={`${styles.metricPill} ${styles.loadingMeta}`}>
                <CortexIcon name="latency" />
                {formatElapsedClock(elapsedMs)} elapsed · {loadingStatusText(loadingStatus)}
              </span>
            ) : isFailed ? (
              <span className={`${styles.metricPill} ${styles.failedMeta}`}>
                Failed after {formatDurationSeconds(failedDurationMs)}
              </span>
            ) : (
              <>
                {durationMs !== null && (
                  <span
                    className={`${styles.metricPill} ${styles.metricText} ${
                      compareHighlights?.fastest ? styles.metricHighlight : ""
                    }`}
                  >
                    <CortexIcon name="latency" />
                    {formatMetricDurationSeconds(durationMs)}
                    {compareHighlights?.fastest && (
                      <span className={`${styles.winnerLabel} winner-label`}>
                        {" "}
                        &middot; Fastest
                      </span>
                    )}
                  </span>
                )}
                {hasCredits && (
                  <span className={`${styles.metricPill} ${styles.metricText}`}>
                    <CortexIcon name="cost" />
                    {formatAiCredits(aiCredits)} credits
                    {response.credit_usage_estimated ? " estimated" : ""}
                  </span>
                )}
              </>
            )}
          </div>
        )}
      </header>

      <div
        id={`response-text-${slotIndex}`}
        className={`${styles.body} ${isStreaming ? styles.streaming : ""}`}
      >
        {hasError ? (
          <div className={styles.errorMsg}>
            <span aria-hidden="true">!</span>
            {errorMessage(response)}
          </div>
        ) : showLoading ? (
          <ResponseLoadingState
            mode={loadingMode}
            researchEnabled={researchEnabled}
            optimizeEnabled={optimizeEnabled}
          />
        ) : (
          <ResponseMarkdown text={responseText} sources={response.web_source_items} />
        )}
        {cacheSavings > 0 && !hasError && !showLoading && (
          <div className={styles.cacheSavings} role="status">
            Saved ~{formatAiCredits(cacheSavings)} credits through context reuse
          </div>
        )}
        {isStreaming && !!responseText && <span className={styles.cursor} aria-hidden="true" />}
      </div>

      {isIncomplete && (
        <div className={styles.incompleteNotice} role="status">
          <div>
            <strong>Response stopped at its token limit.</strong>
            <span>The partial answer was preserved.</span>
          </div>
          {canRetryWithMoreRoom && (
            <button
              type="button"
              onClick={onRetryWithMoreRoom}
              title="Starts a new model call and uses additional AI credits"
            >
              Retry with more room
            </button>
          )}
        </div>
      )}

      {showSuggestedFollowUps && (
        <SuggestedFollowUps
          suggestions={suggestedFollowUps}
          onSelect={onSuggestedFollowUp}
          disabled={!!isStreaming}
        />
      )}

      <footer className={styles.actions}>
        <div className={styles.actionGroup}>
          <span className={styles.copyActionWrap}>
            <button
              type="button"
              className={`${styles.actionButton} ${copied ? styles.actionButtonCopied : ""}`}
              aria-label={copied ? "Copied response" : "Copy response"}
              title={copied ? "Copied" : "Copy response"}
              onClick={() => void copyResponse()}
            >
              <CortexIcon name="copy" />
              <span>{copied ? "Copied" : "Copy"}</span>
            </button>
            {copied && (
              <span className={styles.copyStatus} role="status" aria-live="polite">
                Copied
              </span>
            )}
          </span>
          {showRegenerate && (
            <button
              type="button"
              className={styles.actionButton}
              aria-label="Regenerate response"
              aria-description="This creates a new model call and uses AI credits."
              title="Regenerate response · New model credits will be used"
              onClick={onRegenerate}
            >
              <CortexIcon name="regenerate" />
              <span>Regenerate</span>
            </button>
          )}
        </div>
        <div className={styles.rateGroup}>
          <button
            type="button"
            className={`${styles.iconOnly} ${feedback === "up" ? styles.selectedAction : ""}`}
            aria-label="Helpful response"
            title="Helpful response"
            onClick={() => setFeedback(feedback === "up" ? null : "up")}
          >
            <CortexIcon name="thumb-up" />
          </button>
          <button
            type="button"
            className={`${styles.iconOnly} ${feedback === "down" ? styles.selectedAction : ""}`}
            aria-label="Not helpful response"
            title="Not helpful response"
            onClick={() => setFeedback(feedback === "down" ? null : "down")}
          >
            <CortexIcon name="thumb-down" />
          </button>
        </div>
      </footer>
    </article>
  );
}

const MARKDOWN_PLUGINS = [remarkGfm, remarkCitations];

// Markdown parsing is the dominant render cost of a response. Memoized so a
// card only re-parses when its own text changes, not on every store update.
const ResponseMarkdown = memo(function ResponseMarkdown({
  text,
  sources,
}: {
  text: string;
  sources: ChatResponse["web_source_items"];
}) {
  return (
    <ReactMarkdown
      remarkPlugins={MARKDOWN_PLUGINS}
      components={{
        cite: (props) => (
          <Citation
            refs={citationRefsFromProps(props as CitationMarkdownProps)}
            sources={sources}
          />
        ),
        a: ({ href, children, ...props }) => {
          return (
            <a href={href} target={isExternal(href) ? "_blank" : undefined} rel="noreferrer" {...props}>
              {children}
            </a>
          );
        },
        code: CodeBlock,
        table: MarkdownTable,
      }}
    >
      {text}
    </ReactMarkdown>
  );
});

function MarkdownTable({
  children,
  ...props
}: ComponentPropsWithoutRef<"table">) {
  const headerLabels = tableHeaderLabels(children);
  const labelledChildren = labelTableCells(children, headerLabels);

  return (
    <div
      className={styles.tableWrap}
      role="region"
      aria-label="Response table"
      tabIndex={0}
    >
      <table {...props}>{labelledChildren}</table>
    </div>
  );
}

type MarkdownElement = ReactElement<{
  children?: ReactNode;
  "data-label"?: string;
}>;

function isMarkdownElement(
  value: ReactNode,
  type: "thead" | "tbody" | "tr" | "th" | "td",
): value is MarkdownElement {
  return isValidElement<{ children?: ReactNode }>(value) && value.type === type;
}

function tableHeaderLabels(children: ReactNode): string[] {
  const head = Children.toArray(children).find((child) =>
    isMarkdownElement(child, "thead"),
  );
  if (!head || !isMarkdownElement(head, "thead")) return [];

  const row = Children.toArray(head.props.children).find((child) =>
    isMarkdownElement(child, "tr"),
  );
  if (!row || !isMarkdownElement(row, "tr")) return [];

  return Children.toArray(row.props.children)
    .filter((cell): cell is MarkdownElement => isMarkdownElement(cell, "th"))
    .map((cell) => textContent(cell.props.children));
}

function labelTableCells(children: ReactNode, labels: string[]): ReactNode {
  return Children.map(children, (child) => {
    if (!isMarkdownElement(child, "tbody")) return child;

    return cloneElement(
      child,
      {},
      Children.map(child.props.children, (row) => {
        if (!isMarkdownElement(row, "tr")) return row;
        let cellIndex = 0;

        return cloneElement(
          row,
          {},
          Children.map(row.props.children, (cell) => {
            if (!isMarkdownElement(cell, "td")) return cell;
            const label = labels[cellIndex] || `Column ${cellIndex + 1}`;
            cellIndex += 1;
            return cloneElement(cell, { "data-label": label });
          }),
        );
      }),
    );
  });
}

function textContent(value: ReactNode): string {
  return Children.toArray(value)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      return isValidElement<{ children?: ReactNode }>(child)
        ? textContent(child.props.children)
        : "";
    })
    .join("")
    .trim();
}

function CodeBlock({
  inline,
  className,
  children,
  ...props
}: HTMLAttributes<HTMLElement> & {
  inline?: boolean;
  className?: string;
  children?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const code = String(children ?? "").replace(/\n$/, "");
  const language = /language-(\w+)/.exec(className ?? "")?.[1];

  if (inline || !className) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  }

  const copy = async () => {
    await navigator.clipboard?.writeText(code).catch(() => undefined);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };

  return (
    <div className={styles.codeBlock}>
      <div className={styles.codeHeader}>
        <span>{language ?? "code"}</span>
        <button type="button" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre>
        <code className={className} {...props}>
          {children}
        </code>
      </pre>
    </div>
  );
}

async function writeClipboardText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall back to the legacy copy path below.
  }

  if (typeof document === "undefined") return false;

  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.top = "-9999px";
  document.body.appendChild(textArea);
  textArea.select();

  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(textArea);
  }
}

function getModelBadge(provider: string, model: string) {
  const value = `${provider} ${model}`.toLowerCase();
  if (value.includes("smart")) return { label: "SMART · MODEL", tone: "advanced" as const };
  if (value.includes("gemini")) return { label: "FASTEST", tone: "fastest" as const };
  if (value.includes("grok") || value.includes("xai")) return { label: "RAW", tone: "raw" as const };
  if (value.includes("claude") || value.includes("anthropic")) {
    return { label: "ADVANCED", tone: "advanced" as const };
  }
  if (value.includes("deepseek")) return { label: "DEEP", tone: "deep" as const };
  return { label: "MODEL", tone: "legacy" as const };
}

interface ProviderAccent {
  color: string;
  soft: string;
  rail: string;
}

type ResponseCardStyle = CSSProperties & {
  "--response-provider-color": string;
  "--response-provider-soft": string;
  "--response-provider-rail": string;
};

const PROVIDER_ACCENTS: Record<string, ProviderAccent> = {
  smart: {
    color: "var(--cx-accent)",
    soft: "var(--cx-accent-soft)",
    rail: "linear-gradient(90deg, var(--cx-accent), #8B8BF0)",
  },
  openai: {
    color: "var(--cx-prov-a)",
    soft: "var(--cx-prov-a-soft)",
    rail: "var(--cx-prov-a)",
  },
  claude: {
    color: "var(--cx-prov-b)",
    soft: "var(--cx-prov-b-soft)",
    rail: "var(--cx-prov-b)",
  },
  anthropic: {
    color: "var(--cx-prov-b)",
    soft: "var(--cx-prov-b-soft)",
    rail: "var(--cx-prov-b)",
  },
  deepseek: {
    color: "var(--cx-prov-c)",
    soft: "var(--cx-prov-c-soft)",
    rail: "var(--cx-prov-c)",
  },
  gemini: {
    color: "var(--cx-prov-d)",
    soft: "var(--cx-prov-d-soft)",
    rail: "var(--cx-prov-d)",
  },
  grok: {
    color: "var(--cx-prov-graphite)",
    soft: "var(--cx-prov-graphite-soft)",
    rail: "var(--cx-prov-graphite)",
  },
  xai: {
    color: "var(--cx-prov-graphite)",
    soft: "var(--cx-prov-graphite-soft)",
    rail: "var(--cx-prov-graphite)",
  },
};

const SLOT_ACCENTS: ProviderAccent[] = [
  PROVIDER_ACCENTS.openai,
  PROVIDER_ACCENTS.claude,
  PROVIDER_ACCENTS.deepseek,
  PROVIDER_ACCENTS.gemini,
  PROVIDER_ACCENTS.grok,
];

function resolveProviderAccent(provider: string, slotIndex: number): ProviderAccent {
  const normalized = provider.trim().toLowerCase();
  return PROVIDER_ACCENTS[normalized] ?? SLOT_ACCENTS[slotIndex % SLOT_ACCENTS.length]!;
}

function responseCardStyle(accent: ProviderAccent): ResponseCardStyle {
  return {
    "--response-provider-color": accent.color,
    "--response-provider-soft": accent.soft,
    "--response-provider-rail": accent.rail,
  };
}

const LOADING_STATUSES = new Set<ResponseRunStatus>([
  "queued",
  "optimizing",
  "requesting",
  "streaming",
  "finalizing",
]);

function resolveLoadingStatus(
  response: ChatResponse,
  streaming: boolean,
  hasError: boolean,
): ResponseRunStatus | null {
  if (hasError || response.ui_status === "failed" || response.ui_status === "complete") {
    return null;
  }
  if (!streaming) return null;
  if (response.ui_status && LOADING_STATUSES.has(response.ui_status)) {
    return response.ui_status;
  }
  return "streaming";
}

function loadingStatusText(status: ResponseRunStatus) {
  switch (status) {
    case "queued":
      return "Queued";
    case "optimizing":
      return "Refining prompt";
    case "requesting":
      return "Connecting to model";
    case "streaming":
      return "Generating response";
    case "finalizing":
      return "Finalizing";
    default:
      return "Generating response";
  }
}

function useElapsedMs(startedAt: string | undefined, enabled: boolean) {
  const fallbackStartedAt = useRef(Date.now());
  const [nowMs, setNowMs] = useState(() => Date.now());
  const startedAtMs = parseTimestamp(startedAt) ?? fallbackStartedAt.current;

  useEffect(() => {
    if (!enabled) return undefined;
    setNowMs(Date.now());
    const intervalId = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(intervalId);
  }, [enabled, startedAtMs]);

  return Math.max(0, nowMs - startedAtMs);
}

function resolveFailedDurationMs(response: ChatResponse, elapsedMs: number) {
  const startedAtMs = parseTimestamp(response.started_at);
  const failedAtMs = parseTimestamp(response.failed_at);
  if (startedAtMs !== null && failedAtMs !== null) {
    return Math.max(0, failedAtMs - startedAtMs);
  }
  const latencyMs = validDurationMs(response.latency_ms);
  if (latencyMs !== null) return latencyMs;
  return elapsedMs;
}

function resolveDisplayDurationMs(response: ChatResponse): number | null {
  const startedAtMs = parseTimestamp(response.started_at);
  const completedAtMs = parseTimestamp(response.completed_at);
  if (startedAtMs !== null && completedAtMs !== null) {
    return Math.max(0, completedAtMs - startedAtMs);
  }
  return validDurationMs(response.latency_ms);
}

function validDurationMs(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : null;
}

function parseTimestamp(value: string | undefined): number | null {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMetricDurationSeconds(durationMs: number) {
  return `${(Math.max(0, durationMs) / 1000).toFixed(1)}s`;
}

function formatDurationSeconds(durationMs: number) {
  return `${(Math.max(0, durationMs) / 1000).toFixed(1)} sec`;
}

function formatElapsedClock(durationMs: number) {
  const totalSeconds = Math.floor(Math.max(0, durationMs) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const paddedMinutes = String(minutes).padStart(2, "0");
  const paddedSeconds = String(seconds).padStart(2, "0");
  return hours > 0
    ? `${hours}:${paddedMinutes}:${paddedSeconds}`
    : `${paddedMinutes}:${paddedSeconds}`;
}

function errorMessage(response: ChatResponse): string {
  if (response.error?.details?.kind === "transient_capacity") {
    return "This model is temporarily busy. Try again shortly or switch to another model.";
  }
  return response.error?.message || response.text || "The model returned an error.";
}

type CitationMarkdownProps = HTMLAttributes<HTMLElement> & {
  "data-refs"?: string;
  dataRefs?: string;
};

function citationRefsFromProps(props: CitationMarkdownProps): string {
  return props["data-refs"] ?? props.dataRefs ?? "";
}

function isExternal(href: string | undefined): boolean {
  return !!href && /^https?:\/\//i.test(href);
}
