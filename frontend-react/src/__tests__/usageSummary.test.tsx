import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
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

  it("keeps B2C costs hidden without requesting API-key-only BYOK status", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(usageSummary()));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useUsageSummary());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.hasConfiguredByok).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["configured", { providers: [{ provider: "openai", configured: true }] }, true],
    ["unconfigured", { providers: [{ provider: "openai", configured: false }] }, false],
    ["empty", { providers: [] }, false],
    ["malformed", { providers: [{ provider: "openai", configured: "true" }] }, false],
  ])(
    "uses server-confirmed %s BYOK status for cost visibility",
    async (_name, status, expected) => {
      const fetchMock = vi.fn(async (url: string) =>
        jsonResponse(url === "/v1/byok/status" ? status : usageSummary()),
      );
      vi.stubGlobal("fetch", fetchMock);

      const { result } = renderHook(() => useUsageSummary({}, "key-a"));
      expect(result.current.hasConfiguredByok).toBe(false);
      await waitFor(() => expect(result.current.loading).toBe(false));
      await waitFor(() => expect(result.current.hasConfiguredByok).toBe(expected));
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/byok/status",
        expect.objectContaining({ method: "GET", credentials: "include" }),
      );
    },
  );

  it("keeps costs hidden when BYOK status fails without failing usage data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url === "/v1/byok/status"
          ? errorResponse("BYOK unavailable")
          : jsonResponse(usageSummary()),
      ),
    );

    const { result } = renderHook(() => useUsageSummary({}, "key-a"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasConfiguredByok).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.summary?.totalRequests).toBe(1336);
  });

  it("ignores a late BYOK confirmation after the authenticated API key changes", async () => {
    let resolveOldStatus!: (response: Response) => void;
    const oldStatus = new Promise<Response>((resolve) => {
      resolveOldStatus = resolve;
    });
    let byokRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url !== "/v1/byok/status") return jsonResponse(usageSummary());
        byokRequests += 1;
        return byokRequests === 1 ? oldStatus : jsonResponse({ providers: [] });
      }),
    );

    const { result, rerender } = renderHook(({ apiKeyId }) => useUsageSummary({}, apiKeyId), {
      initialProps: { apiKeyId: "key-a" as string | undefined },
    });
    rerender({ apiKeyId: "key-b" });
    await act(async () => {
      resolveOldStatus(jsonResponse({ providers: [{ provider: "openai", configured: true }] }));
      await oldStatus;
    });
    expect(result.current.hasConfiguredByok).toBe(false);
    rerender({ apiKeyId: undefined });
    expect(result.current.hasConfiguredByok).toBe(false);
  });

  it("hides previously confirmed costs while reload rechecks a removed BYOK key", async () => {
    let resolveReloadStatus!: (response: Response) => void;
    const reloadStatus = new Promise<Response>((resolve) => {
      resolveReloadStatus = resolve;
    });
    let byokRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url !== "/v1/byok/status") return jsonResponse(usageSummary());
        byokRequests += 1;
        return byokRequests === 1
          ? jsonResponse({ providers: [{ provider: "openai", configured: true }] })
          : reloadStatus;
      }),
    );

    const { result } = renderHook(() => useUsageSummary({}, "key-a"));
    await waitFor(() => expect(result.current.hasConfiguredByok).toBe(true));
    act(() => result.current.reload());
    expect(result.current.hasConfiguredByok).toBe(false);
    await act(async () => {
      resolveReloadStatus(jsonResponse({ providers: [] }));
      await reloadStatus;
    });
    expect(result.current.hasConfiguredByok).toBe(false);
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
      expect(screen.getByTestId("usage-state")).not.toHaveTextContent("temporary reporting outage");
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
        {loading ? "loading" : (error?.message ?? summary?.period.label ?? "empty")}
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
