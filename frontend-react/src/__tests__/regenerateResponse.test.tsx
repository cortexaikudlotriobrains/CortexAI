import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import { streamCompare } from "../api/compare";
import { useChat } from "../hooks/useChat";
import { useChatStore } from "../store/chatStore";
import type {
  ChatResponse,
  ChatTurn,
  FileUploadResponse,
  StreamChunk,
} from "../types";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

vi.mock("../api/compare", () => ({
  streamCompare: vi.fn(),
}));

vi.mock("../api/history", () => ({
  fetchHistory: vi.fn().mockResolvedValue([]),
}));

describe("response regeneration", () => {
  beforeEach(() => {
    useChatStore.getState().startNewChat();
    useChatStore.setState({
      mode: "single",
      smartMode: true,
      optimizeMode: false,
      askReasoningLevel: "auto",
      compareReasoningLevel: "low",
      selectedModelKey: "openai:gpt-5.1",
      compareModelKeys: [
        "openai:gpt-5.1",
        "claude:claude-sonnet-4-5",
        "",
      ],
      prompt: "Draft in composer",
      attachments: [attachment("current-file")],
      sessionId: "session-1",
      pendingNewSession: false,
      error: null,
      history: [],
      historySearch: "",
    });
    vi.mocked(streamChat).mockReset();
    vi.mocked(streamCompare).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("regenerates an Ask response in place through chat streaming with sources enabled", async () => {
    const prior = askTurn("prior-turn", "Previous prompt", {
      text: "Previous answer",
    });
    const source = askTurn("source-turn", "Research this", {
      provider: "openai",
      model: "gpt-5.1",
      text: "Original answer",
    });
    source.researchEnabled = true;
    source.attachments = [attachment("source-file")];
    useChatStore.setState({
      turns: [prior, source],
      activeTurnId: source.id,
      responses: source.responses,
    });
    vi.mocked(streamChat).mockReturnValue(chatRegenerationStream());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.regenerate(source.id, 0);
    });

    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(streamCompare).not.toHaveBeenCalled();
    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      prompt: "Research this",
      provider: "openai",
      model: "gpt-5.1",
      generation: {
        profile: "auto",
        reasoning: { mode: "auto", effort: "auto" },
      },
      routing: { smart_mode: false, web_mode: "auto" },
      attachments: [{ file_id: "source-file" }],
      context: {
        session_id: "session-1",
        new_session: false,
        conversation_history: [
          { role: "user", content: "Previous prompt" },
          { role: "assistant", content: "Previous answer" },
        ],
      },
    });
    const state = useChatStore.getState();
    const regeneratedTurn = state.turns[state.turns.length - 1];
    expect(state.turns).toHaveLength(2);
    expect(regeneratedTurn?.id).toBe(source.id);
    expect(regeneratedTurn?.prompt).toBe("Research this");
    expect(regeneratedTurn?.researchEnabled).toBe(true);
    expect(regeneratedTurn?.responses[0]?.text).toBe("Updated sourced answer");
    expect(regeneratedTurn?.responses[0]?.web_source_items).toEqual([
      { title: "Updated source", url: "https://example.com/updated" },
    ]);
    expect(state.prompt).toBe("Draft in composer");
    expect(state.attachments).toEqual([attachment("current-file")]);
    expect(state.activeTurnId).toBe(source.id);
  });

  it("regenerates only the clicked Compare response in place through chat streaming", async () => {
    const source = compareTurn("compare-turn", "Compare this");
    source.researchEnabled = true;
    source.compareSummary = compareSummary(source.responses);
    useChatStore.setState({
      mode: "compare",
      turns: [source],
      activeTurnId: source.id,
      responses: source.responses,
    });
    vi.mocked(streamChat).mockReturnValue(
      chatRegenerationStream(
        response("compare-regenerated-claude", "claude", "claude-sonnet-4-5", {
          text: "Updated Claude answer",
          token_usage: {
            prompt_tokens: 30,
            completion_tokens: 60,
            total_tokens: 90,
          },
          estimated_cost: 0.003,
        }),
      ),
    );
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.regenerate(source.id, 1);
    });

    expect(streamCompare).not.toHaveBeenCalled();
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      prompt: "Compare this",
      provider: "claude",
      model: "claude-sonnet-4-5",
      generation: {
        profile: "auto",
        reasoning: { mode: "auto", effort: "auto" },
      },
      routing: { smart_mode: false, web_mode: "auto" },
      context: {
        session_id: "session-1",
        new_session: false,
      },
    });
    const state = useChatStore.getState();
    const regeneratedTurn = state.turns[state.turns.length - 1];
    expect(state.turns).toHaveLength(1);
    expect(regeneratedTurn?.id).toBe(source.id);
    expect(regeneratedTurn?.mode).toBe("compare");
    expect(regeneratedTurn?.responses).toHaveLength(2);
    expect(regeneratedTurn?.responses[0]?.provider).toBe("openai");
    expect(regeneratedTurn?.responses[0]?.text).toBe("Original response");
    expect(regeneratedTurn?.responses[1]?.provider).toBe("claude");
    expect(regeneratedTurn?.responses[1]?.model).toBe("claude-sonnet-4-5");
    expect(regeneratedTurn?.responses[1]?.text).toBe("Updated Claude answer");
    expect(regeneratedTurn?.compareSummary?.responses[1]?.text).toBe(
      "Updated Claude answer",
    );
    expect(regeneratedTurn?.compareSummary?.success_count).toBe(2);
    expect(regeneratedTurn?.compareSummary?.total_tokens).toBe(150);
    expect(regeneratedTurn?.compareSummary?.total_cost).toBeCloseTo(0.004);
    expect(state.activeTurnId).toBe(source.id);
  });

  it("retries a clipped Compare card with the recommended larger profile", async () => {
    const source = compareTurn("compare-clipped", "Explain this deeply");
    source.responses[1] = {
      ...source.responses[1],
      text: "Partial answer",
      completion_status: "incomplete",
      stop_cause: "token_limit",
      retry_with_more_room: { available: true, recommended_profile: "deep" },
    };
    useChatStore.setState({
      mode: "compare",
      turns: [source],
      activeTurnId: source.id,
      responses: source.responses,
    });
    vi.mocked(streamChat).mockReturnValue(
      chatRegenerationStream(
        response("compare-retry-deep", "claude", "claude-sonnet-4-5", {
          text: "Complete deep answer",
          completion_status: "complete",
        }),
      ),
    );
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.retryWithMoreRoom(source.id, 1);
    });

    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      provider: "claude",
      model: "claude-sonnet-4-5",
      generation: {
        profile: "deep",
        reasoning: { mode: "auto", effort: "auto" },
      },
      regeneration: {
        source_request_id: source.responses[1].request_id,
        retry_reason: "output_limit",
      },
    });
    expect(useChatStore.getState().turns[0]?.responses[1]?.text).toBe(
      "Complete deep answer",
    );
  });

  it("targets only the clicked model and leaves other Compare cards unchanged", async () => {
    const source = compareTurn("compare-three-turn", "Compare with three models", [
      response("compare-three-openai", "openai", "gpt-5.1"),
      response("compare-three-claude", "claude", "claude-sonnet-4-5"),
      response("compare-three-gemini", "gemini", "gemini-2.5-pro"),
    ]);
    source.researchEnabled = true;
    source.attachments = [attachment("source-file")];
    useChatStore.setState({
      mode: "compare",
      turns: [source],
      activeTurnId: source.id,
      responses: source.responses,
    });
    vi.mocked(streamChat).mockReturnValue(
      chatRegenerationStream(
        response("compare-regenerated-gemini", "gemini", "gemini-2.5-pro", {
          text: "Updated Gemini answer",
        }),
      ),
    );
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.regenerate(source.id, 2);
    });

    expect(streamCompare).not.toHaveBeenCalled();
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      prompt: "Compare with three models",
      provider: "gemini",
      model: "gemini-2.5-pro",
      generation: {
        profile: "auto",
        reasoning: { mode: "auto", effort: "auto" },
      },
      routing: { smart_mode: false, web_mode: "auto" },
      attachments: [{ file_id: "source-file" }],
    });
    const state = useChatStore.getState();
    const regeneratedTurn = state.turns[state.turns.length - 1];
    expect(state.turns).toHaveLength(1);
    expect(regeneratedTurn?.id).toBe(source.id);
    expect(regeneratedTurn?.mode).toBe("compare");
    expect(regeneratedTurn?.responses).toHaveLength(3);
    expect(regeneratedTurn?.responses[0]?.text).toBe("Original response");
    expect(regeneratedTurn?.responses[1]?.text).toBe("Original response");
    expect(regeneratedTurn?.responses[2]?.provider).toBe("gemini");
    expect(regeneratedTurn?.responses[2]?.text).toBe("Updated Gemini answer");
  });

  it("does not send a one-target Compare request for malformed restored Compare turns", async () => {
    const source = compareTurn("malformed-compare-turn", "Compare restored data", [
      response("malformed-openai", "openai", "gpt-5.1"),
      response("malformed-placeholder", "smart", "Selecting best model"),
    ]);
    source.researchEnabled = true;
    useChatStore.setState({
      mode: "compare",
      turns: [source],
      activeTurnId: source.id,
      responses: source.responses,
    });
    vi.mocked(streamChat).mockReturnValue(chatRegenerationStream());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.regenerate(source.id, 0);
    });

    const previousTurnCount = useChatStore.getState().turns.length;
    expect(streamCompare).not.toHaveBeenCalled();
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      prompt: "Compare restored data",
      provider: "openai",
      model: "gpt-5.1",
      routing: { smart_mode: false, web_mode: "auto" },
      context: {
        session_id: "session-1",
        new_session: false,
      },
    });
    const state = useChatStore.getState();
    expect(state.turns).toHaveLength(previousTurnCount);
    expect(state.turns[0]?.id).toBe(source.id);
    expect(state.turns[0]?.mode).toBe("compare");
    expect(state.turns[0]?.responses).toHaveLength(2);
    expect(state.turns[0]?.responses[0]?.text).toBe("Updated sourced answer");
    expect(state.turns[0]?.responses[1]?.text).toBe("Original response");
  });
});

