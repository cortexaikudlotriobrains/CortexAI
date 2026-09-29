import { useEffect, useRef, useState } from "react";
import { CortexIcon, type CortexIconName } from "../shared/CortexIcon";
import styles from "./FeatureChips.module.css";

const TOUCH_TOOLTIP_DURATION_MS = 2000;

interface FeatureChipsProps {
  smartMode: boolean;
  onSmartToggle: (v: boolean) => void;
}

export function FeatureChips({ smartMode, onSmartToggle }: FeatureChipsProps) {
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

  return (
    <div className={styles.strip}>
      <div className={styles.segmentedGroup}>
        <Chip
          id="routeSmartBtn"
          active={smartMode}
          label="Smart"
          icon="composer-smart"
          tooltip="Gets you the best answer automatically"
          tooltipAlign="start"
          onToggle={onSmartToggle}
          ariaLabel="Smart routing"
          touchTooltipId={touchTooltipId}
          onTouchTooltip={showTouchTooltip}
        />
      </div>
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
  id?: string;
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
  id,
}: ChipProps) {
  const tooltipId = `${id ?? label.toLowerCase().replace(/\s+/g, "-")}-tooltip`;
  const touchVisible = touchTooltipId === tooltipId;
  const chipClass = [
    styles.chip,
    styles.segmentChip,
    styles.mobileIconOnly,
    active ? styles.active : "",
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
        aria-label={ariaLabel}
        aria-describedby={tooltipId}
        className={chipClass}
        onPointerUp={(event) => {
          if (event.pointerType === "touch") {
            onTouchTooltip(tooltipId);
          }
        }}
        onClick={() => onToggle(!active)}
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
