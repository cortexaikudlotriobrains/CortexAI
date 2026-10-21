import {
  type CSSProperties,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import type { ModelCatalogItem } from "../../types";
import { getModelPresentation } from "../../config/modelPresentation";
import { ProviderLogo } from "../shared/ProviderLogo";
import { CortexIcon } from "../shared/CortexIcon";
import { PlanBadge } from "../subscription/PlanBadge";
import styles from "./ModelPicker.module.css";
import { ComposerModelMenu, type ComposerModelMenuConfig } from "./ComposerModelMenu";

export type ModelPickerPlacement = "up" | "down";
export type ModelPickerAlignment = "left" | "center" | "right";

const MENU_GAP = 7;
const MENU_MAX_HEIGHT = 340;
const MENU_MAX_WIDTH = 380;
const CASCADE_MENU_MAX_WIDTH = 680;
const VIEWPORT_MARGIN = 20;
const HOVER_PREVIEW_CLOSE_DELAY_MS = 140;
const HOVER_PREVIEW_QUERY = "(hover: hover) and (pointer: fine) and (min-width: 761px)";
const EMPTY_MODELS: ModelCatalogItem[] = [];

interface ProviderGroup {
  key: string;
  label: string;
  logoUrl: string;
  color: string;
  models: ModelCatalogItem[];
}

interface ModelPickerProps {
  composer?: ComposerModelMenuConfig;
  id: string;
  models: ModelCatalogItem[];
  value: string;
  onChange: (key: string) => void;
  ariaLabel: string;
  listboxLabel?: string;
  selectedKeys?: string[];
  disabledKeys?: string[];
  lockedKeys?: string[];
  lockedLabels?: Record<string, string>;
  onLockedSelect?: (key: string) => void;
  align?: ModelPickerAlignment;
  placement?: ModelPickerPlacement;
  className?: string;
  selectClassName?: string;
  buttonClassName?: string;
  menuClassName?: string;
}

export function ModelPicker(props: ModelPickerProps) {
  return props.composer ? (
    <ComposerModelMenu {...props} composer={props.composer} />
  ) : (
    <CatalogModelPicker {...props} />
  );
}

function CatalogModelPicker({
  id,
  models,
  value,
  onChange,
  ariaLabel,
  listboxLabel = "Model options",
  selectedKeys = [],
  disabledKeys = [],
  lockedKeys = [],
  lockedLabels = {},
  onLockedSelect,
  align = "left",
  placement = "up",
  className,
  selectClassName,
  buttonClassName,
  menuClassName,
}: ModelPickerProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeProvider, setActiveProvider] = useState<string | null>(null);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({});
  const [cascadeDirection, setCascadeDirection] = useState<"left" | "right">("right");
  const supportsHoverPreview = useMediaQuery(HOVER_PREVIEW_QUERY);
  const rootRef = useRef<HTMLSpanElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const providerPanelRef = useRef<HTMLDivElement>(null);
  const modelPanelRef = useRef<HTMLDivElement>(null);
  const providerOptionRefs = useRef(new Map<string, HTMLButtonElement>());
  const modelOptionRefs = useRef(new Map<string, HTMLButtonElement>());
  const hoverCloseTimerRef = useRef<number | null>(null);
  const focusTargetRef = useRef<"provider" | "model" | null>(null);
  const lastActiveProviderRef = useRef<string | null>(null);
  const selectedModel = models.find((model) => modelKey(model) === value) ?? modelFromKey(value);
  const selectedMeta = getModelPresentation(selectedModel.provider, selectedModel.model);
  const selectedCreditLabel =
    "credit_usage_label" in selectedModel ? selectedModel.credit_usage_label : undefined;
  const listboxId = `${id}Options`;
  const disabledSet = new Set(disabledKeys);
  const lockedSet = new Set(lockedKeys);
  const selectedSet = new Set(selectedKeys.filter((key) => key !== value));
  const providerGroups = useMemo(() => groupModelsByProvider(models), [models]);
  const activeProviderGroup = providerGroups.find((group) => group.key === activeProvider) ?? null;
  const activeModels = activeProviderGroup?.models ?? EMPTY_MODELS;

  const cancelHoverClose = useCallback(() => {
    if (hoverCloseTimerRef.current === null) return;
    window.clearTimeout(hoverCloseTimerRef.current);
    hoverCloseTimerRef.current = null;
  }, []);

  const closeMenu = useCallback(
    (restoreFocus = false) => {
      cancelHoverClose();
      setIsOpen(false);
      setActiveProvider(null);
      focusTargetRef.current = null;
      if (restoreFocus) {
        window.requestAnimationFrame(() => buttonRef.current?.focus());
      }
    },
    [cancelHoverClose],
  );

  const showProviderModels = useCallback(
    (providerKey: string, moveFocus: boolean) => {
      cancelHoverClose();
      lastActiveProviderRef.current = providerKey;
      focusTargetRef.current = moveFocus ? "model" : null;
      setActiveProvider(providerKey);
    },
    [cancelHoverClose],
  );

  const showProviders = useCallback(
    (moveFocus: boolean) => {
      cancelHoverClose();
      focusTargetRef.current = moveFocus ? "provider" : null;
      setActiveProvider(null);
    },
    [cancelHoverClose],
  );

  const scheduleHoverPreviewClose = useCallback(() => {
    if (!supportsHoverPreview || !activeProvider) return;
    cancelHoverClose();
    hoverCloseTimerRef.current = window.setTimeout(() => {
      focusTargetRef.current = null;
      setActiveProvider(null);
      hoverCloseTimerRef.current = null;
    }, HOVER_PREVIEW_CLOSE_DELAY_MS);
  }, [activeProvider, cancelHoverClose, supportsHoverPreview]);

  const updateMenuPosition = useCallback(() => {
    const button = buttonRef.current;
    if (!button) return;

    const triggerRect = button.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const isCascadeOpen = supportsHoverPreview && Boolean(activeProvider);
    const width = Math.min(
      isCascadeOpen ? CASCADE_MENU_MAX_WIDTH : MENU_MAX_WIDTH,
      Math.max(0, viewportWidth - VIEWPORT_MARGIN * 2),
    );
    const providerHeight = providerGroups.length * 56 + 46;
    const modelHeight = activeModels.length * 52 + 58;
    const naturalHeight = Math.min(
      MENU_MAX_HEIGHT,
      isCascadeOpen
        ? Math.max(providerHeight, modelHeight)
        : activeProvider
          ? modelHeight
          : providerHeight,
    );
    const availableAbove = Math.max(0, triggerRect.top - MENU_GAP - VIEWPORT_MARGIN);
    const availableBelow = Math.max(
      0,
      viewportHeight - triggerRect.bottom - MENU_GAP - VIEWPORT_MARGIN,
    );
    const prefersUp = placement === "up";
    const openUp = prefersUp
      ? availableAbove >= Math.min(naturalHeight, MENU_MAX_HEIGHT) ||
        availableAbove >= availableBelow
      : !(
          availableBelow >= Math.min(naturalHeight, MENU_MAX_HEIGHT) ||
          availableBelow >= availableAbove
        );
    const availableHeight = openUp ? availableAbove : availableBelow;
    const maxHeight = Math.min(MENU_MAX_HEIGHT, availableHeight);
    const renderedHeight = Math.min(naturalHeight, maxHeight);

    const providerWidth = Math.min(
      MENU_MAX_WIDTH,
      Math.max(0, viewportWidth - VIEWPORT_MARGIN * 2),
    );
    let providerLeft =
      align === "right"
        ? triggerRect.right - providerWidth
        : align === "center"
          ? triggerRect.left + triggerRect.width / 2 - providerWidth / 2
          : triggerRect.left;
    providerLeft = Math.min(
      Math.max(VIEWPORT_MARGIN, providerLeft),
      Math.max(VIEWPORT_MARGIN, viewportWidth - providerWidth - VIEWPORT_MARGIN),
    );

    let left = providerLeft;
    if (isCascadeOpen) {
      const modelPanelWidth = width - providerWidth;
      const availableOnRight = viewportWidth - VIEWPORT_MARGIN - (providerLeft + providerWidth);
      const availableOnLeft = providerLeft - VIEWPORT_MARGIN;
      const nextDirection =
        availableOnRight >= modelPanelWidth || availableOnRight >= availableOnLeft
          ? "right"
          : "left";
      setCascadeDirection(nextDirection);
      left = nextDirection === "right" ? providerLeft : providerLeft - modelPanelWidth;
      left = Math.min(
        Math.max(VIEWPORT_MARGIN, left),
        Math.max(VIEWPORT_MARGIN, viewportWidth - width - VIEWPORT_MARGIN),
      );
    }

    const top = openUp
      ? triggerRect.top - MENU_GAP - renderedHeight
      : triggerRect.bottom + MENU_GAP;

    setMenuStyle({
      top: Math.max(VIEWPORT_MARGIN, top),
      left,
      width,
      maxHeight,
    });
  }, [
    activeModels.length,
    activeProvider,
    align,
    placement,
    providerGroups.length,
    supportsHoverPreview,
  ]);

  useLayoutEffect(() => {
    if (!isOpen) return;

    updateMenuPosition();
    const frame = window.requestAnimationFrame(updateMenuPosition);
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [isOpen, updateMenuPosition]);

  useLayoutEffect(() => {
    if (!isOpen) return;

    if (activeProvider && focusTargetRef.current === "model") {
      const preferredKey = selectedModel.provider === activeProvider ? value : "";
      const preferredOption = preferredKey ? modelOptionRefs.current.get(preferredKey) : undefined;
      const firstAvailable = activeModels
        .map((model) => modelOptionRefs.current.get(modelKey(model)))
        .find((option) => option && !option.disabled);
      (preferredOption ?? firstAvailable)?.focus();
      focusTargetRef.current = null;
      return;
    }

    if (!activeProvider && focusTargetRef.current === "provider") {
      const preferredProvider = lastActiveProviderRef.current ?? selectedModel.provider;
      const currentProviderOption = providerOptionRefs.current.get(preferredProvider);
      (currentProviderOption ?? providerOptionRefs.current.values().next().value)?.focus();
      focusTargetRef.current = null;
    }
  }, [activeModels, activeProvider, isOpen, selectedModel.provider, value]);

  useEffect(() => cancelHoverClose, [cancelHoverClose]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        closeMenu();
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && isOpen) {
        event.preventDefault();
        closeMenu(true);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeMenu, isOpen]);

  const menuClasses = [
    styles.menu,
    activeProvider ? styles.menuHasModels : "",
    activeProvider
      ? cascadeDirection === "left"
        ? styles.menuCascadeLeft
        : styles.menuCascadeRight
      : "",
    menuClassName ?? "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span ref={rootRef} className={`${styles.root} ${className ?? ""}`}>
      <select
        id={id}
        className={`${styles.nativeSelect} ${selectClassName ?? ""}`}
        value={value}
        onChange={(event) => {
          const nextKey = event.target.value;
          if (lockedSet.has(nextKey)) {
            onLockedSelect?.(nextKey);
            return;
          }
          onChange(nextKey);
        }}
        aria-hidden="true"
        tabIndex={-1}
      >
        {models.map((model) => {
          const key = modelKey(model);
          return (
            <option
              key={key}
              value={key}
              disabled={selectedSet.has(key) || disabledSet.has(key) || lockedSet.has(key)}
            >
              {getModelPresentation(model.provider, model.model).label}
            </option>
          );
        })}
      </select>

      <button
        ref={buttonRef}
        type="button"
        className={`${styles.button} ${buttonClassName ?? ""}`}
        aria-label={`${ariaLabel}: ${selectedMeta.label} (${selectedMeta.model})${
          selectedCreditLabel ? `, ${selectedCreditLabel} credit use` : ""
        }`}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={listboxId}
        title={`${selectedMeta.label}\n${selectedMeta.model}${
          selectedCreditLabel ? `\n${selectedCreditLabel} credit use` : ""
        }`}
        onClick={() => {
          if (isOpen) {
            closeMenu();
            return;
          }
          focusTargetRef.current = "provider";
          lastActiveProviderRef.current = selectedModel.provider;
          setActiveProvider(null);
          setIsOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            focusTargetRef.current = "provider";
            lastActiveProviderRef.current = selectedModel.provider;
            setActiveProvider(null);
            setIsOpen(true);
          }
        }}
      >
        <ProviderLogo
          provider={selectedModel.provider}
          logoUrl={selectedMeta.logoUrl}
          color={selectedMeta.color}
          size={20}
        />
        <span className={styles.selectedText}>
          <strong>{selectedMeta.label}</strong>
          <small>{selectedMeta.model}</small>
        </span>
        {lockedSet.has(value) ? (
          <span className={styles.selectedLock} aria-label="Locked on current plan">
            &#128274;
          </span>
        ) : null}
        <span className={styles.caret} aria-hidden="true" />
      </button>

      {isOpen &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={menuRef}
            id={listboxId}
            className={menuClasses}
            style={menuStyle}
            role="listbox"
            aria-label={listboxLabel}
            data-picker-view={activeProvider ? "models" : "providers"}
            data-picker-interaction={supportsHoverPreview ? "hover" : "drilldown"}
            onPointerEnter={cancelHoverClose}
            onPointerLeave={scheduleHoverPreviewClose}
            onKeyDown={(event) => {
              if (activeProvider && event.key === "ArrowLeft") {
                event.preventDefault();
                showProviders(true);
                return;
              }
              if (!activeProvider && event.key === "ArrowRight") {
                const providerKey = (event.target as HTMLElement).closest<HTMLElement>(
                  "[data-provider-key]",
                )?.dataset.providerKey;
                if (!providerKey) return;
                event.preventDefault();
                showProviderModels(providerKey, true);
                return;
              }
              if (
                event.key !== "ArrowDown" &&
                event.key !== "ArrowUp" &&
                event.key !== "Home" &&
                event.key !== "End"
              ) {
                return;
              }

              const focusedPanel = (document.activeElement as HTMLElement | null)?.closest(
                "[data-picker-panel]",
              );
              const activePanel =
                supportsHoverPreview && activeProvider
                  ? (focusedPanel ?? providerPanelRef.current)
                  : activeProvider
                    ? modelPanelRef.current
                    : providerPanelRef.current;
              const options = Array.from(
                activePanel?.querySelectorAll<HTMLButtonElement>(
                  'button[role="option"]:not(:disabled)',
                ) ?? [],
              );
              if (options.length === 0) return;
              event.preventDefault();
              const currentIndex = options.indexOf(document.activeElement as HTMLButtonElement);
              const nextIndex =
                currentIndex < 0
                  ? event.key === "ArrowUp" || event.key === "End"
                    ? options.length - 1
                    : 0
                  : event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? options.length - 1
                      : event.key === "ArrowDown"
                        ? (currentIndex + 1) % options.length
                        : (currentIndex - 1 + options.length) % options.length;
              options[nextIndex]?.focus();
            }}
          >
            <div
              ref={providerPanelRef}
              className={styles.providerPanel}
              data-picker-panel="providers"
              role="group"
              aria-label="Providers"
            >
              <div className={styles.menuHeader} role="presentation">
                <strong>Choose a provider</strong>
                <span>
                  {providerGroups.length} provider
                  {providerGroups.length === 1 ? "" : "s"}
                </span>
              </div>
              {providerGroups.map((group) => {
                const isCurrentProvider = group.key === selectedModel.provider;
                const isPreviewed = group.key === activeProvider;
                return (
                  <button
                    ref={(node) => {
                      if (node) providerOptionRefs.current.set(group.key, node);
                      else providerOptionRefs.current.delete(group.key);
                    }}
                    key={group.key}
                    type="button"
                    className={`${styles.option} ${styles.providerOption} ${
                      isCurrentProvider ? styles.providerOptionCurrent : ""
                    } ${isPreviewed ? styles.providerOptionPreview : ""}`}
                    role="option"
                    aria-selected={isCurrentProvider}
                    data-provider-key={group.key}
                    aria-label={`${group.label}, ${group.models.length} ${
                      group.models.length === 1 ? "model" : "models"
                    }${
                      isCurrentProvider ? `, current provider, selected ${selectedMeta.label}` : ""
                    }`}
                    title={`View ${group.label} models`}
                    onPointerEnter={(event) => {
                      if (supportsHoverPreview && event.pointerType !== "touch") {
                        showProviderModels(group.key, false);
                      }
                    }}
                    onClick={() => showProviderModels(group.key, true)}
                  >
                    <ProviderLogo
                      provider={group.key}
                      logoUrl={group.logoUrl}
                      color={group.color}
                      size={20}
                    />
                    <span className={styles.optionText}>
                      <strong>{group.label}</strong>
                      <small>
                        {group.models.length} {group.models.length === 1 ? "model" : "models"}
                        {isCurrentProvider ? ` \u00b7 ${selectedMeta.label} selected` : ""}
                      </small>
                    </span>
                    <span className={styles.providerActions} aria-hidden="true">
                      {isCurrentProvider ? (
                        <CortexIcon name="check" className={styles.checkIcon} />
                      ) : null}
                      <CortexIcon name="chevron-right" className={styles.providerChevron} />
                    </span>
                  </button>
                );
              })}
            </div>

            {activeProviderGroup ? (
              <div
                ref={modelPanelRef}
                className={styles.modelPanel}
                data-picker-panel="models"
                role="group"
                aria-label={`${activeProviderGroup.label} models`}
              >
                <button
                  type="button"
                  className={styles.backOption}
                  role="option"
                  aria-selected="false"
                  aria-label="Back to providers"
                  onClick={() => showProviders(true)}
                >
                  <CortexIcon name="chevron-left" />
                  <span className={styles.backText}>
                    <small>Providers</small>
                    <strong>{activeProviderGroup.label} models</strong>
                  </span>
                </button>
                <div className={styles.modelPanelHeader} role="presentation">
                  <ProviderLogo
                    provider={activeProviderGroup.key}
                    logoUrl={activeProviderGroup.logoUrl}
                    color={activeProviderGroup.color}
                    size={20}
                  />
                  <span>
                    <strong>{activeProviderGroup.label}</strong>
                    <small>
                      {activeModels.length} {activeModels.length === 1 ? "model" : "models"}
                    </small>
                  </span>
                </div>
                {activeModels.map((model) => {
                  const key = modelKey(model);
                  const meta = getModelPresentation(model.provider, model.model);
                  const isSelected = key === value;
                  const isDisabled = selectedSet.has(key) || disabledSet.has(key);
                  const isLocked = lockedSet.has(key);
                  return (
                    <button
                      ref={(node) => {
                        if (node) modelOptionRefs.current.set(key, node);
                        else modelOptionRefs.current.delete(key);
                      }}
                      key={key}
                      type="button"
                      className={`${styles.option} ${
                        isSelected ? styles.optionActive : ""
                      } ${isLocked ? styles.optionLocked : ""}`}
                      role="option"
                      aria-selected={isSelected}
                      aria-disabled={isDisabled || isLocked}
                      data-model-key={key}
                      disabled={isDisabled}
                      title={`${meta.label}\n${meta.model}`}
                      onClick={() => {
                        if (isLocked) {
                          onLockedSelect?.(key);
                          closeMenu();
                          return;
                        }
                        onChange(key);
                        closeMenu();
                      }}
                    >
                      <ProviderLogo
                        provider={model.provider}
                        logoUrl={meta.logoUrl}
                        color={meta.color}
                        size={20}
                      />
                      <span className={styles.optionText}>
                        <strong>{meta.label}</strong>
                        <small>
                          {meta.model}
                          {model.credit_usage_label
                            ? ` \u00b7 ${model.credit_usage_label} credit use`
                            : ""}
                        </small>
                      </span>
                      {isSelected && <CortexIcon name="check" className={styles.checkIcon} />}
                      {isLocked ? (
                        <PlanBadge label={lockedLabels[key] ?? "Upgrade"} tone="locked" />
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </div>,
          document.body,
        )}
    </span>
  );
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && Boolean(window.matchMedia?.(query).matches),
  );

  useEffect(() => {
    const mediaQuery = window.matchMedia?.(query);
    if (!mediaQuery) return;
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(mediaQuery.matches);
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, [query]);

  return matches;
}

function modelKey(model: ModelCatalogItem): string {
  return `${model.provider}:${model.model}`;
}

function modelFromKey(key: string): Pick<ModelCatalogItem, "provider" | "model"> {
  const separator = key.indexOf(":");
  if (separator < 0) return { provider: "", model: key };
  return {
    provider: key.slice(0, separator),
    model: key.slice(separator + 1),
  };
}

function groupModelsByProvider(models: ModelCatalogItem[]): ProviderGroup[] {
  const groups = new Map<string, ProviderGroup>();
  for (const model of models) {
    const existing = groups.get(model.provider);
    if (existing) {
      existing.models.push(model);
      continue;
    }

    const meta = getModelPresentation(model.provider, model.model);
    groups.set(model.provider, {
      key: model.provider,
      label: meta.providerLabel,
      logoUrl: meta.logoUrl,
      color: meta.color,
      models: [model],
    });
  }
  return Array.from(groups.values());
}
