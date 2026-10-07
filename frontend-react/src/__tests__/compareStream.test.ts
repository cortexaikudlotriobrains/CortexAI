import { beforeEach, describe, expect, it, vi } from "vitest";
import { streamPost } from "../api/client";
import { streamCompare } from "../api/compare";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, post: vi.fn(), streamPost: vi.fn() };
});

describe("streamCompare", () => {
  beforeEach(() => {
    vi.mocked(streamPost).mockReset();
  });

  it("keeps per-model and request-wide activity scopes distinct", async () => {
    vi.mocked(streamPost).mockReturnValue(
      streamLines([activityLine("REQUEST_RECEIVED", 1), activityLine("SEARCH_STARTED", 2, 1)]),
    );

    const chunks = [];
    for await (const chunk of streamCompare({ prompt: "Compare", targets: [] })) {
      chunks.push(chunk);
    }

    expect(chunks).toMatchObject([
      { type: "activity", index: undefined, activity: { event_type: "REQUEST_RECEIVED" } },
      { type: "activity", index: 1, activity: { event_type: "SEARCH_STARTED" } },
    ]);
  });
});

function activityLine(eventType: string, sequenceNumber: number, index?: number): string {
  return JSON.stringify({
    type: "activity",
    ...(index === undefined ? {} : { index }),
    activity: {
      request_id: "request-compare",
      conversation_id: "session-1",
      provider: index === undefined ? null : "openai",
      model: index === undefined ? null : "gpt-test",
      mode: "compare",
      event_type: eventType,
      phase: "test",
      display_message: "Working…",
      timestamp: "2026-10-04T12:00:00.000Z",
      sequence_number: sequenceNumber,
      metadata: {},
    },
  });
}

async function* streamLines(lines: string[]): AsyncGenerator<string> {
  for (const line of lines) yield line;
}
