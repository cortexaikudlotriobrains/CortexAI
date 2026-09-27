import { create } from "zustand";
import type {
  ChatMode,
  ChatResponse,
  ChatTurn,
  CompareResponse,
  CortexAnalysisRun,
  CortexAnalysisStatus,
  FileUploadResponse,
  HistoryEntry,
  HistoryThread,
  PromptOptimizationState,
  ReasoningLevel,
  ResponseRunStatus,
  TurnStatus,
} from "../types";
import { buildTurnsFromHistoryEntries } from "../history/historyThreads";
import {
  clearActiveSessionId,
  consumeFreshLoginSessionReset,
  loadActiveSessionId,
  normalizeSessionId,
  persistActiveSessionId,
} from "../session/activeSession";
import type { SubscriptionError } from "../subscription/subscriptionErrors";
import { clearAttachmentUploads } from "../uploads/attachmentUploadQueue";

interface BeginTurnInput {
  mode: ChatMode;
  prompt: string;
  submittedPrompt: string;
  researchEnabled?: boolean;
  optimizeEnabled?: boolean;
  attachments: FileUploadResponse[];
  responses: ChatResponse[];
  status?: TurnStatus;
  optimization?: PromptOptimizationState;
}

interface PrepareTurnInput {
  prompt: string;
  submittedPrompt: string;
  responses: ChatResponse[];
  optimization?: PromptOptimizationState;
}

interface ChatStoreState {
  mode: ChatMode;
  setMode: (mode: ChatMode) => void;

  smartMode: boolean;
  optimizeMode: boolean;
  askReasoningLevel: ReasoningLevel;
  compareReasoningLevel: ReasoningLevel;
  setSmartMode: (v: boolean) => void;
  setOptimizeMode: (v: boolean) => void;
  setAskReasoningLevel: (level: ReasoningLevel) => void;
  setCompareReasoningLevel: (level: ReasoningLevel) => void;

  selectedModelKey: string;
  setSelectedModelKey: (key: string) => void;
  compareModelKeys: [string, string, string];
  setCompareModelKey: (index: 0 | 1 | 2, key: string) => void;

  prompt: string;
  setPrompt: (prompt: string) => void;

  attachments: FileUploadResponse[];
  addAttachment: (file: FileUploadResponse) => void;
  updateAttachment: (file: FileUploadResponse) => void;
  removeAttachment: (fileId: string) => void;
  clearAttachments: () => void;

  turns: ChatTurn[];
  activeTurnId: string | null;
  beginTurn: (input: BeginTurnInput) => string;
  prepareTurnForStreaming: (turnId: string, input: PrepareTurnInput) => void;
  prepareTurnResponseForStreaming: (
    turnId: string,
    index: number,
    response: ChatResponse,
  ) => void;
  setTurnOptimization: (turnId: string, optimization: PromptOptimizationState) => void;
  updateTurnResponse: (turnId: string, index: number, response: ChatResponse) => void;
  appendTurnResponseText: (
    turnId: string,
    index: number,
    text: string,
    patch?: Partial<ChatResponse>,
  ) => void;
  setTurnStatus: (turnId: string, status: TurnStatus) => void;
  discardTurn: (turnId: string) => void;
  setTurnCompareSummary: (turnId: string, summary: CompareResponse) => void;
  setTurnAnalysisStatus: (
    turnId: string,
    status: CortexAnalysisStatus,
    error?: string,
  ) => void;
  addTurnAnalysisRun: (turnId: string, run: CortexAnalysisRun) => void;
  hydrateFromHistoryThread: (
    thread: HistoryThread,
    analysisRuns?: CortexAnalysisRun[],
  ) => void;
  startNewChat: () => void;

  responses: ChatResponse[];
  setResponses: (responses: ChatResponse[]) => void;
  appendResponse: (response: ChatResponse) => void;
  clearResponses: () => void;

  streaming: boolean;
  streamingText: string;
  setStreaming: (v: boolean) => void;
  setStreamingText: (text: string) => void;
  appendStreamingText: (chunk: string) => void;

  error: string | null;
  setError: (err: string | null) => void;
  subscriptionError: SubscriptionError | null;
  setSubscriptionError: (err: SubscriptionError | null) => void;

  history: HistoryEntry[];
  setHistory: (entries: HistoryEntry[]) => void;
  historySearch: string;
  setHistorySearch: (q: string) => void;

