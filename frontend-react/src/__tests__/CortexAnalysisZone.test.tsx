import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CortexAnalysisZone } from "../components/results/CortexAnalysisZone";
import { isCortexAnalysisRunStale } from "../analysis/cortexAnalysisStaleness";
import type { ChatResponse, ChatTurn, CortexAnalysisRun } from "../types";

describe("CortexAnalysisZone", () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  it("shows the action only after at least two responses succeed", () => {
    const turn = compareTurn();
    const { rerender } = render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Combine these answers" })).toBeInTheDocument();
    expect(screen.getByText("From your 2 answers")).toBeInTheDocument();
    expect(screen.queryByText(/GPT-5\.4 mini/i)).not.toBeInTheDocument();

    rerender(
      <CortexAnalysisZone
        turn={{ ...turn, responses: [turn.responses[0]!] }}
        onAnalyze={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Combine these answers" })).not.toBeInTheDocument();
  });

  it("runs on demand and shows the three processing steps without a percentage", async () => {
    const user = userEvent.setup();
    const onAnalyze = vi.fn(async () => undefined);
    const previousRun = analysisRun({
      recommendedAnswer: "Previously combined answer.",
    });
    const turn = {
      ...compareTurn(),
      analysisRuns: [previousRun],
    };
    const { rerender } = render(<CortexAnalysisZone turn={turn} onAnalyze={onAnalyze} />);

    await user.click(screen.getByRole("button", { name: "Regenerate analysis" }));
    expect(onAnalyze).toHaveBeenCalledWith(turn.id);

    rerender(
      <CortexAnalysisZone turn={{ ...turn, analysisStatus: "processing" }} onAnalyze={onAnalyze} />,
    );
    expect(screen.getByText("Reading the responses")).toBeInTheDocument();
    expect(screen.getByText("Comparing agreements and differences")).toBeInTheDocument();
    expect(screen.getByText("Building your combined answer")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.queryByText("Previously combined answer.")).not.toBeInTheDocument();
  });

  it("keeps every saved run user-visible and opens the newest by default", async () => {
    const user = userEvent.setup();
    const newest = analysisRun({
      analysisId: "analysis-new",
      recommendedAnswer: "Newest recommendation.",
      createdAt: "2026-07-27T12:00:00Z",
    });
    const older = analysisRun({
      analysisId: "analysis-old",
      recommendedAnswer: "Older recommendation.",
      createdAt: "2026-07-26T12:00:00Z",
    });
    const turn = {
      ...compareTurn(),
      analysisRuns: [newest, older],
    };

    render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} />);

    expect(screen.getByText("Newest recommendation.")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Analysis history"), "analysis-old");
    expect(screen.getByText("Older recommendation.")).toBeInTheDocument();
  });

  it("shows the saved Cortex Analysis AI-credit charge using display credit units", () => {
    const run = analysisRun({ aiCredits: 1_234, creditUsageEstimated: true });

    render(
      <CortexAnalysisZone
        turn={{ ...compareTurn(), analysisRuns: [run] }}
        onAnalyze={vi.fn()}
      />,
    );

    const creditAmount = screen.getByText("1.234 credits estimated");
    expect(creditAmount).toBeInTheDocument();
    expect(creditAmount.parentElement?.querySelector("svg")).toBeInTheDocument();
  });

  it("keeps a stale result readable after a response version changes", () => {
    const run = analysisRun();
    const turn = compareTurn();
    turn.responses[1] = {
      ...turn.responses[1]!,
      request_id: "response-b-regenerated",
      response_version: 2,
      text: "Regenerated response.",
    };
    turn.analysisRuns = [run];

    render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} />);

    expect(screen.getByText("One answer changed after this analysis")).toBeInTheDocument();
    expect(screen.getByText(run.recommendedAnswer)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update combined answer" })).toBeInTheDocument();
    expect(isCortexAnalysisRunStale(run, turn.responses)).toBe(true);
  });

  it("keeps the last saved run visible when an update fails", () => {
    const run = analysisRun();
    const turn = {
      ...compareTurn(),
      analysisRuns: [run],
      analysisStatus: "failed" as const,
      analysisError: "Cortex couldn't combine these answers. Your model responses are safe above.",
    };

    render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} />);

    expect(screen.getByText("Cortex couldn't combine these answers")).toBeInTheDocument();
    expect(screen.getByText(run.recommendedAnswer)).toBeInTheDocument();
  });

  it("renders the complete document without disclosures and leads with strong disagreement", () => {
    const run = analysisRun({
      disagreements: [
        {
          who: "ChatGPT (GPT-5.1)",
          text: "Recommends a path incompatible with Claude (Sonnet 4.5).",
        },
      ],
      disagreementNote: "These are different risk judgments, not conflicting facts.",
      agreements: ["Both identify the same constraint."],
      confidence: {
        level: "limited",
        reason: "The responses reach different recommendations.",
      },
      highStakesDomain: "financial",
      verify: ["Confirm current mortgage terms."],
    });
    const turn = { ...compareTurn(), analysisRuns: [run] };

    render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} />);

    const card = screen.getByRole("region", { name: "Cortex Analysis" });
    const differ = screen.getByRole("heading", { name: "Where they differ" });
    const agree = screen.getByRole("heading", { name: "What they agree on" });
    expect(differ.closest("section")).toHaveAttribute("data-cx-order", "0");
    expect(agree.closest("section")).toHaveAttribute("data-cx-order", "1");
    expect(card.querySelector("[aria-expanded]")).toBeNull();
    expect(
      screen.getByText("Recommends a path incompatible with Claude (Sonnet 4.5)."),
    ).toBeVisible();
    expect(
      screen.getByText("These are different risk judgments, not conflicting facts."),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Worth verifying · financial" })).toBeVisible();
    expect(screen.getByText("Confirm current mortgage terms.")).toBeInTheDocument();
  });
});

