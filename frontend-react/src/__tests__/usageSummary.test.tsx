import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchUsageSummary } from "../api/usage";
import { useUsageSummary } from "../hooks/useUsageSummary";
import type { UsageSummary } from "../types";

describe("usage summary data layer", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("fetches the default usage summary endpoint", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(usageSummary()));
    vi.stubGlobal("fetch", fetchMock);

    const summary = await fetchUsageSummary();

    expect(summary.period.label).toBe("Last 30 days");
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/usage/summary",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
  });

  it("adds period params when provided", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(usageSummary()));
    vi.stubGlobal("fetch", fetchMock);

    await fetchUsageSummary({ from: "2026-06-01", to: "2026-06-30" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/usage/summary?from=2026-06-01&to=2026-06-30",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
  });

  it("loads usage data through the hook and retries after an inline error", async () => {
    const user = userEvent.setup();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(errorResponse("temporary reporting outage"))
      .mockResolvedValueOnce(jsonResponse(usageSummary({ label: "June 2026" })));
    vi.stubGlobal("fetch", fetchMock);

    render(<UsageSummaryProbe from="2026-06-01" to="2026-06-30" />);

    expect(screen.getByTestId("usage-state")).toHaveTextContent("loading");

    await waitFor(() => {
      expect(screen.getByTestId("usage-state")).toHaveTextContent(
        "Your usage data is safe. Please try again.",
      );
      expect(screen.getByTestId("usage-state")).not.toHaveTextContent(
        "temporary reporting outage",
      );
    });

    await user.click(screen.getByRole("button", { name: "Reload" }));

    await waitFor(() => {
      expect(screen.getByTestId("usage-state")).toHaveTextContent("June 2026");
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

function UsageSummaryProbe({ from, to }: { from: string; to: string }) {
  const { summary, loading, error, reload } = useUsageSummary({ from, to });

  return (
    <div>
      <p data-testid="usage-state">
        {loading ? "loading" : error?.message ?? summary?.period.label ?? "empty"}
      </p>
      <button type="button" onClick={reload}>
        Reload
      </button>
    </div>
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(message: string): Response {
  return new Response(JSON.stringify({ detail: { message } }), {
    status: 503,
    statusText: "Service Unavailable",
    headers: { "Content-Type": "application/json" },
  });
}

function usageSummary(overrides: Partial<UsageSummary["period"]> = {}): UsageSummary {
  return {
    period: {
      from: "2026-06-01",
      to: "2026-06-30",
      label: "Last 30 days",
      ...overrides,
    },
    totalTokens: 2840000,
    totalRequests: 1336,
    totalSessions: 312,
    avgLatencyMs: 4600,
    p95LatencyMs: 8100,
    minLatencyMs: 1400,
    avgCostPerRequest: 0.0091,
    totalSpend: 12.16,
    tokensDeltaPct: 18.4,
    smartRoutedTotal: 720,
    models: [
      {
        provider: "openai",
        modelId: "gpt-5.4-mini",
        displayName: "GPT-5.4 Mini",
        replies: 512,
        viaSmart: 470,
      },
    ],
    sessionModes: { askOnly: 168, compareOnly: 96, mixed: 48 },
    switchedMidSession: 48,
    activityDaily: [{ date: "2026-06-30", tokens: 200000 }],
  };
}
