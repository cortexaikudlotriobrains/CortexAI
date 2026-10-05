import { useEffect, useState } from "react";
import type { CortexActivityEvent, CortexActivityType, ResponseRunStatus } from "../types";

const SEMANTIC_ACTIVITY_DELAY_MS = 140;

const FALLBACK_MESSAGES: Record<CortexActivityType, string> = {
  REQUEST_RECEIVED: "Starting…",
  REQUEST_IN_PROGRESS: "Preparing your response…",
  THINKING_STARTED: "Thinking…",
  THINKING_COMPLETED: "Reviewing the result…",
  SEARCH_STARTED: "Searching the web…",
  SEARCH_COMPLETED: "Reviewing sources…",
  FILE_SEARCH_STARTED: "Searching your files…",
  FILE_SEARCH_COMPLETED: "Reviewing the results…",
  FILE_READING_STARTED: "Reading your files…",
  FILE_READING_COMPLETED: "Reviewing the results…",
  TOOL_STARTED: "Using a tool…",
  TOOL_COMPLETED: "Reviewing the results…",
  MCP_TOOL_STARTED: "Using a connected service…",
  MCP_TOOL_COMPLETED: "Reviewing the results…",
  CODE_EXECUTION_STARTED: "Analyzing the data…",
  CODE_EXECUTION_COMPLETED: "Reviewing the results…",
  ANALYZING_RESULTS: "Reviewing the results…",
  ANSWER_STARTED: "Writing the answer…",
  ANSWER_DELTA: "Answering…",
  ANSWER_COMPLETED: "Finalizing…",
  WAITING_FOR_USER: "Waiting for your input…",
  WAITING_FOR_APPROVAL: "Waiting for your approval…",
  REQUEST_COMPLETED: "Complete",
  REQUEST_FAILED: "Response failed",
  REQUEST_CANCELLED: "Generation stopped",
};

const IMMEDIATE_ACTIVITY = new Set<CortexActivityType>([
  "REQUEST_RECEIVED",
  "REQUEST_IN_PROGRESS",
  "ANSWER_STARTED",
  "ANSWER_DELTA",
  "ANSWER_COMPLETED",
  "WAITING_FOR_USER",
  "WAITING_FOR_APPROVAL",
  "REQUEST_COMPLETED",
  "REQUEST_FAILED",
  "REQUEST_CANCELLED",
]);

export function activityDisplayMessage(activity?: CortexActivityEvent): string | undefined {
  if (!activity) return undefined;
  return activity.display_message.trim() || FALLBACK_MESSAGES[activity.event_type];
}

export function activityRunStatus(activity: CortexActivityEvent): ResponseRunStatus {
  if (activity.event_type === "REQUEST_FAILED") return "failed";
  if (activity.event_type === "REQUEST_CANCELLED") return "cancelled";
  if (activity.event_type === "ANSWER_COMPLETED") return "finalizing";
  if (activity.event_type === "REQUEST_COMPLETED") return "complete";
  if (activity.event_type === "ANSWER_STARTED" || activity.event_type === "ANSWER_DELTA") {
    return "streaming";
  }
  return "requesting";
}

export function isNewActivityEvent(
  current: CortexActivityEvent | undefined,
  incoming: CortexActivityEvent,
): boolean {
  if (!current) return true;
  return (
    current.request_id === incoming.request_id && incoming.sequence_number > current.sequence_number
  );
}

export function activityTargetIndexes(index: number | undefined, responseCount: number): number[] {
  if (index !== undefined) return [index];
  return Array.from({ length: Math.max(0, responseCount) }, (_, responseIndex) => responseIndex);
}

export function useSmoothedActivity(
  activity: CortexActivityEvent | undefined,
  enabled: boolean,
): CortexActivityEvent | undefined {
  const [displayed, setDisplayed] = useState<CortexActivityEvent | undefined>(activity);

  useEffect(() => {
    if (!enabled || !activity) {
      setDisplayed(activity);
      return undefined;
    }
    if (displayed?.sequence_number === activity.sequence_number) return undefined;
    if (IMMEDIATE_ACTIVITY.has(activity.event_type) || !displayed) {
      setDisplayed(activity);
      return undefined;
    }
    const timer = window.setTimeout(() => setDisplayed(activity), SEMANTIC_ACTIVITY_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [activity, displayed, enabled]);

  return displayed;
}
