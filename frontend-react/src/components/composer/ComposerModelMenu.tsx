import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ModelCatalogItem } from "../../types";
import { getModelPresentation } from "../../config/modelPresentation";
import { CortexIcon } from "../shared/CortexIcon";
import { ProviderLogo } from "../shared/ProviderLogo";
import { PlanBadge } from "../subscription/PlanBadge";
import { ComposerPopover } from "./ComposerPopover";
import styles from "./ComposerModelMenu.module.css";

export interface ComposerModelMenuConfig {
  smart?: boolean;
  recentModels?: string[];
  newChatStart?: string;
  onNewChatStart?: (key: string) => void;
  compareKeys?: [string, string, string];
  onCompareToggle?: (key: string) => void;
  maxTargets?: number;
  thirdTargetPlanLabel?: string;
  onTargetLimit?: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  interactionBlocked?: boolean;
}

export interface ComposerModelMenuProps {
  id: string;
  models: ModelCatalogItem[];
  value: string;
  onChange: (key: string) => void;
  lockedKeys?: string[];
  lockedLabels?: Record<string, string>;
  onLockedSelect?: (key: string) => void;
  composer: ComposerModelMenuConfig;
}

type MenuModel = Pick<
  ModelCatalogItem,
  "provider" | "model" | "display_name" | "credit_usage_label"
>;
const modelKey = (model: MenuModel) => `${model.provider}:${model.model}`;
function composerModelName(model: MenuModel) {
  return model.display_name || getModelPresentation(model.provider, model.model).label;
}

function Logo({ model }: { model: MenuModel }) {
  const meta = getModelPresentation(model.provider, model.model);
  return (
    <ProviderLogo provider={model.provider} logoUrl={meta.logoUrl} color={meta.color} size={18} />
  );
}