async function* chatRegenerationStream(
  streamResponse: ChatResponse = response("chat-regenerated", "openai", "gpt-5.1", {
    text: "Updated sourced answer",
    web_source_items: [
      { title: "Updated source", url: "https://example.com/updated" },
    ],
  }),
): AsyncGenerator<StreamChunk> {
  yield {
    type: "start",
    provider: streamResponse.provider,
    model: streamResponse.model,
    session_id: "session-1",
  };
  yield { type: "delta", text: streamResponse.text };
  yield {
    type: "metadata",
    metadata: streamResponse,
  };
  yield { type: "done", session_id: "session-1" };
}

function askTurn(
  id: string,
  prompt: string,
  responseOverrides: Partial<ChatResponse> = {},
): ChatTurn {
  const responses = [response(`${id}-response`, "openai", "gpt-5.1", responseOverrides)];
  return {
    id,
    mode: "single",
    prompt,
    submittedPrompt: prompt,
    attachments: [],
    responses,
    status: "complete",
    createdAt: "2026-06-21T00:00:00.000Z",
  };
}

function compareTurn(
  id: string,
  prompt: string,
  responses: ChatResponse[] = [
    response(`${id}-openai`, "openai", "gpt-5.1"),
    response(`${id}-claude`, "claude", "claude-sonnet-4-5"),
  ],
): ChatTurn {
  return {
    id,
    mode: "compare",
    prompt,
    submittedPrompt: prompt,
    attachments: [],
    responses,
    status: "complete",
    createdAt: "2026-06-21T00:00:00.000Z",
    requestGroupId: `${id}-group`,
  };
}

