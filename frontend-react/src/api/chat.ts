import { apiClientErrorFromStreamEvent, post, streamPost } from "./client";
import type { ChatRequest, ChatResponse, CortexActivityEvent, StreamChunk } from "../types";

export async function* streamChat(
  request: ChatRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamChunk> {
  const lines = streamPost("/v1/chat/stream", request, signal);

  for await (const line of lines) {
    if (signal?.aborted) break;
    const trimmed = line.trim();
    if (!trimmed || trimmed === "data: [DONE]") continue;

    const raw = trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed;

    let event: Record<string, unknown>;
    try {
      event = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      yield { type: "delta", text: raw };
      continue;
    }

    switch (event.type) {
      case "start":
        yield {
          type: "start",
          provider: asOptionalString(event.provider),
          model: asOptionalString(event.model),
          session_id: asOptionalString(event.session_id),
        };
        break;
      case "line":
        yield { type: "delta", text: String(event.text ?? "") };
        break;
      case "activity": {
        const activity = asActivityEvent(event.activity);
        if (activity) yield { type: "activity", activity };
        break;
      }
      case "response_done":
        yield { type: "metadata", metadata: event.response as Partial<ChatResponse> };
        break;
      case "done":
        yield { type: "done", session_id: asOptionalString(event.session_id) };
        break;
      case "error":
        throw apiClientErrorFromStreamEvent(event, "The response stream ended unexpectedly.");
    }
  }
}

export async function sendChat(request: ChatRequest): Promise<ChatResponse> {
  return post<ChatResponse>("/v1/chat", request);
}

function asOptionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function asActivityEvent(value: unknown): CortexActivityEvent | undefined {
  if (!value || typeof value !== "object") return undefined;
  const activity = value as Partial<CortexActivityEvent>;
  if (
    typeof activity.request_id !== "string" ||
    typeof activity.event_type !== "string" ||
    typeof activity.display_message !== "string" ||
    typeof activity.sequence_number !== "number" ||
    !Number.isFinite(activity.sequence_number)
  ) {
    return undefined;
  }
  return activity as CortexActivityEvent;
}
