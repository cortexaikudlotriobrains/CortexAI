import type { ReasoningLevel } from "../../types";
import { CortexIcon } from "../shared/CortexIcon";
import styles from "./FeatureChips.module.css";

interface FeatureChipsProps {
  optimizeMode: boolean;
  reasoningValue: ReasoningLevel;
  reasoningUnavailable?: boolean;
  onOpen: () => void;
  onOptimizeReset: () => void;
  onReasoningReset: () => void;
}

export function FeatureChips({
  optimizeMode,
  reasoningValue,
  reasoningUnavailable = false,
  onOpen,
  onOptimizeReset,
  onReasoningReset,
}: FeatureChipsProps) {
  const label =
    reasoningValue === "max"
      ? "Maximum"
      : `${reasoningValue[0].toUpperCase()}${reasoningValue.slice(1)}`;
  const reasoning = !reasoningUnavailable && reasoningValue !== "auto";
  if (!reasoning && !optimizeMode) return null;
  return (
    <div className={styles.chips} data-options-chips aria-label="Active composer options">
      {reasoning && (
        <div className={styles.chip} data-option-chip="reasoning">
          <button
            type="button"
            className={styles.body}
            aria-label={`Edit ${label} reasoning setting`}
            onClick={onOpen}
          >
            <CortexIcon name="composer-smart" />
            <span>{label}</span>
          </button>
          <button
            type="button"
            className={styles.reset}
            aria-label="Reset reasoning"
            onClick={onReasoningReset}
          >
            <CortexIcon name="chip-close" />
          </button>
        </div>
      )}
      {optimizeMode && (
        <div className={styles.chip} data-option-chip="improve">
          <button
            type="button"
            className={styles.body}
            aria-label="Edit Improve prompt setting"
            onClick={onOpen}
          >
            <CortexIcon name="composer-improve" />
            <span>Improve</span>
          </button>
          <button
            type="button"
            className={styles.reset}
            aria-label="Turn off Improve prompt"
            onClick={onOptimizeReset}
          >
            <CortexIcon name="chip-close" />
          </button>
        </div>
      )}
    </div>
  );
}
