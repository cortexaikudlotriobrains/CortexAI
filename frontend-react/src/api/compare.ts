import { apiClientErrorFromStreamEvent, post, streamPost } from "./client";
import type { CompareRequest, CompareResponse, CompareStreamChunk, ChatResponse } from "../types";

export async function sendCompare(request: CompareRequest): Promise<CompareResponse> {
  return post<CompareResponse>("/v1/compare", request);
}

export async function* streamCompare(
  request: CompareRequest,
  signal?: AbortSignal,
): AsyncGenerator<CompareStreamChunk> {
  const lines = streamPost("/v1/compare/stream", request, signal);

  for await (const line of lines) {
    if (signal?.aborted) break;
    const trimmed = line.trim();
    if (!trimmed || trimmed === "data: [DONE]") continue;

    const raw = trimmed.startsWith("data: ") ? trimmed.slice(6) : trimmed;
    let event: Record<string, unknown>;
    try {
      event = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      yield { type: "delta", index: 0, text: raw };
      continue;
    }

    switch (event.type) {
      case "start":
        yield { type: "start", session_id: asOptionalString(event.session_id) };
        break;
      case "response_start":
        yield {
          type: "response_start",
          index: asIndex(event.index),
          provider: asOptionalString(event.provider),
          model: asOptionalString(event.model),
        };
        break;
      case "line":
        yield { type: "delta", index: asIndex(event.index), text: String(event.text ?? "") };
        break;
      case "response_done":
        yield {
          type: "response_done",
          index: asIndex(event.index),
          response: event.response as ChatResponse,
        };
        break;
      case "done":
        yield {
          type: "done",
          session_id: asOptionalString(event.session_id),
          compare: event.compare as CompareResponse | undefined,
        };
        break;
      case "error":
        throw apiClientErrorFromStreamEvent(event, "The comparison stream ended unexpectedly.");
    }
  }
}

function asIndex(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asOptionalString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}
