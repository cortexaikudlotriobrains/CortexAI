import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makePlaceholderResponse, useChatStore } from "../store/chatStore";
import { StreamDeltaBuffer } from "../streaming/streamDeltaBuffer";

describe("StreamDeltaBuffer", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useChatStore.getState().startNewChat();
  });

  afterEach(() => {
    vi.useRealTimers();
    useChatStore.getState().startNewChat();
  });

  it("batches deltas until the flush interval", () => {
    vi.setSystemTime(new Date("2026-09-27T12:00:00.000Z"));
    const turnId = useChatStore.getState().beginTurn({
      mode: "single",
      prompt: "hello",
      submittedPrompt: "hello",
      attachments: [],
      responses: [makePlaceholderResponse(0, "openai", "gpt-test")],
    });
    const buffer = new StreamDeltaBuffer(turnId);

    buffer.append(0, "Hel");
    buffer.append(0, "lo");
    vi.advanceTimersByTime(119);

    expect(useChatStore.getState().turns[0]?.responses[0]?.text).toBe("");

    vi.advanceTimersByTime(1);

    const firstResponse = useChatStore.getState().turns[0]?.responses[0];
    expect(firstResponse?.text).toBe("Hello");
    expect(firstResponse?.first_visible_at).toBe("2026-09-27T12:00:00.120Z");

    buffer.append(0, " again");
    vi.advanceTimersByTime(120);

    const continuedResponse = useChatStore.getState().turns[0]?.responses[0];
    expect(continuedResponse?.text).toBe("Hello again");
    expect(continuedResponse?.first_visible_at).toBe(firstResponse?.first_visible_at);
  });

  it("does not stop first-visible timing on whitespace-only stream data", () => {
    const turnId = useChatStore.getState().beginTurn({
      mode: "single",
      prompt: "hello",
      submittedPrompt: "hello",
      attachments: [],
      responses: [makePlaceholderResponse(0, "openai", "gpt-test")],
    });
    const buffer = new StreamDeltaBuffer(turnId);

    buffer.append(0, "\n  ");
    vi.advanceTimersByTime(120);

    expect(useChatStore.getState().turns[0]?.responses[0]?.first_visible_at).toBeUndefined();
  });

  it("dispose clears pending text and timers after cancellation", () => {
    const turnId = useChatStore.getState().beginTurn({
      mode: "single",
      prompt: "hello",
      submittedPrompt: "hello",
      attachments: [],
      responses: [makePlaceholderResponse(0, "openai", "gpt-test")],
    });
    const buffer = new StreamDeltaBuffer(turnId);

    buffer.append(0, "stale text");
    useChatStore.getState().setTurnStatus(turnId, "cancelled");
    buffer.dispose();
    vi.runAllTimers();

    expect(useChatStore.getState().turns[0]?.responses[0]?.text).toBe("");
  });
});
