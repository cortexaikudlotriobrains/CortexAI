import type { ChatResponse, CortexAnalysisRun } from "../types";

export function currentSuccessfulResponses(responses: ChatResponse[]) {
  return responses.filter(
    (response) =>
      !response.error &&
      response.ui_status !== "failed" &&
      (response.ui_status === undefined || response.ui_status === "complete" || response.ui_status === "incomplete") &&
      !!response.text.trim(),
  );
}

export function isCortexAnalysisRunStale(run: CortexAnalysisRun, responses: ChatResponse[]) {
  const current = currentSuccessfulResponses(responses)
    .map((response) => `${response.request_id}:${Math.max(1, response.response_version ?? 1)}`)
    .sort();
  const saved = run.sourceResponses
    .map((response) => `${response.requestId}:${Math.max(1, response.responseVersion)}`)
    .sort();
  return current.length !== saved.length || current.some((value, index) => value !== saved[index]);
}
