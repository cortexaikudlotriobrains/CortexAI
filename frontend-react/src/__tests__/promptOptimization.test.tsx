import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FeatureChips } from "../components/composer/FeatureChips";
import { ResultsSection } from "../components/results/ResultsSection";
import { useChat } from "../hooks/useChat";
import { buildConversationHistory } from "../hooks/useChat";
import {
  buildOptimizeRequest,
  OPTIMIZATION_ORIGINAL_NOTE,
  resolveOptimizationFailure,
  resolveOptimizationResponse,
} from "../optimization/promptOptimization";
import { useChatStore } from "../store/chatStore";
import type {
  ChatTurn,
  ConversationHistoryItem,
  FileUploadResponse,
  OptimizeResponse,
} from "../types";

describe("prompt optimization", () => {
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
      prompt: "",
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
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("sends no context for a first prompt even when it contains a reference word", () => {
    expect(
      buildOptimizeRequest({
        prompt: "Make this clearer",
        conversationHistory: [],
        context: { new_session: true },
        attachments: [],
      }),
    ).toEqual({ prompt: "Make this clearer" });
  });

  it("carries the shared credit activity id into Prompt Optimizer", () => {
    expect(
      buildOptimizeRequest({
        prompt: "Make this clearer",
        conversationHistory: [],
        context: { new_session: true },
        attachments: [],
        creditActivityId: "activity-one",
      }),
    ).toEqual({
      prompt: "Make this clearer",
      credit_activity_id: "activity-one",
    });
  });

  it("uses one credit activity id and the original question across optimization and Ask", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/v1/optimize") {
        return new Response(
          JSON.stringify({
            original_prompt: "rough prompt",
            optimized_prompt: "Clear, specific prompt",
            was_optimized: true,
            server_optimization_enabled: true,
            optimization_status: "optimized",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (path === "/v1/chat/stream") {
        return new Response(
          [
            JSON.stringify({ type: "start", provider: "openai", model: "gpt-5.1" }),
            JSON.stringify({
              type: "response_done",
              response: {
                request_id: "response-1",
                text: "Answer",
                provider: "openai",
                model: "gpt-5.1",
                latency_ms: 10,
                token_usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
                estimated_cost: 0,
                finish_reason: "stop",
                error: null,
                metadata: {},
              },
            }),
            JSON.stringify({ type: "done", session_id: "session-1" }),
          ].join("\n"),
          { status: 200, headers: { "Content-Type": "application/x-ndjson" } },
        );
      }
      if (path.startsWith("/v1/history")) {
        return new Response("[]", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`Unexpected request: ${path} ${init?.method ?? "GET"}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    useChatStore.setState({ optimizeMode: true, prompt: "rough prompt" });

    render(<ChatActions />);
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input) === "/v1/history?limit=500")).toBe(
        true,
      );
    });
    const optimizeCall = fetchMock.mock.calls.find(([input]) => String(input) === "/v1/optimize");
    const chatCall = fetchMock.mock.calls.find(([input]) => String(input) === "/v1/chat/stream");
    const optimizeBody = JSON.parse(String(optimizeCall?.[1]?.body)) as Record<string, unknown>;
    const chatBody = JSON.parse(String(chatCall?.[1]?.body)) as Record<string, unknown>;

    expect(optimizeBody.credit_activity_id).toBeTruthy();
    expect(chatBody.credit_activity_id).toBe(optimizeBody.credit_activity_id);
    expect(chatBody.initial_query).toBe("rough prompt");
    expect(chatBody.prompt).toBe("Clear, specific prompt");
  });

  it("sends bounded context whenever a thread has history", () => {
    const conversationHistory: ConversationHistoryItem[] = Array.from(
      { length: 12 },
      (_, index) => ({
        role: index % 2 === 0 ? "user" : "assistant",
        content: `${index} ${"detail ".repeat(100)}`,
      }),
    );

    const request = buildOptimizeRequest({
      prompt: "I was talking about mining the asteroids",
      conversationHistory,
      context: { session_id: "session-1", new_session: false },
      attachments: [],
    });

    expect(request.context?.session_id).toBe("session-1");
    expect(request.context?.conversation_history).toHaveLength(10);
    expect(
      request.context?.conversation_history?.every((message) => message.content.length <= 500),
    ).toBe(true);
    expect(request.context_hint?.length).toBeLessThanOrEqual(4000);
  });

  it("includes context for natural follow-ups that do not match the old reference classifier", () => {
    const request = buildOptimizeRequest({
      prompt: "Write it as a table",
      conversationHistory: [
        { role: "user", content: "Compare OpenAI and Claude for legal review." },
        {
          role: "assistant",
          content: "OpenAI is faster. Claude is more cautious with nuanced text.",
        },
      ],
      context: { session_id: "session-1", new_session: false },
      attachments: [],
    });

    expect(request.context?.conversation_history).toEqual([
      { role: "user", content: "Compare OpenAI and Claude for legal review." },
      {
        role: "assistant",
        content: "OpenAI is faster. Claude is more cautious with nuanced text.",
      },
    ]);
    expect(request.context_hint).toContain("Compare OpenAI and Claude");
  });

  it("keeps attachment contents out while still sending prior chat context", () => {
    const request = buildOptimizeRequest({
      prompt: "Summarize that",
      conversationHistory: [{ role: "assistant", content: "Previous answer" }],
      context: { session_id: "session-1" },
      attachments: [attachment()],
    });

    expect(request.context?.conversation_history).toEqual([
      { role: "assistant", content: "Previous answer" },
    ]);
    expect(request.context_hint).toContain("attached file contents are not included");
    expect(JSON.stringify(request)).not.toContain("report.pdf");
    expect(JSON.stringify(request)).not.toContain("file-1");
  });

  it("uses the exact optimized prompt as the visible and submitted prompt", () => {
    const response: OptimizeResponse = {
      original_prompt: "rough prompt",
      optimized_prompt: "Clear, specific prompt",
      was_optimized: true,
      server_optimization_enabled: true,
      optimization_status: "optimized",
    };

    const resolved = resolveOptimizationResponse(response, "rough prompt");

    expect(resolved.finalPrompt).toBe("Clear, specific prompt");
    expect(resolved.optimization.displayPrompt).toBe("Clear, specific prompt");
    expect(resolved.optimization.note).toBeUndefined();
  });

  it("keeps the original prompt with a visible reassurance on fallback", () => {
    const resolved = resolveOptimizationFailure("Already clear");

    expect(resolved.finalPrompt).toBe("Already clear");
    expect(resolved.optimization.status).toBe("kept_original");
    expect(resolved.optimization.note).toBe(OPTIMIZATION_ORIGINAL_NOTE);
  });

  it("renders stable optimization progress below the unchanged user prompt", () => {
    useChatStore.setState({
      turns: [
        turn({
          status: "optimizing",
          optimization: {
            status: "pending",
            originalPrompt: "rough prompt",
            displayPrompt: "rough prompt",
          },
        }),
      ],
    });

    render(<ResultsSection />);

    const bubble = document.querySelector("#chat-msg-0");
    expect(bubble).toHaveTextContent("rough prompt");
    expect(bubble).not.toHaveTextContent("Improving your prompt");
    expect(document.querySelector(".optimization-visible-message")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Improving your prompt");
    expect(screen.getByRole("status").closest("#chat-msg-0")).toBeNull();
    expect(document.querySelector(".optimization-live-message")).toHaveTextContent(
      "Improving your prompt",
    );
    expect(screen.queryByText("Enhancing clarity")).not.toBeInTheDocument();
    expect(screen.queryByText("Waiting for response...")).not.toBeInTheDocument();
  });

  it("omits decorative pending dots for reduced-motion users", () => {
    vi.stubGlobal("matchMedia", createMatchMedia(true));
    useChatStore.setState({
      turns: [
        turn({
          status: "optimizing",
          optimization: {
            status: "pending",
            originalPrompt: "rough prompt",
            displayPrompt: "rough prompt",
          },
        }),
      ],
    });

    render(<ResultsSection />);

    expect(document.querySelector(".optimization-visible-message")).toHaveTextContent(
      "Improving your prompt",
    );
    expect(screen.queryByText("...")).not.toBeInTheDocument();
  });

  it("renders the already-clear status beneath the original prompt", () => {
    useChatStore.setState({
      turns: [
        turn({
          prompt: "Already clear",
          submittedPrompt: "Already clear",
          optimization: resolveOptimizationFailure("Already clear").optimization,
        }),
      ],
    });

    render(<ResultsSection />);

    expect(screen.getByText("Already clear")).toBeInTheDocument();
    const clearStatus = screen.getByText("Already clear — sent as-is").closest("div");
    expect(clearStatus).toHaveClass("optimization-result-note");
    expect(clearStatus).toHaveClass("optimization-reveal");
  });

  it("renders optimized prompt status below the sent prompt and reveals the original", () => {
    useChatStore.setState({
      turns: [
        turn({
          prompt: "rough prompt",
          submittedPrompt: "Clear, specific prompt",
          optimization: {
            status: "optimized",
            originalPrompt: "rough prompt",
            displayPrompt: "Clear, specific prompt",
          },
        }),
      ],
    });

    render(<ResultsSection />);

    const bubble = document.querySelector("#chat-msg-0");
    expect(bubble).toHaveTextContent("Clear, specific prompt");
    expect(bubble).not.toHaveTextContent("Prompt optimized");
    expect(screen.getByText("Prompt optimized").closest("#chat-msg-0")).toBeNull();
    expect(screen.queryByText("rough prompt")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "View original" }));

    expect(screen.getByText("rough prompt")).toBeInTheDocument();
  });

  it("keeps Improve available in Compare mode", () => {
    render(
      <FeatureChips
        compareMode
        smartMode={false}
        optimizeMode={true}
        onSmartToggle={vi.fn()}
        onOptimizeToggle={vi.fn()}
      />,
    );

    expect(screen.getByRole("switch", { name: "Prompt optimization" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("omits optimizing and cancelled turns from future conversation context", () => {
    const completed = turn({
      id: "complete",
      prompt: "Completed prompt",
      submittedPrompt: "Completed prompt",
    });
    const optimizing = turn({
      id: "optimizing",
      prompt: "Pending prompt",
      submittedPrompt: "Pending prompt",
      status: "optimizing",
    });
    const cancelled = turn({
      id: "cancelled",
      prompt: "Cancelled prompt",
      submittedPrompt: "Cancelled prompt",
      status: "cancelled",
    });

    expect(buildConversationHistory([completed, optimizing, cancelled])).toEqual([
      { role: "user", content: "Completed prompt" },
    ]);
  });

  it("aborts optimization without starting a chat request", async () => {
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const rejectAbort = () =>
            reject(new DOMException("The operation was aborted.", "AbortError"));
          if (init?.signal?.aborted) {
            rejectAbort();
            return;
          }
          init?.signal?.addEventListener("abort", rejectAbort, { once: true });
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    useChatStore.setState({
      optimizeMode: true,
      prompt: "Cancel this optimization",
    });

    render(
      <>
        <ChatActions />
        <ResultsSection />
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => {
      expect(document.querySelector(".optimization-visible-message")).toHaveTextContent(
        "Improving your prompt",
      );
    });
    expect(useChatStore.getState().prompt).toBe("");
    expect(document.querySelector("[data-turn-id]")?.querySelectorAll("article"))
      .toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.getByText("Optimization stopped.")).toBeInTheDocument();
    });
    expect(document.querySelector("[data-turn-id]")?.querySelectorAll("article"))
      .toHaveLength(0);
    expect(useChatStore.getState().prompt).toBe("Cancel this optimization");
    expect(useChatStore.getState().streaming).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/v1/optimize");
  });
});

function ChatActions() {
  const { submit, cancel } = useChat();
  return (
    <>
      <button type="button" onClick={() => void submit()}>
        Submit
      </button>
      <button type="button" onClick={cancel}>
        Cancel
      </button>
    </>
  );
}

function turn(overrides: Partial<ChatTurn> = {}): ChatTurn {
  return {
    id: "turn-1",
    mode: "single",
    prompt: "Prompt",
    submittedPrompt: "Prompt",
    attachments: [],
    responses: [],
    status: "complete",
    createdAt: "2026-06-07T00:00:00.000Z",
    ...overrides,
  };
}

function attachment(): FileUploadResponse {
  return {
    file_id: "file-1",
    original_filename: "report.pdf",
    mime_type: "application/pdf",
    size_bytes: 100,
    status: "ready",
    ingestion_meta: {},
    created_at: "2026-06-07T00:00:00.000Z",
    deduplicated: false,
  };
}

function createMatchMedia(matches: boolean) {
  return (query: string) =>
    ({
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}
