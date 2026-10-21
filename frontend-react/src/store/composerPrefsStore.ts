import { create } from "zustand";
import type { ModelCatalogItem, ReasoningLevel, SubscriptionPlanCode } from "../types";
import { DEFAULT_ASK_MODEL_KEYS_BY_PLAN, resolveAskModelKey } from "../config/askDefaults";
import { DEFAULT_COMPARE_MODEL_KEYS } from "../config/compareDefaults";

export interface ComposerPrefs {
  newChatStart: string;
  recentModels: string[];
  smartForNewChats: boolean;
  smartOffModelId: string | null;
}

export const COMPOSER_PREFS_KEY = "cortex.composerPrefs.v1";
export const INITIAL_COMPOSER_PREFS: ComposerPrefs = {
  newChatStart: "smart",
  recentModels: [],
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

function normalizePrefs(value: unknown, plan: SubscriptionPlanCode = "free"): ComposerPrefs {
  const prefs = value && typeof value === "object" ? (value as Partial<ComposerPrefs>) : {};
  const legacyModel =
    typeof prefs.smartOffModelId === "string" && prefs.smartOffModelId
      ? prefs.smartOffModelId
      : null;
  return {
    newChatStart:
      typeof prefs.newChatStart === "string" && prefs.newChatStart
        ? prefs.newChatStart
        : prefs.smartForNewChats === false
          ? (legacyModel ?? DEFAULT_ASK_MODEL_KEYS_BY_PLAN[plan])
          : "smart",
    recentModels: Array.isArray(prefs.recentModels)
      ? [
          ...new Set(
            prefs.recentModels.filter(
              (key): key is string => typeof key === "string" && key.includes(":"),
            ),
          ),
        ].slice(0, 3)
      : [],
    smartForNewChats: typeof prefs.smartForNewChats === "boolean" ? prefs.smartForNewChats : true,
    smartOffModelId: legacyModel,
  };
}

export function loadComposerPrefs(
  userId: string | null,
  plan: SubscriptionPlanCode = "free",
): ComposerPrefs {
  const data = readStorage(COMPOSER_PREFS_KEY);
  // The unscoped prototype format belongs only to the anonymous browser user.
  return normalizePrefs(data[userId ?? "anonymous"] ?? (userId ? null : data), plan);
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
  migrate: (plan: SubscriptionPlanCode, catalog: ModelCatalogItem[]) => void;
  rememberModel: (key: string) => void;
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
  migrate: (plan, catalog) => {
    const { userId, prefs } = get();
    const data = readStorage(COMPOSER_PREFS_KEY);
    const raw = data[userId ?? "anonymous"] ?? (userId ? {} : data);
    const stored = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
    const migrated =
      typeof stored.newChatStart !== "string" &&
      (typeof stored.smartForNewChats === "boolean" || typeof stored.smartOffModelId === "string")
        ? normalizePrefs(raw, plan)
        : prefs;
    const available = new Set(
      catalog
        .filter((model) => model.enabled && model.selectable !== false)
        .map((model) => `${model.provider}:${model.model}`),
    );
    const seeds = [
      DEFAULT_ASK_MODEL_KEYS_BY_PLAN[plan],
      ...DEFAULT_COMPARE_MODEL_KEYS,
      ...available,
    ];
    const recentModels = [...new Set([...migrated.recentModels, ...seeds])]
      .filter((key) => available.has(key))
      .slice(0, 3);
    if (
      typeof stored.newChatStart !== "string" ||
      recentModels.join() !== prefs.recentModels.join()
    )
      get().updatePrefs({ ...migrated, recentModels });
  },
  rememberModel: (key) =>
    get().updatePrefs({
      recentModels: [key, ...get().prefs.recentModels.filter((item) => item !== key)].slice(0, 3),
    }),
}));

export function resolveNewChatStart(
  prefs: ComposerPrefs,
  plan: { code: SubscriptionPlanCode; lockedModelKeys: readonly string[] },
  catalog: ModelCatalogItem[],
): string {
  if (prefs.newChatStart === "smart") return "smart";
  return resolveSmartOffModel({ smartOffModelId: prefs.newChatStart }, plan, catalog);
}

/** Resolve against the active catalogue and server-owned plan access, retaining the saved ID. */
export function resolveSmartOffModel(
  prefs: Pick<ComposerPrefs, "smartOffModelId">,
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
  askReasoningLevel?: ReasoningLevel;
  compareReasoningLevel?: ReasoningLevel;
  optimizeMode?: boolean;
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
    ...(["auto", "low", "medium", "high", "max"].includes(state.askReasoningLevel ?? "")
      ? { askReasoningLevel: state.askReasoningLevel }
      : {}),
    ...(["auto", "low", "medium", "high", "max"].includes(state.compareReasoningLevel ?? "")
      ? { compareReasoningLevel: state.compareReasoningLevel }
      : {}),
    ...(typeof state.optimizeMode === "boolean" ? { optimizeMode: state.optimizeMode } : {}),
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
