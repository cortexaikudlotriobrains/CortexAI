import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { ReasoningLevel } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./ComposerOptionsMenu.module.css";

const PHONE_OPTIONS_MEDIA_QUERY = "(max-width: 767px)";

const REASONING_OPTIONS: Array<{
  value: ReasoningLevel;
  label: string;
  description: string;
}> = [
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
  modeLabel: string;
  anchorRef?: RefObject<HTMLElement | null>;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
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
  modeLabel,
  anchorRef,
  open: controlledOpen,
  onOpenChange,
}: ComposerOptionsMenuProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [phoneLayout, setPhoneLayout] = useState(false);
  const [position, setPosition] = useState({ left: 12, top: 12, width: 400 });
  const [dragOffset, setDragOffset] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const radioRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const dragStartYRef = useRef<number | null>(null);
  const dragOffsetRef = useRef(0);
  const open = controlledOpen ?? internalOpen;

  const setOpen = useCallback(
    (nextOpen: boolean) => {
      if (controlledOpen === undefined) setInternalOpen(nextOpen);
      onOpenChange?.(nextOpen);
    },
    [controlledOpen, onOpenChange],
  );

  const closeAndFocusTrigger = useCallback(() => {
    dragOffsetRef.current = 0;
    setDragOffset(0);
    setOpen(false);
    window.requestAnimationFrame(() => triggerRef.current?.focus());
  }, [setOpen]);

  useEffect(() => {
    const media = window.matchMedia?.(PHONE_OPTIONS_MEDIA_QUERY);
    if (!media) return;
    const sync = () => setPhoneLayout(media.matches);
    sync();
    media.addEventListener?.("change", sync);
    return () => media.removeEventListener?.("change", sync);
  }, []);

  const updatePosition = useCallback(() => {
    if (phoneLayout) return;
    const anchor = anchorRef?.current ?? triggerRef.current;
    if (!anchor) return;

    const rect = anchor.getBoundingClientRect();
    const preferredWidth = window.innerWidth >= 1024 ? 400 : 420;
    const width = Math.min(preferredWidth, Math.max(280, window.innerWidth - 24));
    const height = dialogRef.current?.offsetHeight ?? 458;
    const left = Math.min(
      Math.max(12, rect.left),
      Math.max(12, window.innerWidth - width - 12),
    );
    const top = Math.max(12, rect.top - height - 10);
    setPosition({ left, top, width });
  }, [anchorRef, phoneLayout]);

  useLayoutEffect(() => {
    if (!open || phoneLayout) return;
    updatePosition();
    const frame = window.requestAnimationFrame(updatePosition);
    return () => window.cancelAnimationFrame(frame);
  }, [open, optimizeMode, reasoningValue, phoneLayout, updatePosition]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !dialogRef.current?.contains(target)) {
        closeAndFocusTrigger();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAndFocusTrigger();
        return;
      }
      if (event.key !== "Tab" || !phoneLayout || !dialogRef.current) return;

      const focusable = getFocusable(dialogRef.current);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);

    const previousOverflow = document.body.style.overflow;
    if (phoneLayout) document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => {
      if (phoneLayout) getFocusable(dialogRef.current)[0]?.focus();
    });

    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [closeAndFocusTrigger, open, phoneLayout, updatePosition]);

  const reasoningLabel = reasoningUnavailable ? "Unavailable" : optionLabel(reasoningValue);
  const hasConfiguredOption =
    optimizeMode || (!reasoningUnavailable && reasoningValue !== "auto");
  const reasoningDescription = reasoningSubtitle(reasoningContextLabel, reasoningUnavailable);
  const menuStyle = phoneLayout
    ? ({ "--sheet-drag-y": `${dragOffset}px` } as CSSProperties)
    : position;

  const selectReasoningByKeyboard = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentIndex: number,
  ) => {
    const key = event.key;
    if (!["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(key)) {
      return;
    }
    event.preventDefault();

    const availableIndexes = REASONING_OPTIONS.map((option, index) =>
      option.value === "auto" || supportedReasoningLevels.includes(option.value) ? index : -1,
    ).filter((index) => index >= 0);
    if (availableIndexes.length === 0) return;

    const currentAvailableIndex = Math.max(0, availableIndexes.indexOf(currentIndex));
    const nextAvailableIndex =
      key === "Home"
        ? 0
        : key === "End"
          ? availableIndexes.length - 1
          : key === "ArrowDown" || key === "ArrowRight"
            ? (currentAvailableIndex + 1) % availableIndexes.length
            : (currentAvailableIndex - 1 + availableIndexes.length) % availableIndexes.length;
    const optionIndex = availableIndexes[nextAvailableIndex];
    onReasoningChange(REASONING_OPTIONS[optionIndex].value);
    radioRefs.current[optionIndex]?.focus();
  };

  const menu = open
    ? createPortal(
        <>
          {phoneLayout ? (
            <button
              type="button"
              className={styles.backdrop}
              aria-label="Dismiss options sheet"
              onClick={closeAndFocusTrigger}
            />
          ) : null}
          <div
            ref={dialogRef}
            id="composer-options-menu"
            className={`${styles.menu} ${phoneLayout ? styles.sheet : styles.popover}`}
            role="dialog"
            aria-label="More options"
            aria-modal={phoneLayout || undefined}
            data-layout={phoneLayout ? "sheet" : "popover"}
            style={menuStyle}
          >
            {phoneLayout ? (
              <div
                className={styles.sheetHandle}
                data-sheet-grabber
                aria-hidden="true"
                onPointerDown={(event) => {
                  event.preventDefault();
                  dragOffsetRef.current = 0;
                  dragStartYRef.current = event.clientY;
                  event.currentTarget.setPointerCapture(event.pointerId);
                }}
                onPointerMove={(event) => {
                  if (dragStartYRef.current === null) return;
                  event.preventDefault();
                  const nextOffset = Math.max(0, event.clientY - dragStartYRef.current);
                  dragOffsetRef.current = nextOffset;
                  setDragOffset(nextOffset);
                }}
                onPointerUp={(event) => {
                  if (dragStartYRef.current === null) return;
                  event.currentTarget.releasePointerCapture(event.pointerId);
                  dragStartYRef.current = null;
                  if (dragOffsetRef.current >= 80) closeAndFocusTrigger();
                  else {
                    dragOffsetRef.current = 0;
                    setDragOffset(0);
                  }
                }}
                onPointerCancel={() => {
                  dragStartYRef.current = null;
                  dragOffsetRef.current = 0;
                  setDragOffset(0);
                }}
              />
            ) : null}

            <div className={styles.menuHeader}>
              <span>More options</span>
              <span>{modeLabel}</span>
            </div>

            <button
              id="routeOptimizeBtn"
              type="button"
              role="switch"
              aria-checked={optimizeMode}
              aria-disabled={optimizeBlocked && !optimizeMode}
              className={styles.improveRow}
              onClick={() => {
                if (optimizeBlocked && !optimizeMode) {
                  onOptimizeBlocked?.();
                  return;
                }
                onOptimizeToggle(!optimizeMode);
              }}
            >
              <span className={styles.rowIcon} aria-hidden="true">
                <CortexIcon name="composer-improve" strokeWidth={2} />
              </span>
              <span className={styles.rowCopy}>
                <strong>Improve prompt</strong>
                <small>Rewrite your prompt before sending</small>
              </span>
              <span className={styles.switch} aria-hidden="true">
                <span />
              </span>
            </button>

            <div className={styles.divider} />

            <div className={styles.reasoningHeader}>
              <span className={styles.rowIcon} aria-hidden="true">
                <CortexIcon name="composer-smart" />
              </span>
              <span className={styles.rowCopy}>
                <strong>Reasoning</strong>
                <small>{reasoningDescription}</small>
              </span>
            </div>

            <div className={styles.reasoningList} role="radiogroup" aria-label="Reasoning">
              {REASONING_OPTIONS.map((option, index) => {
                const disabled =
                  option.value !== "auto" && !supportedReasoningLevels.includes(option.value);
                const selected = option.value === reasoningValue;
                return (
                  <button
                    key={option.value}
                    ref={(element) => {
                      radioRefs.current[index] = element;
                    }}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    aria-label={`${option.label} — ${option.description}`}
                    disabled={disabled}
                    className={`${styles.reasoningOption} ${
                      selected ? styles.selectedOption : ""
                    }`}
                    onClick={() => onReasoningChange(option.value)}
                    onKeyDown={(event) => selectReasoningByKeyboard(event, index)}
                  >
                    <span className={styles.optionCopy}>
                      <strong>{option.label}</strong>
                      <small>
                        {disabled
                          ? "Not supported by the current model selection"
                          : option.description}
                      </small>
                    </span>
                    <span className={styles.checkSpace} aria-hidden="true">
                      {selected ? (
                        <CortexIcon name="composer-check" strokeWidth={2.6} />
                      ) : null}
                    </span>
                  </button>
                );
              })}
            </div>

            <div className={styles.note}>
              Higher reasoning may take longer and use more AI credits.
            </div>
          </div>
        </>,
        document.body,
      )
    : null;

  return (
    <>
      <button
        ref={triggerRef}
        id="composerOptionsBtn"
        type="button"
        className={`${styles.trigger} ${open ? styles.open : ""}`}
        aria-label="More options"
        aria-describedby="composer-options-status"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls="composer-options-menu"
        data-improve-enabled={optimizeMode}
        data-reasoning-level={reasoningUnavailable ? "unavailable" : reasoningValue}
        data-options-configured={hasConfiguredOption}
        onClick={() => {
          if (open) closeAndFocusTrigger();
          else setOpen(true);
        }}
      >
        <CortexIcon name="composer-options" strokeWidth={2} />
      </button>
      <span id="composer-options-status" className={styles.screenReaderOnly}>
        Improve {optimizeMode ? "on" : "off"}; reasoning {reasoningLabel}
      </span>
      {menu}
    </>
  );
}

