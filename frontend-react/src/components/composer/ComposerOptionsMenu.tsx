import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { ModelCatalogItem, ReasoningLevel } from "../../types";
import { getModelPresentation } from "../../config/modelPresentation";
import { ProviderLogo } from "../shared/ProviderLogo";
import { PlanBadge } from "../subscription/PlanBadge";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./ComposerOptionsMenu.module.css";

const PHONE_OPTIONS_MEDIA_QUERY = "(max-width: 760px)";

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
  interactionBlocked?: boolean;
  models?: ModelCatalogItem[];
  smartOffModel?: string;
  onSmartOffModelChange?: (id: string) => void;
  currentModel?: string;
  lockedKeys?: string[];
  lockedLabels?: Record<string, string>;
  onLockedSelect?: (id: string) => void;
  smartForNewChats?: boolean;
  onSmartForNewChatsChange?: (enabled: boolean) => void;
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
  interactionBlocked = false,
  models,
  smartOffModel,
  onSmartOffModelChange,
  currentModel,
  lockedKeys = [],
  lockedLabels = {},
  onLockedSelect,
  smartForNewChats,
  onSmartForNewChatsChange,
}: ComposerOptionsMenuProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [phoneLayout, setPhoneLayout] = useState(false);
  const [position, setPosition] = useState({
    left: 12,
    top: 12,
    width: 400,
    maxHeight: window.innerHeight - 24,
  });
  const [dragOffset, setDragOffset] = useState(0);
  const [view, setView] = useState("main");
  const [saved, setSaved] = useState(0);
  const pendingFocusRef = useRef<string | null>(null);
  const lastProviderRef = useRef<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const radioRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const dragStartYRef = useRef<number | null>(null);
  const dragOffsetRef = useRef(0);
  const open = controlledOpen ?? internalOpen;
  const groups = useMemo(() => {
    const byProvider = new Map<string, ModelCatalogItem[]>();
    for (const model of models ?? []) {
      const group = byProvider.get(model.provider) ?? [];
      group.push(model);
      byProvider.set(model.provider, group);
    }
    return Array.from(byProvider, ([provider, items]) => ({
      provider,
      items,
      meta: getModelPresentation(provider, items[0].model),
    }));
  }, [models]);
  const offModel = models?.find((model) => modelKey(model) === smartOffModel);
  const current = models?.find((model) => modelKey(model) === currentModel);
  const activeGroup = groups.find((group) => group.provider === view);
  const offMeta = getModelPresentation(
    offModel?.provider ?? smartOffModel?.split(":")[0] ?? "openai",
    offModel?.model ?? smartOffModel?.split(":")[1] ?? "gpt-5.6-luna",
  );
  const offLabel = offModel?.display_name || offMeta.label;
  const showSmartOff = Boolean(models && smartOffModel && onSmartOffModelChange);
  const showSmartSwitch =
    typeof smartForNewChats === "boolean" && Boolean(onSmartForNewChatsChange);
  const navigate = useCallback((next: string, focusTarget = "back") => {
    pendingFocusRef.current = focusTarget;
    setView(next);
  }, []);
  const stepBack = useCallback(() => {
    if (view === "providers") navigate("main", "smart-off");
    else navigate("providers", lastProviderRef.current ?? "back");
  }, [navigate, view]);

  useEffect(() => {
    if (!open) {
      setView("main");
      setSaved(0);
      pendingFocusRef.current = null;
    }
  }, [open]);
  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(0), 1800);
    return () => window.clearTimeout(timer);
  }, [saved]);
  useLayoutEffect(() => {
    if (!open || !pendingFocusRef.current) return;
    const target = pendingFocusRef.current;
    Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("[data-focus-target]") ?? [])
      .find((element) => element.dataset.focusTarget === target)
      ?.focus();
    pendingFocusRef.current = null;
  }, [open, view]);

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
    setView("main");
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
    const width = Math.min(400, window.innerWidth - 24);
    const height = dialogRef.current
      ? Math.max(dialogRef.current.offsetHeight, dialogRef.current.scrollHeight + 2)
      : 458;
    const left = Math.min(Math.max(12, rect.left), Math.max(12, window.innerWidth - width - 12));
    const above = Math.max(0, rect.top - 22);
    const below = Math.max(0, window.innerHeight - rect.bottom - 22);
    const placeAbove = height <= above || above >= below;
    const maxHeight = placeAbove ? above : below;
    const top = placeAbove ? rect.top - Math.min(height, maxHeight) - 10 : rect.bottom + 10;
    setPosition((previous) =>
      previous.left === left &&
      previous.top === top &&
      previous.width === width &&
      previous.maxHeight === maxHeight
        ? previous
        : { left, top, width, maxHeight },
    );
  }, [anchorRef, phoneLayout]);

  useLayoutEffect(() => {
    if (!open || phoneLayout) return;
    updatePosition();
    const frame = window.requestAnimationFrame(updatePosition);
    const observer =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(updatePosition) : null;
    if (dialogRef.current) observer?.observe(dialogRef.current);
    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [open, view, optimizeMode, reasoningValue, phoneLayout, updatePosition]);

  useEffect(() => {
    if (!open || interactionBlocked) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !dialogRef.current?.contains(target)) {
        closeAndFocusTrigger();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (view !== "main") stepBack();
        else closeAndFocusTrigger();
        return;
      }
      if (event.key !== "Tab" || !phoneLayout || !dialogRef.current) return;

      const focusable = getFocusable(dialogRef.current);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (
        event.shiftKey &&
        (document.activeElement === first || !dialogRef.current.contains(document.activeElement))
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last || !dialogRef.current.contains(document.activeElement))
      ) {
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
      if (phoneLayout && !dialogRef.current?.contains(document.activeElement))
        getFocusable(dialogRef.current)[0]?.focus();
    });

    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [closeAndFocusTrigger, interactionBlocked, open, phoneLayout, stepBack, updatePosition, view]);

  const reasoningLabel = reasoningUnavailable ? "Unavailable" : optionLabel(reasoningValue);
  const hasConfiguredOption = optimizeMode || (!reasoningUnavailable && reasoningValue !== "auto");
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

  const chooseModel = (model: ModelCatalogItem) => {
    const key = modelKey(model);
    if (lockedKeys.includes(key)) {
      onLockedSelect?.(key);
      return;
    }
    onSmartOffModelChange?.(key);
    setSaved((previous) => previous + 1);
    navigate("main", "smart-off");
  };
  const modelRow = (model: ModelCatalogItem) => {
    const key = modelKey(model);
    const meta = getModelPresentation(model.provider, model.model);
    const selected = key === smartOffModel;
    const locked = lockedKeys.includes(key);
    return (
      <button
        key={key}
        type="button"
        role="option"
        aria-selected={selected}
        data-locked={locked || undefined}
        className={`${styles.pickRow} ${selected ? styles.selectedOption : ""}`}
        onClick={() => chooseModel(model)}
      >
        <ProviderLogo
          provider={model.provider}
          logoUrl={meta.logoUrl}
          color={meta.color}
          size={20}
        />
        <span className={styles.rowCopy}>
          <strong>{model.display_name || meta.label}</strong>
          <small>
            {model.credit_usage_label ? `${model.credit_usage_label} credit use` : model.model}
          </small>
        </span>
        {locked ? (
          <PlanBadge tone="locked" label={lockedLabels[key] || "Upgrade"} />
        ) : selected ? (
          <span className={styles.checkSpace} aria-hidden="true">
            <CortexIcon name="composer-check" strokeWidth={2.6} />
          </span>
        ) : null}
      </button>
    );
  };
  const listKeyboard = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="option"]'),
    );
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : (index + (event.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
    event.preventDefault();
    items[next]?.focus();
  };
  const backButton = (
    <button
      type="button"
      data-focus-target="back"
      className={styles.backButton}
      aria-label={view === "providers" ? "Back to More options" : "Back to Providers"}
      onClick={stepBack}
    >
      <CortexIcon name="chevron-left" />
      <span>{view === "providers" ? "More options" : "Providers"}</span>
    </button>
  );
  const doneButton = (
    <button type="button" className={styles.doneButton} onClick={closeAndFocusTrigger}>
      Done
    </button>
  );
  const smartRows = (
    <>
      {showSmartSwitch ? (
        <button
          type="button"
          role="switch"
          aria-checked={smartForNewChats}
          className={styles.improveRow}
          onClick={() => onSmartForNewChatsChange?.(!smartForNewChats)}
        >
          <span className={styles.rowIcon} aria-hidden="true">
            <CortexIcon name="composer-smart" />
          </span>
          <span className={styles.rowCopy}>
            <strong>Smart in new chats</strong>
            <small>New chats start with Smart on</small>
          </span>
          <span className={styles.switch} aria-hidden="true">
            <span />
          </span>
        </button>
      ) : null}
      {showSmartOff ? (
        <button
          type="button"
          aria-haspopup="listbox"
          data-focus-target="smart-off"
          className={styles.improveRow}
          onClick={() => navigate("providers")}
        >
          <span className={styles.modelTile} aria-hidden="true">
            <ProviderLogo
              provider={offModel?.provider ?? "openai"}
              logoUrl={offMeta.logoUrl}
              color={offMeta.color}
              size={20}
            />
          </span>
          <span className={styles.rowCopy}>
            <strong>When Smart is off</strong>
            <small>
              Start on <b>{offLabel}</b>
            </small>
          </span>
          {saved ? (
            <span className={styles.saved} key={saved} role="status">
              <CortexIcon name="composer-check" strokeWidth={2.6} />
              Saved
            </span>
          ) : (
            <CortexIcon className={styles.chevron} name="chevron-right" />
          )}
        </button>
      ) : null}
    </>
  );
  const drillTitle = view === "providers" ? "When Smart is off" : activeGroup?.meta.providerLabel;
  const drillBody =
    view === "providers"
      ? "Cortex starts on this model whenever you turn Smart off, and puts it first in Compare."
      : "Pick the model to start on when Smart is off.";
  const drillView = (
    <>
      {phoneLayout ? (
        <div className={`${styles.menuHeader} ${styles.drillHeader}`}>
          {backButton}
          <strong>{drillTitle}</strong>
          {doneButton}
        </div>
      ) : (
        <>
          {backButton}
          <div className={styles.subHeader}>
            <strong>{drillTitle}</strong>
            <span>{drillBody}</span>
          </div>
        </>
      )}
      {phoneLayout ? <p className={styles.lead}>{drillBody}</p> : null}
      {view === "providers" ? (
        <>
          {current && currentModel !== smartOffModel ? (
            <>
              <div className={styles.sectionLabel}>In use now</div>
              <div
                className={phoneLayout ? styles.card : ""}
                role="listbox"
                aria-label="In use now"
                onKeyDown={listKeyboard}
              >
                {modelRow(current)}
              </div>
            </>
          ) : null}
          <div className={styles.sectionLabel}>Choose a provider</div>
          <div
            className={phoneLayout ? styles.card : ""}
            role="listbox"
            aria-label="Providers"
            onKeyDown={listKeyboard}
          >
            {groups.map((group) => {
              const selected = offModel?.provider === group.provider;
              return (
                <button
                  key={group.provider}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  data-focus-target={group.provider}
                  className={styles.pickRow}
                  onClick={() => {
                    lastProviderRef.current = group.provider;
                    navigate(group.provider);
                  }}
                >
                  <ProviderLogo
                    provider={group.provider}
                    logoUrl={group.meta.logoUrl}
                    color={group.meta.color}
                    size={20}
                  />
                  <span className={styles.rowCopy}>
                    <strong>{group.meta.providerLabel}</strong>
                    <small>
                      {group.items.length} {group.items.length === 1 ? "model" : "models"}
                      {selected ? ` · ${offLabel}` : ""}
                    </small>
                  </span>
                  <span className={styles.providerActions} aria-hidden="true">
                    {selected ? <CortexIcon name="composer-check" strokeWidth={2.6} /> : null}
                    <CortexIcon name="chevron-right" />
                  </span>
                </button>
              );
            })}
          </div>
        </>
      ) : (
        <div
          className={phoneLayout ? styles.card : ""}
          role="listbox"
          aria-label={`${drillTitle} models`}
          onKeyDown={listKeyboard}
        >
          {activeGroup?.items.map(modelRow)}
        </div>
      )}
    </>
  );

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

            {view === "main" ? (
              <>
                <div className={styles.menuHeader}>
                  <span>More options</span>
                  {modeLabel ? <span className={styles.modeLabel}>{modeLabel}</span> : null}
                  {phoneLayout ? doneButton : null}
                </div>

                {showSmartSwitch || showSmartOff ? (
                  <>
                    {phoneLayout ? <div className={styles.sectionLabel}>Smart</div> : null}
                    <div className={phoneLayout ? styles.card : ""}>{smartRows}</div>
                    {!phoneLayout ? <div className={styles.divider} /> : null}
                  </>
                ) : null}
                {phoneLayout ? <div className={styles.sectionLabel}>This message</div> : null}
                <div className={phoneLayout ? styles.card : ""}>
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

                  <div className={phoneLayout ? styles.reasoningBlock : ""}>
                    <div className={styles.reasoningHeader}>
                      <span className={styles.rowIcon} aria-hidden="true">
                        <CortexIcon name="composer-smart" />
                      </span>
                      <span className={styles.rowCopy}>
                        <strong>Reasoning</strong>
                        <small>
                          {phoneLayout
                            ? REASONING_OPTIONS.find((option) => option.value === reasoningValue)
                                ?.description
                            : reasoningDescription}
                        </small>
                      </span>
                    </div>

                    <div className={styles.reasoningList} role="radiogroup" aria-label="Reasoning">
                      {REASONING_OPTIONS.map((option, index) => {
                        const disabled =
                          option.value !== "auto" &&
                          !supportedReasoningLevels.includes(option.value);
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
                            aria-label={
                              phoneLayout ? option.label : `${option.label} — ${option.description}`
                            }
                            tabIndex={selected ? 0 : -1}
                            disabled={disabled}
                            className={`${styles.reasoningOption} ${
                              selected ? styles.selectedOption : ""
                            }`}
                            onClick={() => onReasoningChange(option.value)}
                            onKeyDown={(event) => selectReasoningByKeyboard(event, index)}
                          >
                            <span className={styles.optionCopy}>
                              <strong>
                                {phoneLayout && option.value === "medium"
                                  ? "Med"
                                  : phoneLayout && option.value === "max"
                                    ? "Max"
                                    : option.label}
                              </strong>
                              {!phoneLayout ? (
                                <small>
                                  {disabled
                                    ? "Not supported by the current model selection"
                                    : option.description}
                                </small>
                              ) : null}
                            </span>
                            {!phoneLayout ? (
                              <span className={styles.checkSpace} aria-hidden="true">
                                {selected ? (
                                  <CortexIcon name="composer-check" strokeWidth={2.6} />
                                ) : null}
                              </span>
                            ) : null}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </>
            ) : (
              drillView
            )}

            {view === "main" || phoneLayout ? (
              <div className={styles.note}>
                {phoneLayout ? `${reasoningSubtitle(reasoningContextLabel, false)}. ` : ""}
                Higher reasoning may take longer and use more AI credits.
              </div>
            ) : null}
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
            <CortexIcon className={styles.chipIcon} name="composer-improve" strokeWidth={2} />
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
  ).filter((element) => element.getAttribute("aria-hidden") !== "true" && element.tabIndex >= 0);
}

function optionLabel(level: ReasoningLevel): string {
  if (level === "max") return "Maximum";
  return `${level.charAt(0).toUpperCase()}${level.slice(1)}`;
}

function modelKey(model: ModelCatalogItem): string {
  return `${model.provider}:${model.model}`;
}

function reasoningSubtitle(contextLabel: string, unavailable: boolean): string {
  if (unavailable) return "Unavailable for the current model selection";
  if (contextLabel === "Smart Ask") return "Applied to Smart routing";
  if (contextLabel === "Selected models") return "Shared across selected models";
  return `Applied to ${contextLabel}`;
}
