import { fetchCortexAnalysisRuns } from "../api/cortexAnalysis";
import { fetchHistory } from "../api/history";
import type { HistoryThread } from "../types";
import { buildHistoryThreads } from "./historyThreads";

export async function loadCompleteHistoryThread(thread: HistoryThread) {
  const [entries, analysisRuns] = thread.sessionId
    ? await Promise.all([
        fetchHistory(500, thread.sessionId),
        fetchCortexAnalysisRuns({ sessionId: thread.sessionId }),
      ])
    : [thread.entries, []];

  return {
    thread: buildHistoryThreads(entries)[0] ?? thread,
    analysisRuns,
  };
}