function compareSummary(responses: ChatResponse[]) {
  return {
    request_group_id: "compare-group",
    session_id: "session-1",
    responses,
    success_count: responses.length,
    error_count: 0,
    total_tokens: responses.reduce(
      (total, item) => total + (item.token_usage?.total_tokens ?? 0),
      0,
    ),
    total_cost: responses.reduce((total, item) => total + item.estimated_cost, 0),
    timestamp: "2026-06-21T00:00:00.000Z",
  };
}

function response(
  requestId: string,
  provider: string,
  model: string,
  overrides: Partial<ChatResponse> = {},
): ChatResponse {
  return {
    request_id: requestId,
    session_id: "session-1",
    text: "Original response",
    provider,
    model,
    latency_ms: 320,
    token_usage: {
      prompt_tokens: 20,
      completion_tokens: 40,
      total_tokens: 60,
    },
    estimated_cost: 0.001,
    cost_currency: "USD",
    web_source_items: [],
    timestamp: "2026-06-21T00:00:00.000Z",
    ui_status: "complete",
    ...overrides,
  };
}

function attachment(fileId: string): FileUploadResponse {
  return {
    file_id: fileId,
    original_filename: `${fileId}.txt`,
    mime_type: "text/plain",
    size_bytes: 10,
    status: "ready",
    ingestion_meta: {},
    created_at: "2026-06-21T00:00:00.000Z",
    deduplicated: false,
  };
}
