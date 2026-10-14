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

  it("persists separately for each user and reloads across browser tabs", () => {
    const store = useComposerPrefsStore.getState();
    store.setUser("alice");
    store.updatePrefs({ smartForNewChats: false, smartOffModelId: flash });
    store.setUser("bob");
    expect(useComposerPrefsStore.getState().prefs).toEqual(INITIAL_COMPOSER_PREFS);
    store.updatePrefs({ smartOffModelId: sonnet });
    store.setUser("alice");
    expect(useComposerPrefsStore.getState().prefs).toEqual({
      smartForNewChats: false,
      smartOffModelId: flash,
    });
    localStorage.setItem(
      COMPOSER_PREFS_KEY,
      JSON.stringify({ alice: { smartForNewChats: true, smartOffModelId: luna } }),
    );
    store.reload();
    expect(useComposerPrefsStore.getState().prefs.smartOffModelId).toBe(luna);
  });

  it.each(["free", "plus", "pro"] as const)(
    "falls back to the %s plan model for locked or unavailable saved models",
    (plan) => {
      const prefs = { smartForNewChats: true, smartOffModelId: flash };
      const expected = {
        free: luna,
        plus: "claude:claude-sonnet-4-6",
        pro: "openai:gpt-5.6-terra",
      }[plan];
      expect(
        resolveSmartOffModel(prefs, { code: plan, lockedModelKeys: [flash] }, DEFAULT_MODELS),
      ).toBe(expected);
      expect(
        resolveSmartOffModel(
          prefs,
          { code: plan, lockedModelKeys: [] },
          DEFAULT_MODELS.filter((model) => `${model.provider}:${model.model}` !== flash),
        ),
      ).toBe(expected);
      expect(
        resolveSmartOffModel(
          prefs,
          { code: plan, lockedModelKeys: [] },
          DEFAULT_MODELS.map((model) =>
            `${model.provider}:${model.model}` === flash ? { ...model, enabled: false } : model,
          ),
        ),
      ).toBe(expected);
      expect(prefs.smartOffModelId).toBe(flash);
    },
  );

  it("keeps the current Ask chat when preferences change independently of its menu", async () => {
    render(<PromptComposer models={DEFAULT_MODELS} />);
    await waitFor(() => expect(useChatStore.getState().selectedModelKey).toBe(luna));
    act(() =>
      useComposerPrefsStore
        .getState()
        .updatePrefs({ smartForNewChats: false, smartOffModelId: flash }),
    );
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().selectedModelKey).toBe(luna);
    act(() => useChatStore.getState().startNewChat());
    await waitFor(() => expect(useChatStore.getState().selectedModelKey).toBe(flash));
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().askReasoningLevel).toBe("low");
  });

  it("applies the menu Smart switch immediately and saves it for subsequent chats", async () => {
    const user = userEvent.setup();
    useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: flash });
    render(<PromptComposer models={DEFAULT_MODELS} />);
    await user.click(screen.getByRole("button", { name: "More options" }));
    const preferenceSwitch = screen.getByRole("switch", { name: /Smart in new chats/ });
    await user.click(preferenceSwitch);
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().selectedModelKey).toBe(flash);
    expect(useChatStore.getState().askReasoningLevel).toBe("low");
    expect(screen.getByRole("switch", { name: "Smart routing" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(preferenceSwitch).toHaveTextContent("New chats start with Smart on");
    expect(preferenceSwitch).not.toHaveTextContent("DeepSeek");
    expect(loadComposerPrefs(null).smartForNewChats).toBe(false);
    await user.click(preferenceSwitch);
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().askReasoningLevel).toBe("auto");
    expect(loadComposerPrefs(null).smartForNewChats).toBe(true);
    await user.click(preferenceSwitch);
    act(() => useChatStore.getState().startNewChat());
    expect(useChatStore.getState().smartMode).toBe(false);
  });

  it("keeps both Smart controls synchronized and saves toggles made from the composer", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    const smart = screen.getByRole("switch", { name: "Smart routing" });
    await user.click(smart);
    expect(loadComposerPrefs(null).smartForNewChats).toBe(false);
    await user.click(screen.getByRole("button", { name: "More options" }));
    const menuSmart = screen.getByRole("switch", { name: /Smart in new chats/ });
    expect(menuSmart).toHaveAttribute("aria-checked", "false");
    await user.click(menuSmart);
    expect(smart).toHaveAttribute("aria-checked", "true");
    expect(menuSmart).toHaveAttribute("aria-checked", "true");
    expect(loadComposerPrefs(null).smartForNewChats).toBe(true);
    await user.keyboard("{Escape}");
    await user.click(smart);
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.getByRole("switch", { name: /Smart in new chats/ })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(loadComposerPrefs(null).smartForNewChats).toBe(false);
    act(() => useChatStore.getState().startNewChat());
    expect(smart).toHaveAttribute("aria-checked", "false");
  });

  it("shows the current chat's Smart status in the menu even when its saved new-chat preference differs", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    act(() => useComposerPrefsStore.getState().updatePrefs({ smartForNewChats: false }));
    await user.click(screen.getByRole("button", { name: "More options" }));
    const menuSmart = screen.getByRole("switch", { name: /Smart in new chats/ });
    expect(menuSmart).toHaveAttribute("aria-checked", "true");
    expect(loadComposerPrefs(null).smartForNewChats).toBe(false);
    await user.click(menuSmart);
    expect(screen.getByRole("switch", { name: "Smart routing" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(menuSmart).toHaveAttribute("aria-checked", "false");
  });

  it("updates an existing session immediately while preserving its last manual choice and transcript", async () => {
    const user = userEvent.setup();
    const thread = buildHistoryThreads([
      {
        id: 1,
        session_id: "existing-menu-toggle",
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
    useChatStore.getState().hydrateFromHistoryThread(thread);
    useChatStore.getState().setSelectedModelKey(sonnet);
    useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: flash });
    const originalTurns = useChatStore.getState().turns;
    render(<PromptComposer models={DEFAULT_MODELS} />);
    await user.click(screen.getByRole("button", { name: "More options" }));
    await user.click(screen.getByRole("switch", { name: /Smart in new chats/ }));
    expect(useChatStore.getState().smartMode).toBe(false);
    expect(useChatStore.getState().selectedModelKey).toBe(sonnet);
    expect(useChatStore.getState().turns).toBe(originalTurns);
    expect(useChatStore.getState().sessionId).toBe("existing-menu-toggle");
    expect(loadChatComposer("existing-menu-toggle")?.smartMode).toBe(false);
    await user.click(screen.getByRole("switch", { name: /Smart in new chats/ }));
    expect(useChatStore.getState().smartMode).toBe(true);
    expect(useChatStore.getState().lastManualModel).toBe(sonnet);
    expect(loadChatComposer("existing-menu-toggle")?.smartMode).toBe(true);
  });

  it("uses the preference when switching Smart off and preserves a chat-scoped manual choice", async () => {
    const user = userEvent.setup();
    render(<PromptComposer models={DEFAULT_MODELS} />);
    act(() => useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: flash }));
    await user.click(screen.getByRole("switch", { name: "Smart routing" }));
    expect(useChatStore.getState().selectedModelKey).toBe(flash);
    act(() => useChatStore.getState().setSelectedModelKey(sonnet));
    await user.click(screen.getByRole("switch", { name: "Smart routing" }));
    act(() => useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: luna }));
    await user.click(screen.getByRole("switch", { name: "Smart routing" }));
    expect(useChatStore.getState().selectedModelKey).toBe(sonnet);
    expect(useComposerPrefsStore.getState().prefs.smartOffModelId).toBe(luna);
  });

  it("initializes Compare with distinct models and follows changes only before its first send", async () => {
    expect(newCompareModelKeys(DEFAULT_MODELS, sonnet)).toEqual([sonnet, luna, ""]);
    const view = render(<PromptComposer models={DEFAULT_MODELS} />);
    act(() => {
      useChatStore.getState().setMode("compare");
      useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: flash });
    });
    await waitFor(() => expect(useChatStore.getState().compareModelKeys[0]).toBe(flash));
    act(() =>
      useChatStore.getState().beginTurn({
        mode: "compare",
        prompt: "First send",
        submittedPrompt: "First send",
        attachments: [],
        responses: [],
      }),
    );
    act(() => useComposerPrefsStore.getState().updatePrefs({ smartOffModelId: sonnet }));
    expect(useChatStore.getState().compareModelKeys[0]).toBe(flash);
    view.rerender(
      <PromptComposer
        models={DEFAULT_MODELS.filter((model) => `${model.provider}:${model.model}` !== flash)}
      />,
    );
    expect(useChatStore.getState().compareModelKeys[0]).toBe(flash);
    act(() => useChatStore.getState().startNewChat());
    await waitFor(() =>
      expect(useChatStore.getState().compareModelKeys).toEqual([sonnet, luna, ""]),
    );
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