interface ComposerOptionChipsProps {
  optimizeMode: boolean;
  reasoningValue: ReasoningLevel;
  reasoningUnavailable?: boolean;
  onOpen: () => void;
  onOptimizeReset: () => void;
  onReasoningReset: () => void;
}

export function ComposerOptionChips({
  optimizeMode,
  reasoningValue,
  reasoningUnavailable = false,
  onOpen,
  onOptimizeReset,
  onReasoningReset,
}: ComposerOptionChipsProps) {
  const showReasoning = !reasoningUnavailable && reasoningValue !== "auto";
  if (!optimizeMode && !showReasoning) return null;

  return (
    <div className={styles.chips} data-options-chips aria-label="Active composer options">
      {optimizeMode ? (
        <div className={styles.optionChip} data-option-chip="improve">
          <button
            type="button"
            className={styles.chipBody}
            aria-label="Edit Improve prompt setting"
            onClick={onOpen}
          >
            <CortexIcon
              className={styles.chipIcon}
              name="composer-improve"
              strokeWidth={2}
            />
            <span>Improve</span>
          </button>
          <button
            type="button"
            className={styles.chipReset}
            aria-label="Turn off Improve prompt"
            onClick={onOptimizeReset}
          >
            <CortexIcon name="chip-close" strokeWidth={2.6} />
          </button>
        </div>
      ) : null}
      {showReasoning ? (
        <div className={styles.optionChip} data-option-chip="reasoning">
          <button
            type="button"
            className={styles.chipBody}
            aria-label={`Edit ${optionLabel(reasoningValue)} reasoning setting`}
            onClick={onOpen}
          >
            <CortexIcon className={styles.chipIcon} name="composer-smart" />
            <span>{optionLabel(reasoningValue)}</span>
          </button>
          <button
            type="button"
            className={styles.chipReset}
            aria-label="Reset reasoning to Auto"
            onClick={onReasoningReset}
          >
            <CortexIcon name="chip-close" strokeWidth={2.6} />
          </button>
        </div>
      ) : null}
    </div>
  );
}

function getFocusable(container: HTMLElement | null): HTMLElement[] {
  if (!container) return [];
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.getAttribute("aria-hidden") !== "true");
}

function optionLabel(level: ReasoningLevel): string {
  if (level === "max") return "Maximum";
  return `${level.charAt(0).toUpperCase()}${level.slice(1)}`;
}

function reasoningSubtitle(contextLabel: string, unavailable: boolean): string {
  if (unavailable) return "Unavailable for the current model selection";
  if (contextLabel === "Smart Ask") return "Applied to Smart routing";
  if (contextLabel === "Selected models") return "Shared across selected models";
  return `Applied to ${contextLabel}`;
}
