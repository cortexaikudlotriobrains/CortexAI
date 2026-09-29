import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import { streamCompare } from "../api/compare";
import { optimizePrompt } from "../api/optimize";
import { useChat } from "../hooks/useChat";
import { useChatStore } from "../store/chatStore";
import type { ChatResponse, ChatTurn, FileUploadResponse, StreamChunk } from "../types";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

vi.mock("../api/compare", () => ({
  streamCompare: vi.fn(),
}));

vi.mock("../api/optimize", () => ({
  optimizePrompt: vi.fn(),
}));

vi.mock("../api/history", () => ({
  fetchHistory: vi.fn().mockResolvedValue([]),
}));

describe("suggested follow-up submission", () => {
  beforeEach(() => {
    useChatStore.getState().startNewChat();
    useChatStore.setState({
      mode: "single",
      smartMode: true,
      optimizeMode: true,
      selectedModelKey: "openai:gpt-5.1",
      compareModelKeys: [
        "openai:gpt-5.1",
        "claude:claude-sonnet-4-5",
        "",
      ],
      prompt: "Draft in composer",
      attachments: [attachment()],
      turns: [
        askTurn("prior-turn", "Is jal jeera good for liver health?", {
          text: "It may support digestion. If you want, I can also give you:\n- Evidence summary",
        }),
      ],
      activeTurnId: "prior-turn",
      responses: [],
      streaming: false,
      streamingText: "",
      error: null,
      history: [],
      historySearch: "",
      sessionId: "session-1",
      pendingNewSession: false,
    });
    vi.mocked(streamChat).mockReset();
    vi.mocked(streamCompare).mockReset();
    vi.mocked(optimizePrompt).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("sends chip text as a new Ask turn without optimizing or clearing the composer", async () => {
    vi.mocked(streamChat).mockReturnValue(chatStream());
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.submitFollowUp("Evidence summary");
    });

    expect(optimizePrompt).not.toHaveBeenCalled();
    expect(streamCompare).not.toHaveBeenCalled();
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(vi.mocked(streamChat).mock.calls[0]?.[0]).toMatchObject({
      prompt: "Evidence summary",
      routing: { smart_mode: true, web_mode: "auto" },
      attachments: undefined,
      context: {
        session_id: "session-1",
        new_session: false,
        conversation_history: [
          { role: "user", content: "Is jal jeera good for liver health?" },
          {
            role: "assistant",
            content:
              "It may support digestion. If you want, I can also give you:\n- Evidence summary",
          },
        ],
      },
    });

    const state = useChatStore.getState();
    const latest = state.turns[state.turns.length - 1];
    expect(latest?.prompt).toBe("Evidence summary");
    expect(latest?.submittedPrompt).toBe("Evidence summary");
    expect(latest?.optimization).toBeUndefined();
    expect(latest?.responses[0]?.text).toBe("Follow-up answer");
    expect(state.prompt).toBe("Draft in composer");
    expect(state.attachments).toEqual([attachment()]);
  });
});

async function* chatStream(): AsyncGenerator<StreamChunk> {
  yield { type: "start", provider: "openai", model: "gpt-5.1" };
  yield { type: "delta", text: "Follow-up answer" };
  yield {
    type: "metadata",
    metadata: response("follow-up-response", {
      text: "Follow-up answer",
    }),
  };
  yield { type: "done", session_id: "session-1" };
}

function askTurn(
  id: string,
  prompt: string,
  responseOverrides: Partial<ChatResponse> = {},
): ChatTurn {
  return {
    id,
    mode: "single",
    prompt,
    submittedPrompt: prompt,
    attachments: [],
    responses: [response(`${id}-response`, responseOverrides)],
    status: "complete",
    createdAt: "2026-07-11T00:00:00.000Z",
  };
}

function response(
  requestId: string,
  overrides: Partial<ChatResponse> = {},
): ChatResponse {
  return {
    request_id: requestId,
    session_id: "session-1",
    text: "Prior answer",
    provider: "openai",
    model: "gpt-5.1",
    latency_ms: 120,
    token_usage: {
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
    },
    estimated_cost: 0.001,
    cost_currency: "USD",
    web_source_items: [],
    timestamp: "2026-07-11T00:00:00.000Z",
    ui_status: "complete",
    ...overrides,
  };
}

function attachment(): FileUploadResponse {
  return {
    file_id: "file-1",
    original_filename: "draft.txt",
    mime_type: "text/plain",
    size_bytes: 10,
    status: "ready",
    ingestion_meta: {},
    created_at: "2026-07-11T00:00:00.000Z",
    deduplicated: false,
  };
}
