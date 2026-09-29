import { describe, expect, it } from "vitest";
import {
  buildHistoryThreads,
  buildTurnsFromHistoryEntries,
  filterHistoryThreads,
} from "../history/historyThreads";
import { buildConversationHistory } from "../hooks/useChat";
import type { ChatTurn, HistoryEntry } from "../types";

describe("history threads", () => {
  it("groups multiple Ask entries from one session into one sidebar thread", () => {
    const entries = [
      entry({
        id: 2,
        timestamp: "2026-06-07T12:01:00Z",
        prompt: "Follow up",
        response: "Second answer",
      }),
      entry({
        id: 1,
        timestamp: "2026-06-07T12:00:00Z",
        prompt: "Initial question",
        response: "First answer",
        prompt_tokens: 4,
        completion_tokens: 6,
        tokens: 10,
        ai_credits: 52,
      }),
    ];

    const threads = buildHistoryThreads(entries);

    expect(threads).toHaveLength(1);
    expect(threads[0]).toMatchObject({
      sessionId: "session-1",
      title: "Initial question",
      mode: "single",
      turnCount: 2,
    });
    expect(buildTurnsFromHistoryEntries(threads[0].entries).map((turn) => turn.prompt)).toEqual([
      "Initial question",
      "Follow up",
    ]);
    const restoredResponse = buildTurnsFromHistoryEntries(threads[0].entries)[0].responses[0];
    expect(restoredResponse.ai_credits).toBe(52);
    expect(restoredResponse.token_usage).toMatchObject({
      prompt_tokens: 4,
      completion_tokens: 6,
      total_tokens: 10,
    });
  });

  it("groups compare target rows into one turn using request_group_id", () => {
    const entries = [
      entry({
        id: 10,
        mode: "compare",
        prompt: "Compare these",
        provider: "openai",
        model: "gpt-5",
        response: "OpenAI answer",
        request_group_id: "group-1",
        ai_credits: 100,
        research_ai_credits: 10_000,
      }),
      entry({
        id: 11,
        mode: "compare",
        prompt: "Compare these",
        provider: "gemini",
        model: "gemini-2.5-flash",
        response: "Gemini answer",
        request_group_id: "group-1",
        ai_credits: 200,
        research_ai_credits: 10_000,
      }),
    ];

    const threads = buildHistoryThreads(entries);
    const turns = buildTurnsFromHistoryEntries(threads[0].entries);

    expect(threads).toHaveLength(1);
    expect(threads[0].turnCount).toBe(1);
    expect(turns).toHaveLength(1);
    expect(turns[0].mode).toBe("compare");
    expect(turns[0].requestGroupId).toBe("group-1");
    expect(turns[0].responses).toHaveLength(2);
    expect(turns[0].responses.map((response) => response.ai_credits)).toEqual([100, 200]);
    expect(turns[0].compareSummary).toMatchObject({
      request_group_id: "group-1",
      success_count: 2,
      error_count: 0,
      total_ai_credits: 10_300,
    });
  });

  it.each([
    [8_192, "deep"],
    [12_288, "extended"],
    [32_768, undefined],
  ] as const)(
    "reconstructs the Auto retry step from an effective %i-token budget",
    (effectiveMaxOutputTokens, recommendedProfile) => {
      const [restored] = buildTurnsFromHistoryEntries([
        entry({
          completion_status: "incomplete",
          stop_cause: "token_limit",
          generation_profile: "auto",
          effective_max_output_tokens: effectiveMaxOutputTokens,
        }),
      ]);

      expect(restored.responses[0].retry_with_more_room).toEqual({
        available: Boolean(recommendedProfile),
        recommended_profile: recommendedProfile,
      });
    },
  );

  it("restores Smart routing and effective reasoning metadata", () => {
    const [restored] = buildTurnsFromHistoryEntries([
      entry({
        routing_mode: "smart",
        generation_profile: "auto",
        effective_max_output_tokens: 8_192,
        effective_reasoning_mode: "standard",
        effective_reasoning_effort: "high",
        generation_policy_version: "generation-budget-v3",
      }),
    ]);

    expect(restored.responses[0]).toMatchObject({
      routing_mode: "smart",
      generation_budget: {
        effective_reasoning_mode: "standard",
        effective_reasoning_effort: "high",
      },
    });
  });

  it("searches all prompts, responses, providers, and models in a thread", () => {
    const threads = buildHistoryThreads([
      entry({ prompt: "Initial question", response: "Contains a deployment checklist" }),
      entry({ id: 2, prompt: "Follow up", provider: "gemini", model: "gemini-2.5-flash" }),
    ]);

    expect(filterHistoryThreads(threads, "deployment")).toHaveLength(1);
    expect(filterHistoryThreads(threads, "gemini-2.5")).toHaveLength(1);
    expect(filterHistoryThreads(threads, "not present")).toHaveLength(0);
  });

  it("uses a persisted session title and includes it in search", () => {
    const threads = buildHistoryThreads([
      entry({ prompt: "Original prompt", session_title: "Launch readiness" }),
    ]);

    expect(threads[0].title).toBe("Launch readiness");
    expect(filterHistoryThreads(threads, "launch readiness")).toHaveLength(1);
  });

  it("restores the persisted error category for safe response-card presentation", () => {
    const [turn] = buildTurnsFromHistoryEntries([
      entry({
        response: "[error] Traceback from private-provider-host",
        error_code: "provider_5xx",
        completion_status: "failed",
      }),
    ]);

    expect(turn.responses[0].error).toMatchObject({
      code: "provider_5xx",
      retryable: false,
    });
  });

  it.each(["API Chat", "API Compare", " api chat "])(
    "falls back to the first prompt for the generic session title %s",
    (sessionTitle) => {
      const threads = buildHistoryThreads([
        entry({ prompt: "Actual thread question", session_title: sessionTitle }),
      ]);

      expect(threads[0].title).toBe("Actual thread question");
      expect(filterHistoryThreads(threads, "actual thread")).toHaveLength(1);
      expect(filterHistoryThreads(threads, "api chat")).toHaveLength(0);
    },
  );
});

describe("conversation history", () => {
  it("includes the latest completed active turn and skips streaming turns", () => {
    const completed: ChatTurn = {
      id: "completed",
      mode: "single",
      prompt: "Previous prompt",
      submittedPrompt: "Previous prompt",
      attachments: [],
      responses: [chatResponse("Previous answer")],
      status: "complete",
      createdAt: "2026-06-07T12:00:00Z",
    };
    const streaming: ChatTurn = {
      ...completed,
      id: "streaming",
      prompt: "Current prompt",
      submittedPrompt: "Current prompt",
      status: "streaming",
    };

    expect(buildConversationHistory([completed, streaming])).toEqual([
      { role: "user", content: "Previous prompt" },
      { role: "assistant", content: "Previous answer" },
    ]);
  });
});

function entry(overrides: Partial<HistoryEntry> = {}): HistoryEntry {
  return {
    id: 1,
    session_id: "session-1",
    timestamp: "2026-06-07T12:00:00Z",
    mode: "chat",
    prompt: "Question",
    provider: "openai",
    model: "gpt-5",
    response: "Answer",
    latency_ms: 100,
    tokens: 10,
    cost: 0.001,
    web_source_items: [],
    ...overrides,
  };
}

function chatResponse(text: string) {
  return {
    request_id: "response-1",
    text,
    provider: "openai",
    model: "gpt-5",
    latency_ms: 100,
    token_usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
    estimated_cost: 0.001,
    cost_currency: "USD",
    web_source_items: [],
    timestamp: "2026-06-07T12:00:00Z",
  };
}
