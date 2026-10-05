import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResponseCard } from "../components/results/ResponseCard";
import {
  activityDisplayMessage,
  activityRunStatus,
  activityTargetIndexes,
  isNewActivityEvent,
  useSmoothedActivity,
} from "../streaming/activityPresentation";
import type { ChatResponse, CortexActivityEvent, CortexActivityType } from "../types";

describe("provider-aware response activity", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("maps normalized activity to the existing response run state", () => {
    expect(activityRunStatus(activity("SEARCH_STARTED", 1))).toBe("requesting");
    expect(activityRunStatus(activity("ANSWER_DELTA", 2))).toBe("streaming");
    expect(activityRunStatus(activity("REQUEST_FAILED", 3))).toBe("failed");
    expect(activityRunStatus(activity("REQUEST_CANCELLED", 4))).toBe("cancelled");
  });

  it("rejects duplicate and old-request activity", () => {
    const current = activity("SEARCH_STARTED", 4);

    expect(isNewActivityEvent(current, activity("ANSWER_DELTA", 5))).toBe(true);
    expect(isNewActivityEvent(current, activity("ANSWER_DELTA", 4))).toBe(false);
    expect(
      isNewActivityEvent(current, {
        ...activity("ANSWER_DELTA", 5),
        request_id: "old-request",
      }),
    ).toBe(false);
  });

  it("targets one Compare card unless an event is request-wide", () => {
    expect(activityTargetIndexes(1, 3)).toEqual([1]);
    expect(activityTargetIndexes(undefined, 3)).toEqual([0, 1, 2]);
  });

  it("does not flash a short semantic phase before answer output", () => {
    vi.useFakeTimers();
    const { rerender } = render(<ActivityProbe value={activity("REQUEST_RECEIVED", 1)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Starting…");

    rerender(<ActivityProbe value={activity("SEARCH_STARTED", 2)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Starting…");

    rerender(<ActivityProbe value={activity("ANSWER_DELTA", 3)} />);
    expect(screen.getByRole("status")).toHaveTextContent("Answering…");

    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("status")).toHaveTextContent("Answering…");
  });

  it("shows backend-evidenced provider activity in the stable loading container", () => {
    render(
      <ResponseCard
        response={{
          ...response(""),
          ui_status: "requesting",
          activity: activity("SEARCH_STARTED", 2, "Searching the web…"),
        }}
        isStreaming
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Searching the web…");
    expect(document.querySelector('[data-activity-event="SEARCH_STARTED"]')).toBeInTheDocument();
  });

  it("preserves partial answer text when the provider fails", () => {
    render(
      <ResponseCard
        response={{
          ...response("Useful partial answer."),
          ui_status: "failed",
          failed_at: "2026-10-04T12:00:03.000Z",
          error: {
            code: "provider_error",
            message: "The provider stopped before finishing.",
            provider: "openai",
            retryable: true,
            details: {},
          },
        }}
      />,
    );

    expect(screen.getByText("Useful partial answer.")).toBeInTheDocument();
    expect(screen.getByText("Try again or choose another model.")).toBeInTheDocument();
  });

  it("shows a stopped state without inventing an empty response", () => {
    render(
      <ResponseCard
        response={{
          ...response(""),
          ui_status: "cancelled",
          cancelled_at: "2026-10-04T12:00:03.000Z",
        }}
      />,
    );

    expect(screen.getByText("Generation stopped.")).toBeInTheDocument();
    expect(screen.queryByText("(empty response)")).not.toBeInTheDocument();
  });
});

function ActivityProbe({ value }: { value: CortexActivityEvent }) {
  const displayed = useSmoothedActivity(value, true);
  return <span role="status">{activityDisplayMessage(displayed)}</span>;
}

function activity(
  eventType: CortexActivityType,
  sequenceNumber: number,
  displayMessage?: string,
): CortexActivityEvent {
  return {
    request_id: "request-1",
    conversation_id: "conversation-1",
    provider: "openai",
    model: "gpt-test",
    mode: "chat",
    event_type: eventType,
    phase: eventType.toLowerCase(),
    display_message: displayMessage ?? fallbackMessage(eventType),
    timestamp: "2026-10-04T12:00:00.000Z",
    sequence_number: sequenceNumber,
    metadata: {},
  };
}

function fallbackMessage(eventType: CortexActivityType): string {
  return (
    activityDisplayMessage({
      request_id: "fallback",
      mode: "chat",
      event_type: eventType,
      phase: "test",
      display_message: "",
      timestamp: "2026-10-04T12:00:00.000Z",
      sequence_number: 0,
      metadata: {},
    }) ?? "Working…"
  );
}

function response(text: string): ChatResponse {
  return {
    request_id: "response-1",
    text,
    provider: "openai",
    model: "gpt-test",
    latency_ms: null,
    token_usage: null,
    estimated_cost: 0,
    cost_currency: "USD",
    web_source_items: [],
    timestamp: "2026-10-04T12:00:00.000Z",
    started_at: "2026-10-04T12:00:00.000Z",
  };
}