  sessionId: string | null;
  setSessionId: (id: string | null) => void;
  pendingNewSession: boolean;
  setPendingNewSession: (v: boolean) => void;
}

const shouldResetForFreshLogin = consumeFreshLoginSessionReset();
const initialSessionId = shouldResetForFreshLogin ? null : loadActiveSessionId();

export const useChatStore = create<ChatStoreState>((set) => ({
  mode: "single",
  setMode: (mode) => set({ mode }),

  smartMode: true,
  optimizeMode: false,
  askReasoningLevel: "auto",
  compareReasoningLevel: "low",
  setSmartMode: (v) => set({ smartMode: v }),
  setOptimizeMode: (v) => set({ optimizeMode: v }),
  setAskReasoningLevel: (level) => set({ askReasoningLevel: level }),
  setCompareReasoningLevel: (level) => set({ compareReasoningLevel: level }),

  selectedModelKey: "",
  setSelectedModelKey: (key) => set({ selectedModelKey: key }),
  compareModelKeys: ["", "", ""],
  setCompareModelKey: (index, key) =>
    set((state) => {
      const updated = [...state.compareModelKeys] as [string, string, string];
      updated[index] = key;
      // On removal, compact: shift non-empty values to the front, empty slots to the back
      if (!key) {
        const compacted = updated.filter(Boolean);
        while (compacted.length < 3) compacted.push("");
        return { compareModelKeys: compacted as [string, string, string] };
      }
      return { compareModelKeys: updated };
    }),

  prompt: "",
  setPrompt: (prompt) => set({ prompt }),

  attachments: [],
  addAttachment: (file) =>
    set((state) => ({
      attachments: state.attachments.some((item) => item.file_id === file.file_id)
        ? state.attachments
        : [...state.attachments, file],
    })),
  updateAttachment: (file) =>
    set((state) => ({
      attachments: state.attachments.map((item) =>
        item.file_id === file.file_id ? { ...item, ...file } : item,
      ),
    })),
  removeAttachment: (fileId) =>
    set((state) => ({
      attachments: state.attachments.filter((a) => a.file_id !== fileId),
    })),
  clearAttachments: () => set({ attachments: [] }),

  turns: [],
  activeTurnId: null,
  beginTurn: (input) => {
    const id = makeId();
    const turn: ChatTurn = {
      id,
      mode: input.mode,
      prompt: input.prompt,
      submittedPrompt: input.submittedPrompt,
      researchEnabled: input.researchEnabled,
      optimizeEnabled: input.optimizeEnabled,
      attachments: input.attachments,
      responses: input.responses,
      status: input.status ?? "streaming",
      createdAt: new Date().toISOString(),
      optimization: input.optimization,
    };
    set((state) => ({
      turns: [...state.turns, turn],
      activeTurnId: id,
      responses: input.responses,
      streaming: turn.status === "optimizing" || turn.status === "streaming",
      streamingText: "",
      error: null,
    }));
    return id;
  },
  prepareTurnForStreaming: (turnId, input) =>
    set((state) => {
      const turns = state.turns.map((turn) =>
        turn.id === turnId
          ? {
              ...turn,
              prompt: input.prompt,
              submittedPrompt: input.submittedPrompt,
              responses: input.responses,
              status: "streaming" as const,
              optimization: input.optimization ?? turn.optimization,
            }
          : turn,
      );
      return {
        turns,
        responses: state.activeTurnId === turnId ? input.responses : state.responses,
        streaming: true,
        streamingText: "",
      };
    }),
  prepareTurnResponseForStreaming: (turnId, index, response) =>
    set((state) => {
      const updated = updateResponseState(state, turnId, index, () => response);
      return {
        ...updated,
        activeTurnId: turnId,
        turns: updated.turns.map((turn) =>
          turn.id === turnId ? { ...turn, status: "streaming" as const } : turn,
        ),
        responses:
          updated.turns.find((turn) => turn.id === turnId)?.responses ??
          updated.responses,
        streaming: true,
        streamingText: "",
        error: null,
      };
    }),
  setTurnOptimization: (turnId, optimization) =>
    set((state) => ({
      turns: state.turns.map((turn) =>
        turn.id === turnId ? { ...turn, optimization } : turn,
      ),
    })),
  updateTurnResponse: (turnId, index, response) =>
    set((state) => updateResponseState(state, turnId, index, () => response)),
  appendTurnResponseText: (turnId, index, text, patch) =>
    set((state) =>
      updateResponseState(state, turnId, index, (current) => ({
        ...current,
        ...patch,
        text: `${current.text}${text}`,
      })),
    ),
  setTurnStatus: (turnId, status) =>
    set((state) => ({
      turns: state.turns.map((turn) => (turn.id === turnId ? { ...turn, status } : turn)),
      streaming:
        status === "optimizing" || status === "streaming"
          ? true
          : state.activeTurnId === turnId
            ? false
            : state.streaming,
    })),
  discardTurn: (turnId) =>
    set((state) => {
      const turns = state.turns.filter((turn) => turn.id !== turnId);
      const nextActive = turns[turns.length - 1] ?? null;
      return {
        turns,
        activeTurnId: nextActive?.id ?? null,
        responses: nextActive?.responses ?? [],
        streaming: false,
        streamingText: "",
      };
    }),
  setTurnCompareSummary: (turnId, summary) =>
    set((state) => {
      let activeResponses = state.responses;
      const turns = state.turns.map((turn) => {
        if (turn.id !== turnId) return turn;
        const responses = mergeCompletedResponses(turn.responses, summary.responses);
        if (state.activeTurnId === turnId) activeResponses = responses;
        return {
          ...turn,
          requestGroupId: summary.request_group_id,
          compareSummary: { ...summary, responses },
          responses,
        };
      });
      return {
        turns,
        responses: state.activeTurnId === turnId ? activeResponses : state.responses,
      };
    }),
  setTurnAnalysisStatus: (turnId, status, error) =>
    set((state) => ({
      turns: state.turns.map((turn) =>
        turn.id === turnId
          ? {
              ...turn,
              analysisStatus: status,
              analysisError: status === "failed" ? error : undefined,
            }
          : turn,
      ),
    })),
  addTurnAnalysisRun: (turnId, run) =>
    set((state) => ({
      turns: state.turns.map((turn) =>
        turn.id === turnId
          ? {
              ...turn,
              analysisRuns: [
                run,
                ...(turn.analysisRuns ?? []).filter(
                  (item) => item.analysisId !== run.analysisId,
                ),
              ],
              analysisStatus: "idle",
              analysisError: undefined,
            }
          : turn,
      ),
    })),
  hydrateFromHistoryThread: (thread, analysisRuns = []) => {
    void clearAttachmentUploads({ deleteRemote: true });
    const analysisByGroup = new Map<string, CortexAnalysisRun[]>();
    for (const run of analysisRuns) {
      const existing = analysisByGroup.get(run.requestGroupId) ?? [];
      existing.push(run);
      analysisByGroup.set(run.requestGroupId, existing);
    }
    const turns = buildTurnsFromHistoryEntries(thread.entries).map((turn) => ({
      ...turn,
      analysisRuns: turn.requestGroupId
        ? analysisByGroup.get(turn.requestGroupId) ?? []
        : [],
      analysisStatus: "idle" as const,
    }));
    const latestTurn = turns[turns.length - 1] ?? null;
    const sessionId = normalizeSessionId(thread.sessionId);
    persistActiveSessionId(sessionId);
    set({
      mode: latestTurn?.mode ?? thread.preferredMode,
      prompt: "",
      attachments: [],
      activeTurnId: latestTurn?.id ?? null,
      turns,
      responses: latestTurn?.responses ?? [],
      streaming: false,
      streamingText: "",
      sessionId,
      pendingNewSession: !sessionId,
      error: null,
      subscriptionError: null,
    });
  },
  startNewChat: () => {
    void clearAttachmentUploads({ deleteRemote: true });
    clearActiveSessionId();
    set({
      prompt: "",
      attachments: [],
      turns: [],
      activeTurnId: null,
      responses: [],
      streamingText: "",
      streaming: false,
      error: null,
      subscriptionError: null,
      sessionId: null,
      pendingNewSession: true,
    });
  },

  responses: [],
  setResponses: (responses) => set({ responses }),
  appendResponse: (response) => set((state) => ({ responses: [...state.responses, response] })),
  clearResponses: () => set({ responses: [], streamingText: "", streaming: false, turns: [] }),

  streaming: false,
  streamingText: "",
  setStreaming: (v) => set({ streaming: v }),
  setStreamingText: (text) => set({ streamingText: text }),
  appendStreamingText: (chunk) =>
    set((state) => ({ streamingText: state.streamingText + chunk })),

  error: null,
  setError: (err) => set({ error: err }),
  subscriptionError: null,
  setSubscriptionError: (err) => set({ subscriptionError: err }),

  history: [],
  setHistory: (entries) => set({ history: entries }),
  historySearch: "",
  setHistorySearch: (q) => set({ historySearch: q }),

  sessionId: initialSessionId,
  setSessionId: (id) => {
    const sessionId = normalizeSessionId(id);
    persistActiveSessionId(sessionId);
    set({ sessionId, pendingNewSession: !sessionId });
  },
  pendingNewSession: !initialSessionId,
  setPendingNewSession: (v) => set({ pendingNewSession: v }),
}));

