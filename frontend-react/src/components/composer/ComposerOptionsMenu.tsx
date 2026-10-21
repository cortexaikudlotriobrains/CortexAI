import { useCallback, useRef, useState, type KeyboardEvent } from "react";
import type { ReasoningLevel } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import { ComposerPopover } from "./ComposerPopover";
import { useComposerSheet } from "./useComposerSheet";
import styles from "./ComposerOptionsMenu.module.css";

const REASONING_OPTIONS: { value: ReasoningLevel; label: string; description: string }[] = [
  { value: "auto", label: "Auto", description: "Cortex chooses for each request" },
  { value: "low", label: "Low", description: "Faster, lighter analysis" },
  { value: "medium", label: "Medium", description: "Balanced speed and depth" },
  { value: "high", label: "High", description: "Deeper analysis for difficult questions" },
  { value: "max", label: "Maximum", description: "Most reasoning available; slower" },
];

interface ComposerOptionsMenuProps {
  optimizeMode: boolean;
  onOptimizeToggle: (enabled: boolean) => void;
  optimizeBlocked?: boolean;
  onOptimizeBlocked?: () => void;
  reasoningValue: ReasoningLevel;
  supportedReasoningLevels: Exclude<ReasoningLevel, "auto">[];
  reasoningUnavailable?: boolean;
  reasoningContextLabel: string;
  onReasoningChange: (level: ReasoningLevel) => void;
  modeLabel?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  interactionBlocked?: boolean;
}

export function ComposerOptionsMenu({
  optimizeMode,
  onOptimizeToggle,
  optimizeBlocked = false,
  onOptimizeBlocked,
  reasoningValue,
  supportedReasoningLevels,
  reasoningUnavailable = false,
  reasoningContextLabel,
  onReasoningChange,
  open: controlledOpen,
  onOpenChange,
  interactionBlocked = false,
}: ComposerOptionsMenuProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const radioRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const sheet = useComposerSheet();
  const open = controlledOpen ?? internalOpen;
  const setOpen = useCallback(
    (next: boolean) => {
      if (controlledOpen === undefined) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [controlledOpen, onOpenChange],
  );
  const close = useCallback(() => setOpen(false), [setOpen]);
  const closeAndFocus = () => {
    close();
    requestAnimationFrame(() => triggerRef.current?.focus());
  };
  const description =
    reasoningContextLabel === "Smart Ask"
      ? "Applied to Smart routing"
      : reasoningContextLabel === "Selected models"
        ? "Applied to every compared model"
        : `Applied to ${reasoningContextLabel}`;
  const selected = REASONING_OPTIONS.find((option) => option.value === reasoningValue)!;
  const isDisabled = (level: ReasoningLevel) =>
    level !== "auto" && !supportedReasoningLevels.includes(level);
  const keyboard = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(event.key))
      return;
    event.preventDefault();
    const indexes = REASONING_OPTIONS.map((option, index) =>
      isDisabled(option.value) ? -1 : index,
    ).filter((index) => index >= 0);
    const current = Math.max(0, indexes.indexOf(currentIndex));
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? indexes.length - 1
          : (current + (["ArrowUp", "ArrowLeft"].includes(event.key) ? -1 : 1) + indexes.length) %
            indexes.length;
    onReasoningChange(REASONING_OPTIONS[indexes[next]].value);
    radioRefs.current[indexes[next]]?.focus();
  };
  return (
    <>
      <button
        ref={triggerRef}
        id="composerOptionsBtn"
        type="button"
        className={`${styles.trigger} ${open ? styles.open : ""}`}
        aria-label="More options"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls="composer-options-menu"
        aria-describedby="composer-options-status"
        data-improve-enabled={optimizeMode}
        data-reasoning-level={reasoningUnavailable ? "unavailable" : reasoningValue}
        data-options-configured={
          optimizeMode || (!reasoningUnavailable && reasoningValue !== "auto")
        }
        onClick={() => setOpen(!open)}
      >
        <CortexIcon name="composer-options" strokeWidth={2} />
      </button>
      <span id="composer-options-status" className={styles.screenReaderOnly}>
        Improve {optimizeMode ? "on" : "off"}; reasoning{" "}
        {reasoningUnavailable ? "Unavailable" : selected.label}
      </span>
      <ComposerPopover
        open={open}
        triggerRef={triggerRef}
        id="composer-options-menu"
        label="More options"
        width={360}
        onClose={close}
        interactionBlocked={interactionBlocked}
      >
        <div className={styles.menuHeader}>
          <span>More options</span>
          {sheet && (
            <button type="button" className={styles.doneButton} onClick={closeAndFocus}>
              Done
            </button>
          )}
        </div>
        {sheet && <div className={styles.sectionLabel}>THIS MESSAGE</div>}
        <button
          id="routeOptimizeBtn"
          type="button"
          role="switch"
          aria-checked={optimizeMode}
          aria-disabled={optimizeBlocked && !optimizeMode}
          className={styles.improveRow}
          onClick={() => {
            if (optimizeBlocked && !optimizeMode) onOptimizeBlocked?.();
            else onOptimizeToggle(!optimizeMode);
          }}
        >
          <span className={styles.rowIcon}>
            <CortexIcon name="composer-improve" />
          </span>
          <span className={styles.rowCopy}>
            <strong>Improve prompt</strong>
            <small>Rewrite your prompt before sending</small>
          </span>
          <span className={styles.switch} aria-hidden="true">
            <span />
          </span>
        </button>
        {!reasoningUnavailable && (
          <>
            <div className={styles.divider} />
            <div className={styles.reasoningHeader}>
              <span className={styles.rowIcon}>
                <CortexIcon name="composer-smart" />
              </span>
              <span className={styles.rowCopy}>
                <strong>Reasoning</strong>
                <small>{sheet ? selected.description : description}</small>
              </span>
            </div>
            <div className={styles.reasoningList} role="radiogroup" aria-label="Reasoning">
              {REASONING_OPTIONS.map((option, index) => (
                <button
                  key={option.value}
                  ref={(element) => {
                    radioRefs.current[index] = element;
                  }}
                  type="button"
                  role="radio"
                  aria-checked={option.value === reasoningValue}
                  aria-label={sheet ? option.label : `${option.label} — ${option.description}`}
                  tabIndex={option.value === reasoningValue ? 0 : -1}
                  disabled={isDisabled(option.value)}
                  className={`${styles.reasoningOption} ${option.value === reasoningValue ? styles.selectedOption : ""}`}
                  onClick={() => onReasoningChange(option.value)}
                  onKeyDown={(event) => keyboard(event, index)}
                >
                  <span className={styles.optionCopy}>
                    <strong>
                      {sheet && option.value === "medium"
                        ? "Med"
                        : sheet && option.value === "max"
                          ? "Max"
                          : option.label}
                    </strong>
                    {!sheet && <small>{option.description}</small>}
                  </span>
                  {!sheet && option.value === reasoningValue && (
                    <CortexIcon name="check" className={styles.check} />
                  )}
                </button>
              ))}
            </div>
          </>
        )}
        <div className={styles.note}>
          {sheet ? `${description}. ` : ""}Higher reasoning may take longer and use more AI credits.
        </div>
      </ComposerPopover>
    </>
  );
}

export { FeatureChips as ComposerOptionChips } from "./FeatureChips";
