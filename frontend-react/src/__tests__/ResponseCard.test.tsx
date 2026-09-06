import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResponseCard } from "../components/results/ResponseCard";
import type { ChatResponse } from "../types";

describe("ResponseCard", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a friendly model name while retaining the exact API model ID", () => {
    const { container } = render(<ResponseCard response={response()} compact />);

    expect(screen.getByRole("heading", { name: "Claude Sonnet" })).toBeInTheDocument();
    expect(screen.getByText("claude-sonnet-4-5")).toBeInTheDocument();
    expect(container.querySelector('img[src*="claude.ai"]')).toBeInTheDocument();
  });

  it("falls back to the provider initial when the shared logo cannot load", () => {
    const { container } = render(<ResponseCard response={response()} compact />);
    const logo = container.querySelector<HTMLImageElement>('img[src*="claude.ai"]');

    expect(logo).not.toBeNull();
    fireEvent.error(logo!);

    expect(container.querySelector('img[src*="claude.ai"]')).not.toBeInTheDocument();
    expect(container.querySelector("header")).toHaveTextContent("C");
  });

  it("keeps compact actions accessible by name", () => {
    const onRegenerate = vi.fn();
    render(<ResponseCard response={response()} compact onRegenerate={onRegenerate} />);

    expect(screen.queryByRole("button", { name: "Resources" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy response" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Branch response" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Helpful response" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not helpful response" })).toBeInTheDocument();
  });

  it("keeps Ask card icon actions accessible without adding branch", () => {
    const onRegenerate = vi.fn();
    render(<ResponseCard response={response()} onRegenerate={onRegenerate} />);

    expect(screen.getByRole("button", { name: "Copy response" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate response" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Branch response" })).not.toBeInTheDocument();
  });

  it("shows a visible confirmation after copying a response", async () => {
    const { restoreClipboard, writeText } = mockClipboardWrite();

    try {
      render(<ResponseCard response={response(false, "Copy this answer.")} />);

      fireEvent.click(screen.getByRole("button", { name: "Copy response" }));

      await waitFor(() => expect(writeText).toHaveBeenCalledWith("Copy this answer."));
      expect(await screen.findByRole("status")).toHaveTextContent("Copied");
      expect(screen.getByRole("button", { name: "Copied response" })).toBeInTheDocument();
    } finally {
      restoreClipboard();
    }
  });

  it("renders an explicit placeholder for a completed empty response", () => {
    render(<ResponseCard response={response(false, "")} />);

    expect(screen.getByText("(empty response)")).toBeInTheDocument();
  });

  it("renders suggested follow-ups before the action toolbar and marks a tapped chip as sent", () => {
    vi.useFakeTimers();
    const onSuggestedFollowUp = vi.fn();
    render(
      <ResponseCard
        response={response(false, "Answer with next steps.")}
        suggestedFollowUps={["Show the full odds table", "Explain dark-horse teams"]}
        onSuggestedFollowUp={onSuggestedFollowUp}
      />,
    );

    const row = screen.getByLabelText("Suggested follow-ups");
    const chip = screen.getByRole("button", {
      name: "Ask follow-up: Show the full odds table",
    });

    expect(row.nextElementSibling?.tagName).toBe("FOOTER");
    expect(
      screen.getByRole("button", { name: "Ask follow-up: Explain dark-horse teams" }),
    ).toBeInTheDocument();

    fireEvent.click(chip);

    expect(onSuggestedFollowUp).toHaveBeenCalledWith("Show the full odds table");
    expect(chip).toBeDisabled();

    act(() => {
      vi.advanceTimersByTime(1700);
    });

    expect(chip).toBeEnabled();
  });

  it("shows completed response stats without a run-details disclosure control", () => {
    render(
      <ResponseCard
        response={{ ...response(), ai_credits: 1_234, cache_savings_ai_credits: 250 }}
        compact
      />,
    );

    const stats = document.querySelector('[id^="response-stats-"]');

    expect(screen.queryByRole("button", { name: /run details/i })).not.toBeInTheDocument();
    expect(stats).toHaveTextContent("20.0s");
    expect(stats).not.toHaveTextContent("60 tok");
    expect(stats).toHaveTextContent("1.234 credits");
    expect(stats).not.toHaveTextContent("$0.0010");
    expect(stats?.querySelectorAll("svg")).toHaveLength(2);
    expect(screen.getByText("Saved ~0.25 credits through context reuse")).toBeInTheDocument();
  });

  it("preserves a token-limited partial answer and offers a larger retry", () => {
    const onRetry = vi.fn();
    render(
      <ResponseCard
        response={{
          ...response(false, "Partial but useful answer"),
          completion_status: "incomplete",
          stop_cause: "token_limit",
          retry_with_more_room: {
            available: true,
            recommended_profile: "deep",
          },
        }}
        onRetryWithMoreRoom={onRetry}
      />,
    );

    expect(screen.getByText("Partial but useful answer")).toBeInTheDocument();
    expect(screen.getByText("Response stopped at its token limit.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry with more room" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows live elapsed loading meta without placeholder zero metrics", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-09T00:00:08.000Z"));
    const pending = {
      ...response(false, ""),
      latency_ms: null,
      token_usage: null,
      estimated_cost: 0,
      ui_status: "streaming" as const,
      started_at: "2026-06-09T00:00:00.000Z",
    };

    render(<ResponseCard response={pending} isStreaming loadingMode="compare" />);

    const header = document.querySelector("header");
    const stats = header?.querySelector('[id^="response-stats-"]');
    expect(screen.queryByRole("button", { name: /run details/i })).not.toBeInTheDocument();
    expect(stats?.className).toContain("metaRowPinned");
    expect(stats?.className).toContain("loadingMetaRow");
    expect(header?.firstElementChild?.nextElementSibling).toBe(stats);
    expect(header).toHaveTextContent("00:08 elapsed · Generating response");
    expect(header).not.toHaveTextContent("0.0s");
    expect(header).not.toHaveTextContent("0 tokens");

    act(() => {
      vi.advanceTimersByTime(2000);
    });

    expect(header).toHaveTextContent("00:10 elapsed · Generating response");
  });

  it("shows completed duration without rendering token usage from the response", () => {
    const completed = {
      ...response(false, "Completed answer."),
      latency_ms: 12400,
      token_usage: {
        prompt_tokens: 248,
        completion_tokens: 1000,
        total_tokens: 1248,
      },
      estimated_cost: 0,
    };

    render(<ResponseCard response={completed} compact />);

    const header = document.querySelector("header");
    expect(header).toHaveTextContent("12.4s");
    expect(header).not.toHaveTextContent("1,248 tok");
    expect(header?.querySelectorAll("svg")).toHaveLength(1);
  });

  it("uses UI-observed timestamps for completed duration when available", () => {
    const completed = {
      ...response(false, "Completed answer."),
      latency_ms: 1200,
      started_at: "2026-06-09T00:00:00.000Z",
      completed_at: "2026-06-09T00:00:08.400Z",
      estimated_cost: 0,
    };

    render(<ResponseCard response={completed} compact />);

    const header = document.querySelector("header");
    expect(header).toHaveTextContent("8.4s");
    expect(header).not.toHaveTextContent("1.2s");
  });

  it("shows completed duration when token usage is unavailable", () => {
    const completed = {
      ...response(false, "Completed answer."),
      token_usage: null,
      estimated_cost: 0,
    };

    render(<ResponseCard response={completed} compact />);

    const header = document.querySelector("header");
    expect(header).toHaveTextContent("20.0s");
    expect(header).not.toHaveTextContent("tokens");
  });

  it("shows failed elapsed time without token metrics", () => {
    const failed = {
      ...response(false, ""),
      latency_ms: null,
      token_usage: null,
      estimated_cost: 0,
      ui_status: "failed" as const,
      started_at: "2026-06-09T00:00:00.000Z",
      failed_at: "2026-06-09T00:00:08.200Z",
      error: {
        code: "stream_error",
        message: "Stream disconnected.",
        provider: "claude",
        retryable: false,
        details: {},
      },
    };

    render(<ResponseCard response={failed} compact />);

    const header = document.querySelector("header");
    expect(header).toHaveTextContent("Failed after 8.2 sec");
    expect(header).not.toHaveTextContent("tokens");
  });

  it("does not render legacy source controls when sources have no inline markers", () => {
    render(<ResponseCard response={response(true, "A sourced answer without inline refs.")} compact />);

    expect(screen.queryByRole("button", { name: "Resources" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /source:/i })).not.toBeInTheDocument();
    expect(screen.queryByText("CortexAI documentation")).not.toBeInTheDocument();
  });

  it("replaces the loading state as soon as streamed text arrives", () => {
    const pending = response(false, "");
    const { rerender } = render(
      <ResponseCard response={pending} isStreaming loadingMode="ask" />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Thinking through your request\u2026",
    );
    expect(screen.queryByText("Waiting for response...")).not.toBeInTheDocument();

    rerender(
      <ResponseCard
        response={{ ...pending, text: "The first streamed token" }}
        isStreaming
        loadingMode="ask"
      />,
    );

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByText("The first streamed token")).toBeInTheDocument();
  });

  it("uses request-aware loading copy for sources and prompt improvement", () => {
    const pending = response(false, "");
    const { rerender } = render(
      <ResponseCard
        response={pending}
        isStreaming
        loadingMode="compare"
        researchEnabled
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking sources and preparing an answer\u2026",
    );

    rerender(
      <ResponseCard
        response={pending}
        isStreaming
        loadingMode="compare"
        researchEnabled
        optimizeEnabled
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent(
      "Refining prompt and preparing response\u2026",
    );
  });

  it("groups consecutive numeric citations into one publisher pill", () => {
    render(
      <ResponseCard
        response={responseWithSources("The claim is supported. [1][2] [3]")}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Sources: NPR and 2 more" }),
    ).toHaveTextContent("NPR + 2");
  });

  it("does not convert citation-looking text inside links or code", () => {
    const { container } = render(
      <ResponseCard
        response={response(
          false,
          [
            "Read [the source](https://example.com/path).",
            "",
            "Inline `value [1]` remains code.",
            "",
            "```txt",
            "block [2]",
            "```",
          ].join("\n"),
        )}
      />,
    );

    expect(container.querySelector("cite")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /source:/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "the source" })).toHaveAttribute(
      "href",
      "https://example.com/path",
    );
    expect(screen.getByText("value [1]")).toBeInTheDocument();
    expect(screen.getByText("block [2]")).toBeInTheDocument();
  });

  it("opens desktop citation previews on hover and closes them when hover leaves", () => {
    vi.useFakeTimers();
    render(<ResponseCard response={responseWithSources("Supported by reporting. [1][2]")} />);

    const pill = screen.getByRole("button", { name: "Sources: NPR and 1 more" });
    const root = citationRootFor(pill);

    fireEvent.click(pill);
    expect(screen.queryByRole("dialog", { name: "Citation sources" })).not.toBeInTheDocument();

    fireEvent.mouseEnter(root);

    const dialog = screen.getByRole("dialog", { name: "Citation sources" });
    expect(root.className).toContain("citationRootOpen");
    expect(dialog).toBeInTheDocument();
    expect(
      within(dialog).getByRole("link", { name: /Morning Edition NPR/ }),
    ).toHaveAttribute("href", "https://www.npr.org/sections/news/");
    expect(
      within(dialog).getByRole("link", { name: /World report BBC/ }),
    ).toHaveAttribute("href", "https://www.bbc.co.uk/news/world");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Citation sources" })).not.toBeInTheDocument();

    fireEvent.mouseEnter(root);
    expect(screen.getByRole("dialog", { name: "Citation sources" })).toBeInTheDocument();
    fireEvent.pointerDown(
      within(screen.getByRole("dialog", { name: "Citation sources" })).getByRole(
        "link",
        { name: /Morning Edition NPR/ },
      ),
    );
    expect(screen.getByRole("dialog", { name: "Citation sources" })).toBeInTheDocument();

    fireEvent.mouseLeave(root);
    act(() => {
      vi.advanceTimersByTime(90);
    });
    expect(screen.queryByRole("dialog", { name: "Citation sources" })).not.toBeInTheDocument();
  });

  it("anchors the desktop citation preview from a body portal near the hovered pill", async () => {
    render(<ResponseCard response={responseWithSources("Supported by reporting. [1][2]")} />);

    const pill = screen.getByRole("button", { name: "Sources: NPR and 1 more" });
    vi.spyOn(pill, "getBoundingClientRect").mockReturnValue({
      x: 240,
      y: 120,
      left: 240,
      top: 120,
      right: 306,
      bottom: 144,
      width: 66,
      height: 24,
      toJSON: () => ({}),
    } as DOMRect);

    fireEvent.mouseEnter(citationRootFor(pill));

    const dialog = await screen.findByRole("dialog", { name: "Citation sources" });
    expect(dialog.parentElement).toBe(document.body);
    await waitFor(() => {
      expect(dialog.style.getPropertyValue("--citation-popover-left")).toBe("240px");
      expect(dialog.style.getPropertyValue("--citation-popover-top")).toBe("152px");
    });
  });

  it("keeps a lower-page Ask citation preview adjacent to the hovered pill", async () => {
    const previewRectSpy = vi
      .spyOn(HTMLSpanElement.prototype, "getBoundingClientRect")
      .mockReturnValue({
        x: 240,
        y: 12,
        left: 240,
        top: 12,
        right: 600,
        bottom: 136,
        width: 360,
        height: 124,
        toJSON: () => ({}),
      } as DOMRect);
    render(<ResponseCard response={responseWithSources("Supported by reporting. [1][2]")} />);

    const pill = screen.getByRole("button", { name: "Sources: NPR and 1 more" });
    vi.spyOn(pill, "getBoundingClientRect").mockReturnValue({
      x: 240,
      y: 720,
      left: 240,
      top: 720,
      right: 306,
      bottom: 744,
      width: 66,
      height: 24,
      toJSON: () => ({}),
    } as DOMRect);

    fireEvent.mouseEnter(citationRootFor(pill));

    const dialog = await screen.findByRole("dialog", { name: "Citation sources" });
    await waitFor(() => {
      expect(dialog.style.getPropertyValue("--citation-popover-top")).toBe("588px");
      expect(dialog.style.getPropertyValue("--citation-popover-max-height")).toBe("360px");
    });
    previewRectSpy.mockRestore();
  });

  it("opens the citation external icon as a direct source link", () => {
    render(<ResponseCard response={responseWithSources("Supported by reporting. [1]")} />);

    expect(
      screen.getByRole("link", { name: "Open NPR source in a new tab" }),
    ).toHaveAttribute("href", "https://www.npr.org/sections/news/");
  });

  it("normalizes source links that arrive without a URL scheme", () => {
    render(
      <ResponseCard
        response={{
          ...response(false, "Supported by reporting. [1]"),
          web_source_items: [
            { title: "Bare source", url: "example.com/report" },
          ],
        }}
      />,
    );

    expect(
      screen.getByRole("link", { name: "Open Example source in a new tab" }),
    ).toHaveAttribute("href", "https://example.com/report");

    fireEvent.mouseEnter(citationRootFor(screen.getByRole("button", { name: "Source: Example" })));
    expect(
      within(screen.getByRole("dialog", { name: "Citation sources" })).getByRole(
        "link",
        { name: /Bare source/ },
      ),
    ).toHaveAttribute("href", "https://example.com/report");
  });

  it("falls back to a publisher initial when a citation favicon fails", () => {
    render(<ResponseCard response={responseWithSources("Supported by reporting. [1]")} />);

    fireEvent.mouseEnter(citationRootFor(screen.getByRole("button", { name: "Source: NPR" })));

    const dialog = screen.getByRole("dialog", { name: "Citation sources" });
    const favicon = dialog.querySelector("img");
    expect(favicon).not.toBeNull();
    fireEvent.error(favicon!);

    expect(within(dialog).getByText("N")).toBeInTheDocument();
  });

  it("keeps mobile citation previews on tap as a bottom sheet", () => {
    const restoreMatchMedia = mockMatchMedia(true);

    try {
      render(<ResponseCard response={responseWithSources("Supported by reporting. [1]")} />);

      const pill = screen.getByRole("button", { name: "Source: NPR" });
      fireEvent.mouseEnter(citationRootFor(pill));
      expect(screen.queryByRole("dialog", { name: "Citation sources" })).not.toBeInTheDocument();

      fireEvent.click(pill);

      const dialog = screen.getByRole("dialog", { name: "Citation sources" });
      expect(dialog.className).toContain("citationSheet");

      fireEvent.click(dialog.parentElement!);
      expect(screen.queryByRole("dialog", { name: "Citation sources" })).not.toBeInTheDocument();
    } finally {
      restoreMatchMedia();
    }
  });

  it("renders GFM tables with semantic headers and mobile data labels", () => {
    render(
      <ResponseCard
        response={response(
          false,
          [
            "| Area | Risk | Owner |",
            "| :--- | :---: | ---: |",
            "| API | Medium | Platform |",
            "| UI | Low | Product |",
          ].join("\n"),
        )}
      />,
    );

    const table = screen.getByRole("table");
    expect(table).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
    expect(screen.getByRole("columnheader", { name: "Risk" })).toHaveStyle({
      textAlign: "center",
    });
    expect(screen.getByRole("cell", { name: "Platform" })).toHaveAttribute(
      "data-label",
      "Owner",
    );
    expect(screen.getByRole("region", { name: "Response table" })).toHaveAttribute(
      "tabindex",
      "0",
    );
  });
});

