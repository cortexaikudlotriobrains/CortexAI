import { useCallback } from "react";
import {
  fetchHistory,
  deleteHistoryEntry,
  clearHistory,
  renameHistorySession,
} from "../api/history";
import { fetchCortexAnalysisRuns } from "../api/cortexAnalysis";
import { buildHistoryThreads } from "../history/historyThreads";
import { loadActiveSessionId, normalizeSessionId } from "../session/activeSession";
import { useChatStore } from "../store/chatStore";
import { presentError, userFacingMessage } from "../errors/userFacingError";
import type { CortexAnalysisRun, HistoryEntry, HistoryThread } from "../types";

interface LoadHistoryOptions {
  sessionId?: string;
  restoreActiveTranscript?: boolean;
}

export function useHistory() {
  const { setHistory, setError } = useChatStore();

  const load = useCallback(
    async (input?: string | LoadHistoryOptions) => {
      const options: LoadHistoryOptions =
        typeof input === "string" ? { sessionId: input } : (input ?? {});
      try {
        const entries = await fetchHistory(500, options.sessionId);
        setHistory(entries);
        if (options.restoreActiveTranscript) {
          await restorePersistedActiveTranscript(entries);
        }
        return entries;
      } catch (err) {
        console.warn("History load failed", err);
        return [];
      }
    },
    [setHistory],
  );

  const remove = useCallback(
    async (id: number) => {
      try {
        await deleteHistoryEntry(id);
        const currentHistory = useChatStore.getState().history;
        setHistory(currentHistory.filter((entry) => entry.id !== id));
        return true;
      } catch (err) {
        setError(presentError(err, "history_delete"));
        return false;
      }
    },
    [setHistory, setError],
  );

  const removeThread = useCallback(
    async (thread: HistoryThread) => {
      const entryIds = [...new Set(thread.entries.map((entry) => entry.id))].filter((id) =>
        Number.isFinite(id),
      );
      if (entryIds.length === 0) return false;

      try {
        for (const id of entryIds) {
          await deleteHistoryEntry(id);
        }

        const removedIds = new Set(entryIds);
        const currentHistory = useChatStore.getState().history;
        setHistory(currentHistory.filter((entry) => !removedIds.has(entry.id)));
        return true;
      } catch (err) {
        setError(presentError(err, "history_delete"));
        return false;
      }
    },
    [setHistory, setError],
  );

  const renameThread = useCallback(
    async (thread: HistoryThread, title: string) => {
      const sessionId = normalizeSessionId(thread.sessionId);
      const normalizedTitle = title.trim();
      if (!sessionId || !normalizedTitle) {
        setError(userFacingMessage("This chat can't be renamed", "Choose a non-empty title and try again.", {
          code: "invalid_history_title",
          context: "history_rename",
        }));
        return false;
      }

      try {
        const renamed = await renameHistorySession(sessionId, normalizedTitle);
        const currentHistory = useChatStore.getState().history;
        setHistory(
          currentHistory.map((entry) =>
            normalizeSessionId(entry.session_id) === sessionId
              ? { ...entry, session_title: renamed.title }
              : entry,
          ),
        );
        return true;
      } catch (err) {
        setError(presentError(err, "history_rename"));
        return false;
      }
    },
    [setHistory, setError],
  );

  const clear = useCallback(
    async (sessionId?: string) => {
      try {
        await clearHistory(sessionId);
        const normalizedSessionId = normalizeSessionId(sessionId);
        if (!normalizedSessionId) {
          setHistory([]);
          return true;
        }

        const currentHistory = useChatStore.getState().history;
        setHistory(
          currentHistory.filter(
            (entry) => normalizeSessionId(entry.session_id) !== normalizedSessionId,
          ),
        );
        return true;
      } catch (err) {
        setError(presentError(err, "history_clear"));
        return false;
      }
    },
    [setHistory, setError],
  );

  return { load, remove, removeThread, renameThread, clear };
}

async function restorePersistedActiveTranscript(entries: HistoryEntry[]): Promise<void> {
  const state = useChatStore.getState();
  if (state.streaming || state.turns.length > 0) return;

  const activeSessionId = loadActiveSessionId();
  if (!activeSessionId) return;

  const sessionEntries = await fetchHistory(500, activeSessionId).catch(() => []);
  const analysisRuns = await fetchCortexAnalysisRuns({
    sessionId: activeSessionId,
  }).catch(() => []);
  const entriesToRestore =
    sessionEntries.length > 0
      ? sessionEntries
      : entries.filter(
          (entry) => normalizeSessionId(entry.session_id) === activeSessionId,
        );
  if (entriesToRestore.length > 0) {
    const merged = mergeHistoryEntries(entries, sessionEntries);
    useChatStore.getState().setHistory(merged);
    hydrateSessionFromEntries(activeSessionId, entriesToRestore, analysisRuns);
    return;
  }

  useChatStore.getState().setSessionId(null);
}

function hydrateSessionFromEntries(
  sessionId: string,
  entries: HistoryEntry[],
  analysisRuns: CortexAnalysisRun[] = [],
): boolean {
  const matchingEntries = entries.filter(
    (entry) => normalizeSessionId(entry.session_id) === sessionId,
  );
  const thread = buildHistoryThreads(matchingEntries)[0];
  if (!thread) return false;

  useChatStore.getState().hydrateFromHistoryThread(thread, analysisRuns);
  return true;
}

function mergeHistoryEntries(left: HistoryEntry[], right: HistoryEntry[]): HistoryEntry[] {
  const merged = new Map<number, HistoryEntry>();
  [...left, ...right].forEach((entry) => merged.set(entry.id, entry));
  return [...merged.values()];
}