export function ComposerModelMenu({
  id,
  models,
  value,
  onChange,
  lockedKeys = [],
  lockedLabels = {},
  onLockedSelect,
  composer,
}: ComposerModelMenuProps) {
  const [view, setView] = useState<"main" | "all" | "newChats">("main");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const { open, onOpenChange, compareKeys, smart = false } = composer;
  const catalog = models.filter((model) => model.enabled && model.selectable !== false);
  const selectedModel = catalog.find((model) => modelKey(model) === value);
  const displayModelForKey = (key: string): MenuModel => {
    const found = models.find((model) => modelKey(model) === key);
    if (found) return found;
    const separator = key.indexOf(":");
    return {
      provider: separator < 0 ? "" : key.slice(0, separator),
      model: separator < 0 ? key : key.slice(separator + 1),
    };
  };
  const compared = (compareKeys ?? []).filter(Boolean).map(displayModelForKey);
  // Keep restored targets visible so a retired model can still be removed.
  const compareCatalog = [
    ...catalog,
    ...(compareKeys ?? [])
      .filter((key) => key && !catalog.some((model) => modelKey(model) === key))
      .map(displayModelForKey),
  ];
  const count = compareKeys?.filter(Boolean).length ?? 0;
  const name = smart
    ? "Smart"
    : selectedModel
      ? composerModelName(selectedModel)
      : value.split(":").slice(1).join(":") || "Choose model";
  const shortName = (model: MenuModel) =>
    composerModelName(model).replace(/^(?:GPT-[\d.]+|Claude|Gemini|DeepSeek|Grok)\s+/i, "");
  const chipLabel = compareKeys
    ? count === 3
      ? "3 models"
      : compared.map(shortName).join(" vs ")
    : name;
  const savedModel = catalog.find((model) => modelKey(model) === composer.newChatStart);
  const groups = Array.from(new Set(catalog.map((model) => model.provider)));
  const close = useCallback(() => onOpenChange(false), [onOpenChange]);
  const back = useCallback(() => setView("main"), []);
  useEffect(() => {
    if (!open) setView("main");
  }, [open]);

  const choose = (key: string) => {
    if (lockedKeys.includes(key)) {
      onLockedSelect?.(key);
      return;
    }
    if (view === "newChats") {
      composer.onNewChatStart?.(key);
      setView("main");
    } else {
      onChange(key);
      close();
    }
  };
  const smartRow = (selected: boolean) => (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      className={`${styles.row} ${selected ? styles.selected : ""}`}
      onClick={() => choose("smart")}
    >
      <span className={`${styles.tile} ${styles.smartTile}`}>
        <CortexIcon name="composer-smart" />
      </span>
      <span className={styles.copy}>
        <strong>Smart</strong>
        <small>Picks the best model for each prompt</small>
      </span>
      {selected && <CortexIcon name="check" className={styles.check} />}
    </button>
  );
  const modelRow = (model: MenuModel) => {
    const key = modelKey(model);
    const selected = compareKeys
      ? compareKeys.includes(key)
      : view === "newChats"
        ? composer.newChatStart === key
        : !smart && value === key;
    const locked = lockedKeys.includes(key);
    const atMinimum = Boolean(compareKeys && selected && count <= 2);
    const atMaximum = Boolean(compareKeys && !selected && count >= 3);
    const planLimit = Boolean(
      compareKeys && !selected && count >= (composer.maxTargets ?? 3) && !atMaximum,
    );
    const disabled = atMinimum || atMaximum;
    return (
      <button
        key={key}
        type="button"
        role="option"
        aria-selected={selected}
        aria-disabled={disabled || undefined}
        disabled={disabled}
        data-model-key={key}
        data-locked={locked || undefined}
        className={`${styles.row} ${selected ? styles.selected : ""}`}
        onClick={() => {
          if (compareKeys && selected) composer.onCompareToggle?.(key);
          else if (locked) onLockedSelect?.(key);
          else if (planLimit) composer.onTargetLimit?.();
          else if (compareKeys) composer.onCompareToggle?.(key);
          else choose(key);
        }}
      >
        {compareKeys && (
          <span
            className={`${styles.checkbox} ${selected ? styles.checked : ""}`}
            aria-hidden="true"
          >
            {selected && <CortexIcon name="check" />}
          </span>
        )}
        <span className={styles.tile}>
          <Logo model={model} />
        </span>
        <span className={styles.copy}>
          <strong>{composerModelName(model)}</strong>
          <small className={styles.modelId}>{model.model}</small>
        </span>
        {locked || planLimit ? (
          <PlanBadge
            tone="locked"
            label={
              locked ? lockedLabels[key] || "Upgrade" : composer.thirdTargetPlanLabel || "Upgrade"
            }
          />
        ) : (
          <>
            {view !== "newChats" && model.credit_usage_label && (
              <span className={styles.tag}>{model.credit_usage_label}</span>
            )}
            {selected && !compareKeys && <CortexIcon name="check" className={styles.check} />}
          </>
        )}
      </button>
    );
  };
  const listKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
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

  return (
    <>
      <button
        ref={triggerRef}
        id={`${id}Button`}
        type="button"
        className={`${styles.trigger} ${open ? styles.open : ""}`}
        aria-label={compareKeys ? "Models to compare" : `Model: ${name}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${id}Options`}
        onClick={() => onOpenChange(!open)}
      >
        {compareKeys ? (
          <span className={styles.logos}>
            {compared.slice(0, 2).map((model) => (
              <Logo key={modelKey(model)} model={model} />
            ))}
          </span>
        ) : smart ? (
          <CortexIcon name="composer-smart" className={styles.smartIcon} />
        ) : selectedModel ? (
          <Logo model={selectedModel} />
        ) : null}
        <span className={styles.chipName}>{chipLabel}</span>
        <CortexIcon name="chevron-right" className={styles.chevron} />
      </button>
      <ComposerPopover
        open={open}
        triggerRef={triggerRef}
        label={compareKeys ? "Models to compare" : "Choose a model"}
        id={`${id}Menu`}
        onClose={close}
        onBack={view === "main" ? undefined : back}
        view={view}
        interactionBlocked={composer.interactionBlocked}
      >
        <div
          role="listbox"
          id={`${id}Options`}
          aria-label={
            compareKeys
              ? "Compare models"
              : view === "all"
                ? "All models"
                : view === "newChats"
                  ? "New chats start with"
                  : "Model options"
          }
          aria-multiselectable={compareKeys ? true : undefined}
          onKeyDown={listKeyboard}
        >
          {compareKeys ? (
            <>
              <div className={styles.header}>
                <span className={styles.sectionLabel}>COMPARE 2–3 MODELS</span>
                <span className={styles.count}>{count}/3</span>
              </div>
              {compareCatalog.map(modelRow)}
              <p className={styles.note}>
                Smart isn't available in Compare. Each model answers the same prompt side by side.
              </p>
            </>
          ) : view === "main" ? (
            <>
              {smartRow(smart)}
              <div className={styles.divider} />
              <div className={styles.sectionLabel}>OR CHOOSE A MODEL</div>
              {(composer.recentModels ?? [])
                .map((key) => catalog.find((model) => modelKey(model) === key))
                .filter((model): model is ModelCatalogItem => Boolean(model))
                .slice(0, 3)
                .map(modelRow)}
              <div className={styles.divider} />
              <button type="button" className={styles.footerRow} onClick={() => setView("all")}>
                <span>All models</span>
                <CortexIcon name="chevron-right" />
              </button>
              <div className={styles.divider} />
              <button
                type="button"
                className={styles.footerRow}
                onClick={() => setView("newChats")}
              >
                <span>New chats start with</span>
                <span className={styles.preferenceValue}>
                  {savedModel ? (
                    <Logo model={savedModel} />
                  ) : (
                    <CortexIcon name="composer-smart" className={styles.smartIcon} />
                  )}
                  <span>
                    {savedModel
                      ? composerModelName(savedModel)
                      : composer.newChatStart === "smart"
                        ? "Smart"
                        : composer.newChatStart?.split(":").slice(1).join(":") || "Smart"}
                  </span>
                </span>
                <CortexIcon name="chevron-right" />
              </button>
            </>
          ) : (
            <>
              <div className={styles.header}>
                <button type="button" className={styles.back} onClick={back}>
                  <CortexIcon name="chevron-left" />
                  Back
                </button>
                <span className={styles.sectionLabel}>
                  {view === "all" ? "ALL MODELS" : "NEW CHATS START WITH"}
                </span>
              </div>
              {view === "newChats" && (
                <>
                  {smartRow(composer.newChatStart === "smart")}
                  <div className={styles.divider} />
                </>
              )}
              {groups.map((provider) => (
                <div key={provider}>
                  {view === "all" && (
                    <div className={styles.sectionLabel}>{provider.toUpperCase()}</div>
                  )}
                  {catalog.filter((model) => model.provider === provider).map(modelRow)}
                </div>
              ))}
              {view === "newChats" && (
                <p className={styles.note}>
                  Applies to new chats. This chat keeps what you picked above.
                </p>
              )}
            </>
          )}
        </div>
      </ComposerPopover>
    </>
  );
}