export function makePlaceholderResponse(
  index: number,
  provider: string,
  model: string,
  sessionId?: string | null,
  options: { status?: ResponseRunStatus; startedAt?: string } = {},
): ChatResponse {
  const timestamp = new Date().toISOString();
  const startedAt = options.startedAt ?? timestamp;
  return {
    request_id: makeId(`pending-${index}`),
    session_id: sessionId ?? undefined,
    text: "",
    provider: provider || "auto",
    model: model || "Working",
    latency_ms: null,
    token_usage: null,
    estimated_cost: 0,
    cost_currency: "USD",
    web_source_items: [],
    timestamp,
    ui_status: options.status ?? "queued",
    started_at: startedAt,
  };
}

type StoreSnapshot = Pick<ChatStoreState, "turns" | "activeTurnId" | "responses" | "streaming">;

function updateResponseState(
  state: StoreSnapshot,
  turnId: string,
  index: number,
  updater: (current: ChatResponse) => ChatResponse,
) {
  const turns = state.turns.map((turn) => {
    if (turn.id !== turnId) return turn;
    const responses = [...turn.responses];
    const current = responses[index] ?? makePlaceholderResponse(index, "", "", undefined);
    responses[index] = recordFirstVisibleTimestamp(current, updater(current));
    return {
      ...turn,
      responses,
      compareSummary: turn.compareSummary
        ? summarizeCompareResponses(turn.compareSummary, responses)
        : turn.compareSummary,
    };
  });

  const active = turns.find((turn) => turn.id === state.activeTurnId);
  return {
    turns,
    responses: state.activeTurnId === turnId ? active?.responses ?? state.responses : state.responses,
  };
}