function compareTurn(): ChatTurn {
  const responses = [
    response("response-a", "openai", "gpt-5.1"),
    response("response-b", "claude", "claude-sonnet-4-5"),
  ];
  return {
    id: "compare-turn",
    mode: "compare",
    prompt: "Which approach should I use?",
    submittedPrompt: "Which approach should I use?",
    attachments: [],
    responses,
    status: "complete",
    createdAt: "2026-07-27T11:00:00Z",
    requestGroupId: "compare-group",
  };
}

function response(requestId: string, provider: string, model: string): ChatResponse {
  return {
    request_id: requestId,
    response_version: 1,
    text: `${provider} response`,
    provider,
    model,
    latency_ms: 100,
    token_usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
    estimated_cost: 0.001,
    cost_currency: "USD",
    web_source_items: [],
    timestamp: "2026-07-27T11:00:00Z",
    ui_status: "complete",
  };
}

function analysisRun(overrides: Partial<CortexAnalysisRun> = {}): CortexAnalysisRun {
  return {
    analysisId: "analysis-1",
    requestGroupId: "compare-group",
    sessionId: "session-1",
    model: "gpt-5.4-mini",
    recommendedAnswer: "Use a staged approach based on the shared constraints.",
    agreements: ["Both responses identify the same constraint."],
    disagreements: [],
    uniqueInsights: [],
    confidence: {
      level: "moderate",
      reason: "The responses align on the main tradeoff.",
    },
    verify: [],
    highStakesDomain: null,
    sourceFingerprint: "fingerprint",
    sourceResponses: [
      {
        requestId: "response-a",
        responseVersion: 1,
        responseName: "ChatGPT",
      },
      {
        requestId: "response-b",
        responseVersion: 1,
        responseName: "Claude",
      },
    ],
    combinedResponseCount: 2,
    failedResponseCount: 0,
    aiCredits: 180,
    creditUsageEstimated: false,
    createdAt: "2026-07-27T12:00:00Z",
    isStale: false,
    ...overrides,
    disagreementNote: overrides.disagreementNote ?? null,
  };
}
