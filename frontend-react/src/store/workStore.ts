import { create } from "zustand";
import type {
  ToolCatalogItem,
  ToolConnection,
  WorkApproval,
  WorkArtifact,
  WorkEvent,
  WorkRun,
  WorkSession,
  WorkWebMode,
} from "../types";

export interface WorkRunHistoryItem {
  run: WorkRun;
  events: WorkEvent[];
  artifacts: WorkArtifact[];
}

interface WorkStoreState {
  sessions: WorkSession[];
  session: WorkSession | null;
  history: WorkRunHistoryItem[];
  run: WorkRun | null;
  events: WorkEvent[];
  artifacts: WorkArtifact[];
  approval: WorkApproval | null;
  toolCatalog: ToolCatalogItem[];
  connections: ToolConnection[];
  enabledConnectionIds: string[];
  webMode: WorkWebMode;
  maxCreditBudget: number;
  loading: boolean;
  streaming: boolean;
  error: string | null;
  setSessions: (sessions: WorkSession[]) => void;
  setSession: (session: WorkSession | null) => void;
  setHistory: (items: WorkRunHistoryItem[]) => void;
  upsertHistoryItem: (item: WorkRunHistoryItem) => void;
  ensureSession: (createSession: () => Promise<WorkSession>) => Promise<WorkSession>;
  setRun: (run: WorkRun | null) => void;
  replaceEvents: (events: WorkEvent[]) => void;
  appendEvent: (event: WorkEvent) => void;
  setArtifacts: (artifacts: WorkArtifact[]) => void;
  setApproval: (approval: WorkApproval | null) => void;
  setToolCatalog: (items: ToolCatalogItem[]) => void;
  setConnections: (items: ToolConnection[]) => void;
  toggleConnection: (id: string) => void;
  setWebMode: (mode: WorkWebMode) => void;
  setMaxCreditBudget: (value: number) => void;
  setLoading: (loading: boolean) => void;
  setStreaming: (streaming: boolean) => void;
  setError: (error: string | null) => void;
  resetWorkspace: () => void;
}

export const useWorkStore = create<WorkStoreState>((set, get) => ({
  sessions: [],
  session: null,
  history: [],
  run: null,
  events: [],
  artifacts: [],
  approval: null,
  toolCatalog: [],
  connections: [],
  enabledConnectionIds: [],
  webMode: "auto",
  maxCreditBudget: 1_000_000,
  loading: false,
  streaming: false,
  error: null,
  setSessions: (sessions) => set({ sessions }),
  setSession: (session) => set({ session }),
  setHistory: (history) => set({ history: sortHistory(history) }),
  upsertHistoryItem: (item) =>
    set((state) => ({
      history: sortHistory([
        ...state.history.filter((existing) => existing.run.id !== item.run.id),
        item,
      ]),
    })),
  ensureSession: async (createSession) => {
    const existing = get().session;
    if (existing) return existing;
    const session = await createSession();
    set({ session });
    return session;
  },
  setRun: (run) => set({ run }),
  replaceEvents: (events) => set({ events: dedupeEvents(events) }),
  appendEvent: (event) =>
    set((state) => ({ events: dedupeEvents([...state.events, event]) })),
  setArtifacts: (artifacts) => set({ artifacts }),
  setApproval: (approval) => set({ approval }),
  setToolCatalog: (toolCatalog) => set({ toolCatalog }),
  setConnections: (connections) => set({ connections }),
  toggleConnection: (id) =>
    set((state) => ({
      enabledConnectionIds: state.enabledConnectionIds.includes(id)
        ? state.enabledConnectionIds.filter((value) => value !== id)
        : [...state.enabledConnectionIds, id],
    })),
  setWebMode: (webMode) => set({ webMode }),
  setMaxCreditBudget: (maxCreditBudget) => set({ maxCreditBudget }),
  setLoading: (loading) => set({ loading }),
  setStreaming: (streaming) => set({ streaming }),
  setError: (error) => set({ error }),
  resetWorkspace: () =>
    set({
      session: null,
      history: [],
      run: null,
      events: [],
      artifacts: [],
      approval: null,
      enabledConnectionIds: [],
      webMode: "auto",
      streaming: false,
      error: null,
    }),
}));

function dedupeEvents(events: WorkEvent[]): WorkEvent[] {
  return [...new Map(events.map((event) => [event.sequence, event])).values()].sort(
    (left, right) => left.sequence - right.sequence,
  );
}

function sortHistory(items: WorkRunHistoryItem[]): WorkRunHistoryItem[] {
  return [...items].sort(
    (left, right) =>
      new Date(left.run.created_at).getTime() - new Date(right.run.created_at).getTime(),
  );
}
