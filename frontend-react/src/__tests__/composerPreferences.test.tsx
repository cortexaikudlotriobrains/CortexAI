import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PromptComposer } from "../components/composer/PromptComposer";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { newCompareModelKeys } from "../config/compareDefaults";
import { useChatStore } from "../store/chatStore";
import {
  COMPOSER_PREFS_KEY,
  INITIAL_COMPOSER_PREFS,
  loadComposerPrefs,
  resolveSmartOffModel,
  resolveNewChatStart,
  loadChatComposer,
  saveChatComposer,
  useComposerPrefsStore,
} from "../store/composerPrefsStore";
import { buildHistoryThreads } from "../history/historyThreads";

vi.mock("../hooks/useChat", () => ({ useChat: () => ({ submit: vi.fn(), cancel: vi.fn() }) }));
vi.mock("../api/files", () => ({ deleteFile: vi.fn().mockResolvedValue(undefined) }));

const luna = "openai:gpt-5.6-luna";
const sonnet = "claude:claude-sonnet-5";
const flash = "deepseek:deepseek-v4-flash";

beforeEach(() => {
  localStorage.clear();
  useComposerPrefsStore.setState({ userId: null, prefs: { ...INITIAL_COMPOSER_PREFS } });
  useChatStore.getState().startNewChat();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("composer preferences", () => {
  it("uses safe initial values for missing and malformed storage", () => {
    expect(loadComposerPrefs(null)).toEqual(INITIAL_COMPOSER_PREFS);
    localStorage.setItem(COMPOSER_PREFS_KEY, "broken");
    expect(loadComposerPrefs("alice")).toEqual(INITIAL_COMPOSER_PREFS);
    localStorage.setItem(
      COMPOSER_PREFS_KEY,
      JSON.stringify({ alice: { smartForNewChats: "false", smartOffModelId: 42 } }),
    );
    expect(loadComposerPrefs("alice")).toEqual(INITIAL_COMPOSER_PREFS);
  });

  it("migrates legacy preferences using the actual plan default", () => {
    localStorage.setItem(
      COMPOSER_PREFS_KEY,
      JSON.stringify({ alice: { smartForNewChats: false, smartOffModelId: 42 } }),
    );
    expect(loadComposerPrefs("alice", "plus").newChatStart).toBe("claude:claude-sonnet-4-6");
    localStorage.setItem(
      COMPOSER_PREFS_KEY,
      JSON.stringify({ alice: { smartForNewChats: false, smartOffModelId: null } }),
    );
    expect(loadComposerPrefs("alice", "plus").newChatStart).toBe("claude:claude-sonnet-4-6");
    expect(loadComposerPrefs("bob").newChatStart).toBe("smart");
    useComposerPrefsStore.getState().setUser("alice");
    useComposerPrefsStore.getState().migrate("pro", DEFAULT_MODELS);
    expect(useComposerPrefsStore.getState().prefs.newChatStart).toBe("openai:gpt-5.6-terra");
  });
  it("persists separately per user, de-duplicates and caps recents", () => {
    const store = useComposerPrefsStore.getState();
    store.setUser("alice");
    store.updatePrefs({ newChatStart: flash });
    [luna, sonnet, flash, luna].forEach(store.rememberModel);
    expect(loadComposerPrefs("alice").recentModels).toEqual([luna, flash, sonnet]);
    store.setUser("bob");
    expect(useComposerPrefsStore.getState().prefs.newChatStart).toBe("smart");
    store.setUser("alice");
    expect(useComposerPrefsStore.getState().prefs.newChatStart).toBe(flash);
  });
  it.each(["free", "plus", "pro"] as const)(
    "falls back on %s without deleting a locked preference",
    (plan) => {
      const prefs = { ...INITIAL_COMPOSER_PREFS, newChatStart: flash };
      const expected = {
        free: luna,
        plus: "claude:claude-sonnet-4-6",
        pro: "openai:gpt-5.6-terra",
      }[plan];
      expect(
        resolveNewChatStart(prefs, { code: plan, lockedModelKeys: [flash] }, DEFAULT_MODELS),
      ).toBe(expected);
      expect(
        resolveNewChatStart(
          prefs,
          { code: plan, lockedModelKeys: [] },
          DEFAULT_MODELS.filter((model) => `${model.provider}:${model.model}` !== flash),
        ),
      ).toBe(expected);
      expect(
        resolveSmartOffModel(
          { smartOffModelId: flash },
          { code: plan, lockedModelKeys: [flash] },
          DEFAULT_MODELS,
        ),
      ).toBe(expected);
      expect(prefs.newChatStart).toBe(flash);
    },
  );
  it("saves a new-chat preference without changing either current selection", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    const compare = useChatStore.getState().compareModelKeys;
    await user.click(screen.getByRole("button", { name: "Model: Smart" }));
    await user.click(screen.getByRole("button", { name: /New chats start with/ }));
    await user.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(loadComposerPrefs(null).newChatStart).toBe(flash);
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().compareModelKeys).toEqual(compare);
    await user.keyboard("{Escape}");
    act(() => useChatStore.getState().startNewChat());
    await waitFor(() => expect(useChatStore.getState().selectedModelKey).toBe(flash));
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().compareModelKeys[0]).toBe(flash);
  });
  it("chooses from the complete catalog, updates recents and focuses the textarea", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    await user.click(screen.getByRole("button", { name: "Model: Smart" }));
    await user.click(screen.getByRole("button", { name: "All models" }));
    expect(screen.getAllByRole("option")).toHaveLength(
      DEFAULT_MODELS.filter((model) => model.enabled && model.selectable !== false).length,
    );
    await user.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(useChatStore.getState().selectedModelKey).toBe(flash);
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(loadComposerPrefs(null).recentModels[0]).toBe(flash);
    expect(loadComposerPrefs(null).newChatStart).toBe("smart");
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveFocus());
  });
  it("steps back from drill-ins before closing and resetting the view", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    const trigger = screen.getByRole("button", { name: "Model: Smart" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "All models" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: "All models" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    await user.click(trigger);
    expect(screen.getByRole("button", { name: "All models" })).toBeVisible();
  });
  it("does not follow cross-tab preference updates in the current chat", async () => {
    render(<PromptComposer models={DEFAULT_MODELS} />);
    const compare = useChatStore.getState().compareModelKeys;
    act(() => useComposerPrefsStore.getState().updatePrefs({ newChatStart: flash }));
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().compareModelKeys).toEqual(compare);
    act(() => useChatStore.getState().startNewChat());
    await waitFor(() => expect(useChatStore.getState().selectedModelKey).toBe(flash));
    expect(newCompareModelKeys(DEFAULT_MODELS, sonnet)).toEqual([sonnet, luna, ""]);
  });

  it("restores chat-scoped reasoning and Improve and resets them for a new chat", () => {
    useChatStore.setState({ composerInitialized: true });
    useChatStore.getState().setSessionId("message-options");
    useChatStore.getState().setAskReasoningLevel("high");
    useChatStore.getState().setCompareReasoningLevel("max");
    useChatStore.getState().setOptimizeMode(true);
    expect(loadChatComposer("message-options")).toMatchObject({
      askReasoningLevel: "high",
      compareReasoningLevel: "max",
      optimizeMode: true,
    });
    useChatStore.getState().startNewChat();
    expect(useChatStore.getState()).toMatchObject({
      askReasoningLevel: "auto",
      compareReasoningLevel: "auto",
      optimizeMode: false,
    });
  });

  it("restores an existing chat's Smart and last manual model after preference changes", () => {
    const thread = buildHistoryThreads([
      {
        id: 1,
        session_id: "existing",
        timestamp: "2026-10-14T00:00:00Z",
        mode: "single",
        prompt: "Question",
        provider: "openai",
        model: "gpt-5.6-luna",
        routing_mode: "smart",
        response: "Answer",
        web_source_items: [],
      },
    ])[0];
    useChatStore.setState({ composerInitialized: true });
    useChatStore.getState().setSessionId("existing");
    useChatStore.getState().setSelectedModelKey(sonnet);
    useChatStore.getState().setSmartMode(false);
    useComposerPrefsStore
      .getState()
      .updatePrefs({ smartForNewChats: true, smartOffModelId: flash });
    useChatStore.getState().startNewChat();
    useChatStore.getState().hydrateFromHistoryThread(thread);
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().lastManualModel).toBe(sonnet);
    expect(useChatStore.getState().selectedModelKey).toBe(sonnet);
  });

  it("derives restored state from history when this device has no chat snapshot", () => {
    const thread = buildHistoryThreads([
      {
        id: 1,
        session_id: "from-server",
        timestamp: "2026-10-14T00:00:00Z",
        mode: "single",
        prompt: "Question",
        provider: "deepseek",
        model: "deepseek-v4-flash",
        routing_mode: "manual",
        response: "Answer",
        web_source_items: [],
      },
    ])[0];
    useChatStore.getState().hydrateFromHistoryThread(thread);
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().lastManualModel).toBe(flash);
  });

  it("keeps legacy history with no routing metadata in Smart mode", () => {
    const thread = buildHistoryThreads([
      {
        id: 1,
        session_id: "legacy",
        timestamp: "2026-10-14T00:00:00Z",
        mode: "single",
        prompt: "Question",
        provider: "openai",
        model: "gpt-5.6-luna",
        response: "Answer",
        web_source_items: [],
      },
    ])[0];
    useChatStore.getState().hydrateFromHistoryThread(thread);
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().lastManualModel).toBeNull();
  });

  it("keeps the saved chat snapshot intact while reload waits for history", () => {
    const saved = {
      smartMode: false,
      selectedModelKey: flash,
      lastManualModel: flash,
      compareModelKeys: [luna, sonnet, ""] as [string, string, string],
      compareHasSent: true,
    };
    saveChatComposer("waiting-for-history", saved);
    useChatStore.getState().setSessionId("waiting-for-history");
    render(<PromptComposer models={DEFAULT_MODELS} />);
    expect(useChatStore.getState().composerInitialized).toBe(false);
    expect(loadChatComposer("waiting-for-history")).toEqual(saved);
  });

  it("restores Compare's requested identities instead of provider served snapshots", () => {
    const thread = buildHistoryThreads([
      {
        id: 1,
        session_id: "compare-restored",
        request_group_id: "group",
        timestamp: "2026-10-14T00:00:00Z",
        mode: "compare",
        prompt: "Question",
        provider: "openai",
        model: "gpt-5.6-luna-2026-10-01",
        requested_model: "gpt-5.6-luna",
        response: "Answer",
        web_source_items: [],
      },
      {
        id: 2,
        session_id: "compare-restored",
        request_group_id: "group",
        timestamp: "2026-10-14T00:00:00Z",
        mode: "compare",
        prompt: "Question",
        provider: "claude",
        model: "claude-sonnet-5-2026-10-01",
        requested_model: "claude-sonnet-5",
        response: "Answer",
        web_source_items: [],
      },
    ])[0];
    useChatStore.getState().hydrateFromHistoryThread(thread);
    expect(useChatStore.getState().compareModelKeys).toEqual([luna, sonnet, ""]);
    expect(useChatStore.getState().compareHasSent).toBe(true);
  });
});