function response(withSources = false, text = "A compact comparison response."): ChatResponse {
  return {
    request_id: "response-1",
    session_id: "session-1",
    text,
    provider: "claude",
    model: "claude-sonnet-4-5",
    latency_ms: 20027,
    token_usage: {
      prompt_tokens: 20,
      completion_tokens: 40,
      total_tokens: 60,
    },
    estimated_cost: 0.001,
    cost_currency: "USD",
    web_source_items: withSources
      ? [{ title: "CortexAI documentation", url: "https://example.com/cortex" }]
      : [],
    timestamp: "2026-06-09T00:00:00.000Z",
  };
}

function responseWithSources(text: string): ChatResponse {
  return {
    ...response(false, text),
    web_source_items: [
      { title: "Morning Edition", url: "https://www.npr.org/sections/news/" },
      { title: "World report", url: "https://www.bbc.co.uk/news/world" },
      { title: "Large language model - Wikipedia", url: "https://en.wikipedia.org/wiki/Large_language_model" },
    ],
  };
}

function citationRootFor(pill: HTMLElement): HTMLElement {
  const root = pill.parentElement;
  if (!root) throw new Error("Citation pill root was not rendered");
  return root;
}

function mockMatchMedia(matchesSmallScreen: boolean): () => void {
  const originalMatchMedia = window.matchMedia;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query === "(max-width: 760px)" ? matchesSmallScreen : false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  return () => {
    window.matchMedia = originalMatchMedia;
  };
}

function mockClipboardWrite() {
  const originalClipboard = navigator.clipboard;
  const writeText = vi.fn().mockResolvedValue(undefined);

  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });

  return {
    writeText,
    restoreClipboard: () => {
      if (originalClipboard) {
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: originalClipboard,
        });
        return;
      }

      Reflect.deleteProperty(navigator, "clipboard");
    },
  };
}
