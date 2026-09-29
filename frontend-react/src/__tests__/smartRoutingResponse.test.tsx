import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamChat } from "../api/chat";
import { useChat } from "../hooks/useChat";
import { useChatStore } from "../store/chatStore";
import type { StreamChunk } from "../types";

vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
}));

vi.mock("../api/history", () => ({
  fetchHistory: vi.fn().mockResolvedValue([]),
}));

describe("Smart routing response identity", () => {
  beforeEach(() => {
    useChatStore.setState({
      mode: "single",
      smartMode: true,
      optimizeMode: false,
      selectedModelKey: "openai:gpt-5.1",
      compareModelKeys: [
        "openai:gpt-5.1",
        "claude:claude-sonnet-4-5",
        "",
      ],
      prompt: "Explain the tradeoffs",
      attachments: [],
      turns: [],
      activeTurnId: null,
      responses: [],
      streaming: false,
      streamingText: "",
      error: null,
      history: [],
      historySearch: "",
      sessionId: null,
      pendingNewSession: true,
    });
    vi.mocked(streamChat).mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps Smart routing neutral until final response metadata identifies the model", async () => {
    let finishStream: (() => void) | undefined;
    vi.mocked(streamChat).mockReturnValue(
      controlledStream(
        {
          type: "start",
          provider: "claude",
          model: "claude-sonnet-4-5",
        },
        (finish) => {
          finishStream = finish;
        },
      ),
    );
    const { result } = renderHook(() => useChat());

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.submit();
    });
    expect(useChatStore.getState().prompt).toBe("");

    await waitFor(() => {
      const response = useChatStore.getState().turns[0]?.responses[0];
      expect(response?.provider).toBe("smart");
      expect(response?.model).toBe("Selecting best model");
      expect(response?.text).toBe("");
    });

    finishStream?.();
    await act(async () => {
      await submission;
    });

    const response = useChatStore.getState().turns[0]?.responses[0];
    expect(response?.provider).toBe("openai");
    expect(response?.model).toBe("gpt-5.4-mini");
    expect(response?.text).toBe("Final answer");
  });
});

async function* controlledStream(
  start: StreamChunk,
  exposeFinish: (finish: () => void) => void,
): AsyncGenerator<StreamChunk> {
  yield start;
  await new Promise<void>((resolve) => exposeFinish(resolve));
  yield {
    type: "metadata",
    metadata: {
      request_id: "request-1",
      session_id: "session-1",
      text: "Final answer",
      provider: "openai",
      model: "gpt-5.4-mini",
      latency_ms: 120,
      token_usage: {
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
      },
      estimated_cost: 0.001,
      cost_currency: "USD",
      web_source_items: [],
      timestamp: "2026-06-14T00:00:00.000Z",
    },
  };
  yield { type: "done", session_id: "session-1" };
}
