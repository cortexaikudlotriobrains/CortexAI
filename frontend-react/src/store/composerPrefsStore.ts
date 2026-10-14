import { create } from "zustand";
import type { ModelCatalogItem, SubscriptionPlanCode } from "../types";
import { DEFAULT_ASK_MODEL_KEYS_BY_PLAN, resolveAskModelKey } from "../config/askDefaults";

export interface ComposerPrefs {
  smartForNewChats: boolean;
  smartOffModelId: string | null;
}

export const COMPOSER_PREFS_KEY = "cortex.composerPrefs.v1";
export const INITIAL_COMPOSER_PREFS: ComposerPrefs = {
  smartForNewChats: true,
  smartOffModelId: null,
};

function readStorage(key: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) ?? "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function normalizePrefs(value: unknown): ComposerPrefs {
  const prefs = value && typeof value === "object" ? (value as Partial<ComposerPrefs>) : {};
  return {
    smartForNewChats: typeof prefs.smartForNewChats === "boolean" ? prefs.smartForNewChats : true,
    smartOffModelId:
      typeof prefs.smartOffModelId === "string" && prefs.smartOffModelId
        ? prefs.smartOffModelId
        : null,
  };
}

export function loadComposerPrefs(userId: string | null): ComposerPrefs {
  const data = readStorage(COMPOSER_PREFS_KEY);
  // The unscoped prototype format belongs only to the anonymous browser user.
  return normalizePrefs(data[userId ?? "anonymous"] ?? (userId ? null : data));
}

function writeStorage(key: string, data: Record<string, unknown>) {
  try {
    localStorage.setItem(key, JSON.stringify(data));
  } catch {
    /* Storage can be disabled. */
  }
}

interface ComposerPrefsState {
  userId: string | null;
  prefs: ComposerPrefs;
  setUser: (userId: string | null) => void;
  updatePrefs: (patch: Partial<ComposerPrefs>) => void;
  reload: () => void;
}

export const useComposerPrefsStore = create<ComposerPrefsState>((set, get) => ({
  userId: null,
  prefs: loadComposerPrefs(null),
  setUser: (userId) => {
    if (get().userId !== userId) set({ userId, prefs: loadComposerPrefs(userId) });
  },
  updatePrefs: (patch) => {
    const { userId, prefs } = get();
    const next = normalizePrefs({ ...prefs, ...patch });
    writeStorage(COMPOSER_PREFS_KEY, {
      ...readStorage(COMPOSER_PREFS_KEY),
      [userId ?? "anonymous"]: next,
    });
    set({ prefs: next });
  },
  reload: () => set({ prefs: loadComposerPrefs(get().userId) }),
}));

/** Resolve against the active catalogue and server-owned plan access, retaining the saved ID. */
export function resolveSmartOffModel(
  prefs: ComposerPrefs,
  plan: { code: SubscriptionPlanCode; lockedModelKeys: readonly string[] },
  catalog: ModelCatalogItem[],
): string {
  const eligible = catalog.filter(
    (model) =>
      model.enabled &&
      model.selectable !== false &&
      !plan.lockedModelKeys.includes(`${model.provider}:${model.model}`),
  );
  if (eligible.some((model) => `${model.provider}:${model.model}` === prefs.smartOffModelId)) {
    return prefs.smartOffModelId!;
  }
  return (
    resolveAskModelKey(eligible, "", plan.code, null) || DEFAULT_ASK_MODEL_KEYS_BY_PLAN[plan.code]
  );
}

export interface ChatComposerState {
  smartMode: boolean;
  selectedModelKey: string;
  lastManualModel: string | null;
  compareModelKeys: [string, string, string];
  compareHasSent: boolean;
}

const CHAT_COMPOSER_KEY = "cortex.chatComposer.v1";
export function loadChatComposer(sessionId: string): Partial<ChatComposerState> | null {
  const key = `${useComposerPrefsStore.getState().userId ?? "anonymous"}:${sessionId}`;
  const saved = readStorage(CHAT_COMPOSER_KEY)[key];
  if (!saved || typeof saved !== "object") return null;
  const state = saved as ChatComposerState;
  if (
    typeof state.smartMode !== "boolean" ||
    typeof state.compareHasSent !== "boolean" ||
    typeof state.selectedModelKey !== "string" ||
    !(state.lastManualModel === null || typeof state.lastManualModel === "string") ||
    !Array.isArray(state.compareModelKeys) ||
    state.compareModelKeys.length !== 3 ||
    !state.compareModelKeys.every((value) => typeof value === "string")
  )
    return null;
  return {
    smartMode: state.smartMode,
    selectedModelKey: state.selectedModelKey,
    lastManualModel: state.lastManualModel,
    compareModelKeys: state.compareModelKeys,
    compareHasSent: state.compareHasSent,
  };
}

export function saveChatComposer(sessionId: string, state: ChatComposerState) {
  const key = `${useComposerPrefsStore.getState().userId ?? "anonymous"}:${sessionId}`;
  writeStorage(CHAT_COMPOSER_KEY, { ...readStorage(CHAT_COMPOSER_KEY), [key]: state });
}
