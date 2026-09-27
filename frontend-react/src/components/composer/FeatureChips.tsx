import { useEffect, useRef, useState } from "react";
import { CortexIcon, type CortexIconName } from "../shared/CortexIcon";
import styles from "./FeatureChips.module.css";

const TOUCH_TOOLTIP_DURATION_MS = 2000;
type FeatureChipsVariant = "default" | "improveOnly";

interface FeatureChipsProps {
  smartMode: boolean;
  optimizeMode: boolean;
  compareMode?: boolean;
  variant?: FeatureChipsVariant;
  onSmartToggle: (v: boolean) => void;
  onOptimizeToggle: (v: boolean) => void;
  optimizeBlocked?: boolean;
  optimizeAllowanceLabel?: string;
  onOptimizeBlocked?: () => void;
}

export function FeatureChips({
  smartMode,
  optimizeMode,
  compareMode = false,
  variant = "default",
  onSmartToggle,
  onOptimizeToggle,
  optimizeBlocked = false,
  optimizeAllowanceLabel,
  onOptimizeBlocked,
}: FeatureChipsProps) {
  const [touchTooltipId, setTouchTooltipId] = useState<string | null>(null);
  const touchTooltipTimer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (touchTooltipTimer.current !== null) {
        window.clearTimeout(touchTooltipTimer.current);
      }
    };
  }, []);

  const showTouchTooltip = (tooltipId: string) => {
    if (touchTooltipTimer.current !== null) {
      window.clearTimeout(touchTooltipTimer.current);
    }
    setTouchTooltipId(tooltipId);
    touchTooltipTimer.current = window.setTimeout(() => {
      setTouchTooltipId(null);
      touchTooltipTimer.current = null;
    }, TOUCH_TOOLTIP_DURATION_MS);
  };

  const showSmart = !compareMode && variant === "default";
  const showOptimize = true;
  const stripClass = [
    styles.strip,
    variant === "improveOnly" ? styles.improveOnlyStrip : "",
  ]
    .filter(Boolean)
    .join(" ");

  const smartChip = showSmart ? (
    <Chip
      id="routeSmartBtn"
      active={smartMode}
      label="Smart"
      icon="smart"
      tooltip="Gets you the best answer automatically"
      tooltipAlign="start"
      onToggle={onSmartToggle}
      ariaLabel="Smart routing"
      touchTooltipId={touchTooltipId}
      onTouchTooltip={showTouchTooltip}
      tone="segment"
    />
  ) : null;

  const optimizeChip = showOptimize ? (
    <Chip
      id="routeOptimizeBtn"
      active={optimizeMode}
      label="Improve"
      icon="improve"
      tooltip={`Helps you ask better for better results${optimizeAllowanceLabel ? ` · ${optimizeAllowanceLabel}` : ""}`}
      tooltipAlign="end"
      onToggle={onOptimizeToggle}
      ariaLabel="Prompt optimization"
      touchTooltipId={touchTooltipId}
      onTouchTooltip={showTouchTooltip}
      tone="ghost"
      blocked={optimizeBlocked}
      onBlocked={onOptimizeBlocked}
    />
  ) : null;

  return (
    <div className={stripClass}>
      {smartChip && (
        <div className={styles.segmentedGroup}>
          {smartChip}
        </div>
      )}
      {optimizeChip}
    </div>
  );
}

interface ChipProps {
  active: boolean;
  label: string;
  icon: CortexIconName;
  onToggle: (v: boolean) => void;
  ariaLabel: string;
  tooltip: string;
  tooltipAlign: "start" | "center" | "end";
  touchTooltipId: string | null;
  onTouchTooltip: (tooltipId: string) => void;
  tone: "segment" | "ghost";
  id?: string;
  blocked?: boolean;
  onBlocked?: () => void;
}

function Chip({
  active,
  label,
  icon,
  tooltip,
  tooltipAlign,
  onToggle,
  ariaLabel,
  touchTooltipId,
  onTouchTooltip,
  tone,
  id,
  blocked = false,
  onBlocked,
}: ChipProps) {
  const tooltipId = `${id ?? label.toLowerCase().replace(/\s+/g, "-")}-tooltip`;
  const touchVisible = touchTooltipId === tooltipId;
  const chipClass = [
    styles.chip,
    styles[`${tone}Chip`],
    id === "routeSmartBtn" || id === "routeOptimizeBtn" ? styles.mobileIconOnly : "",
    active ? styles.active : "",
    blocked ? styles.blocked : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span className={styles.chipWrap}>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={active}
        aria-disabled={blocked && !active}
        aria-label={ariaLabel}
        aria-describedby={tooltipId}
        className={chipClass}
        onPointerUp={(event) => {
          if (event.pointerType === "touch") {
            onTouchTooltip(tooltipId);
          }
        }}
        onClick={() => {
          if (blocked && !active) {
            onBlocked?.();
            return;
          }
          onToggle(!active);
        }}
      >
        <CortexIcon name={icon} />
        <span>{label}</span>
      </button>
      <span
        id={tooltipId}
        role="tooltip"
        data-touch-visible={touchVisible ? "true" : "false"}
        className={`${styles.tooltip} ${styles[`tooltip${capitalize(tooltipAlign)}`]} ${
          touchVisible ? styles.tooltipVisible : ""
        }`}
      >
        {tooltip}
      </span>
    </span>
  );
}

function capitalize(value: string) {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}
