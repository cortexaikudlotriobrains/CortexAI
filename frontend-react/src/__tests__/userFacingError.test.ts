import { describe, expect, it } from "vitest";
import { ApiClientError } from "../api/client";
import { presentError, presentResponseError } from "../errors/userFacingError";

describe("user-facing error presentation", () => {
  it("does not expose an arbitrary exception message", () => {
    const error = presentError(
      new Error("psycopg connection failed for postgres://private-host/internal"),
      "history_load",
    );

    expect(error.title).toBe("This chat couldn't load");
    expect(error.message).toBe("Your other chats are still available. Try opening this chat again.");
    expect(error.message).not.toContain("postgres");
  });

  it("maps structured timeout errors and preserves the support request id", () => {
    const error = presentError(
      new ApiClientError(
        504,
        "upstream read timeout at internal-gateway:8443",
        { detail: { code: "timeout", retryable: true, request_id: "req-timeout-1" } },
      ),
      "chat",
    );

    expect(error).toMatchObject({
      title: "The request took too long",
      message: "Try again. If it keeps happening, choose another model.",
      retryable: true,
      action: "retry",
      requestId: "req-timeout-1",
      context: "chat",
    });
  });

  it("turns provider response failures into model-specific recovery guidance", () => {
    const error = presentResponseError({
      code: "provider_error",
      message: "stack trace: provider.internal.example refused connection",
      provider: "example",
      retryable: true,
      details: { kind: "provider_5xx" },
    });

    expect(error.message).toBe("Try again or choose another model.");
    expect(error.message).not.toContain("stack trace");
  });
});
