import { ThinkingOrb } from "thinking-orbs";
import { activityDisplayMessage } from "../../streaming/activityPresentation";
import type { CortexActivityEvent } from "../../types";
import styles from "./ResponseLoadingState.module.css";

export type ResponseLoadingMode = "ask" | "compare";

interface ResponseLoadingStateProps {
  mode: ResponseLoadingMode;
  researchEnabled?: boolean;
  optimizeEnabled?: boolean;
  activity?: CortexActivityEvent;
}

export function ResponseLoadingState({
  mode,
  researchEnabled = false,
  optimizeEnabled = false,
  activity,
}: ResponseLoadingStateProps) {
  const message =
    activityDisplayMessage(activity) ??
    getResponseLoadingMessage({ mode, researchEnabled, optimizeEnabled });
  const orbState = activity?.phase.includes("search") ? "searching" : "working";

  return (
    <div className={styles.loading} role="status" aria-live="polite">
      <div className={styles.statusLine}>
        <ThinkingOrb
          aria-hidden="true"
          className={`${styles.orb} response-loading-orb`}
          data-loading-state={orbState}
          size={20}
          state={orbState}
          theme="auto"
        />
        <span className={styles.message} data-activity-event={activity?.event_type}>
          {message}
        </span>
      </div>
      <div className={styles.skeleton} aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    </div>
  );
}

function getResponseLoadingMessage({ mode }: ResponseLoadingStateProps): string {
  return mode === "ask" ? "Starting…" : "Preparing this response…";
}
