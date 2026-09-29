import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, streamPost } from "../api/client";
import { streamChat } from "../api/chat";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, post: vi.fn(), streamPost: vi.fn() };
});

describe("streamChat", () => {
  beforeEach(() => {
    vi.mocked(streamPost).mockReset();
  });

  it("preserves the routed provider and model from the start event", async () => {
    vi.mocked(streamPost).mockReturnValue(
      streamLines([
        JSON.stringify({
          type: "start",
          provider: "claude",
          model: "claude-sonnet-4-5",
          session_id: "session-1",
        }),
      ]),
    );

    const chunks = [];
    for await (const chunk of streamChat({ prompt: "Explain this" })) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual([
      {
        type: "start",
        provider: "claude",
        model: "claude-sonnet-4-5",
        session_id: "session-1",
      },
    ]);
  });

  it("throws a structured stream error without losing the support request id", async () => {
    vi.mocked(streamPost).mockReturnValue(
      streamLines([
        JSON.stringify({
          type: "error",
          code: "internal_error",
          message: "A safe public message.",
          retryable: true,
          request_id: "req-stream-1",
        }),
      ]),
    );

    let thrown: unknown;
    try {
      for await (const chunk of streamChat({ prompt: "Explain this" })) {
        // The error event terminates the stream before a chunk is yielded.
        void chunk;
      }
    } catch (error) {
      thrown = error;
    }

    expect(thrown).toBeInstanceOf(ApiClientError);
    expect(thrown).toMatchObject({
      code: "internal_error",
      retryable: true,
      requestId: "req-stream-1",
    });
  });
});

async function* streamLines(lines: string[]): AsyncGenerator<string> {
  for (const line of lines) yield line;
}