function summarizeCompareResponses(
  previousSummary: CompareResponse,
  responses: ChatResponse[],
): CompareResponse {
  const completed = responses.filter((response) => response.ui_status === "complete");
  return {
    ...previousSummary,
    responses,
    success_count: completed.filter((response) => !response.error).length,
    error_count: responses.filter((response) => !!response.error).length,
    total_tokens: responses.reduce(
      (total, response) => total + (response.token_usage?.total_tokens ?? 0),
      0,
    ),
    total_cost: responses.reduce(
      (total, response) => total + (response.estimated_cost || 0),
      0,
    ),
  };
}

function mergeCompletedResponses(
  existing: ChatResponse[],
  completed: ChatResponse[],
): ChatResponse[] {
  const resolvedAt = new Date().toISOString();
  return completed.map((response, index) => {
    const previous = existing[index];
    const failed = !!response.error;
    return {
      ...response,
      started_at: previous?.started_at ?? response.started_at,
      first_visible_at: previous?.first_visible_at ?? response.first_visible_at,
      completed_at: failed
        ? response.completed_at ?? previous?.completed_at
        : response.completed_at ?? previous?.completed_at ?? resolvedAt,
      failed_at: failed
        ? response.failed_at ?? previous?.failed_at ?? resolvedAt
        : response.failed_at,
      ui_status: failed ? "failed" : "complete",
    };
  });
}

function hasVisibleResponseText(text: string): boolean {
  return text.trim().length > 0;
}

function recordFirstVisibleTimestamp(
  current: ChatResponse,
  next: ChatResponse,
): ChatResponse {
  if (next.first_visible_at) return next;
  if (current.first_visible_at) {
    return { ...next, first_visible_at: current.first_visible_at };
  }
  if (hasVisibleResponseText(current.text) || !hasVisibleResponseText(next.text)) {
    return next;
  }
  return { ...next, first_visible_at: new Date().toISOString() };
}

function makeId(prefix = "turn"): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}
