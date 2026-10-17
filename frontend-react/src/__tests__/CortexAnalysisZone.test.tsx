import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CortexAnalysisZone } from "../components/results/CortexAnalysisZone";
import { useResponseUiStore } from "../store/responseUiStore";
import type { ChatResponse, ChatTurn, CortexAnalysisRun } from "../types";

describe("CortexAnalysisZone", () => {
  afterEach(() => { cleanup(); useResponseUiStore.setState({ feedback: {} }); });

  it("tracks each live model through queue, search, writing and completion", async () => {
    const turn = compareTurn();
    turn.status = "streaming";
    turn.responses = turn.responses.map(item => ({ ...item, text: "", ui_status: "queued" }));
    const onResponses = vi.fn();
    const { rerender, container } = render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} onResponses={onResponses} />);
    expect(screen.getAllByText("Waiting to start")).toHaveLength(2);
    expect(screen.queryByRole("heading", { name: "The takeaway" })).not.toBeInTheDocument();
    expect(container.querySelectorAll('[data-provider-logo]')).toHaveLength(2);
    turn.responses[0] = { ...turn.responses[0]!, ui_status: "requesting", activity: {
      request_id: "response-a", mode: "compare", event_type: "SEARCH_STARTED", phase: "search",
      display_message: "Searching the web…", timestamp: "2026-07-27T11:00:01Z", sequence_number: 1, metadata: {},
    } };
    turn.responses[1] = { ...turn.responses[1]!, ui_status: "streaming", text: "Partial text" };
    rerender(<CortexAnalysisZone turn={{ ...turn }} onAnalyze={vi.fn()} onResponses={onResponses} />);
    expect(screen.getByText("Searching the web…")).toBeVisible();
    expect(screen.getByText("Writing its answer…")).toBeVisible();
    expect(screen.queryByText(/Answered in/)).not.toBeInTheDocument();
    turn.responses[0] = { ...turn.responses[0]!, ui_status: "complete", text: "Answer" };
    rerender(<CortexAnalysisZone turn={{ ...turn }} onAnalyze={vi.fn()} onResponses={onResponses} />);
    expect(screen.getByText("Answered in 0.1s")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Model responses" }));
    expect(onResponses).toHaveBeenCalledOnce();
  });

  it("advances the synthesis checklist only from server milestones", () => {
    const turn = { ...compareTurn(), analysisStatus: "processing" as const };
    const state = { state: "processing" as const, automatic: true, estimatedAiCredits: 1000 };
    const { rerender } = render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} analysisState={{ ...state, progressStage: "comparing" }} />);
    const workflow = screen.getByRole("list", { name: "Analysis workflow" });
    expect(within(workflow).getByText("Comparing the answers").closest("li")).toHaveAttribute("aria-current", "step");
    rerender(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} analysisState={{ ...state, progressStage: "reading" }} />);
    expect(within(workflow).getByText("Reading the supporting passages").closest("li")).toHaveAttribute("aria-current", "step");
    rerender(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} analysisState={{ ...state, progressStage: "synthesizing" }} />);
    expect(within(workflow).getByText("Bringing the main points together").closest("li")).toHaveAttribute("aria-current", "step");
    rerender(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} analysisState={{ ...state, progressStage: "validating" }} />);
    expect(within(workflow).getByText("Checking the attributed quotes before saving")).toBeVisible();
    rerender(<CortexAnalysisZone turn={{ ...turn, analysisStatus: "idle", analysisRuns: [v2Run()] }} onAnalyze={vi.fn()} analysisState={{ ...state, state: "complete" }} />);
    expect(screen.queryByRole("list", { name: "Analysis workflow" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The takeaway" })).toBeVisible();
  });

  it("keeps failed and stopped models actionable without marking them as answers", async () => {
    const turn = compareTurn();
    turn.responses[1] = { ...turn.responses[1]!, text: "", ui_status: "cancelled" };
    const onRetryResponse = vi.fn();
    render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} onRetryResponse={onRetryResponse} responseEstimates={{ "response-b": 1000 }} />);
    expect(screen.getByText("Only one model answered")).toBeVisible();
    expect(screen.getByText("Stopped")).toBeVisible();
    expect(screen.getByRole("region", { name: "Cortex Analysis progress" })).toHaveAttribute("aria-busy", "false");
    await userEvent.click(screen.getByRole("button", { name: /Retry.*1 AI credits estimated/ }));
    expect(onRetryResponse).toHaveBeenCalledWith(1);
  });

  it("discloses the cost before a direct legacy create action", async () => {
    const onAnalyze = vi.fn(async () => undefined);
    render(<CortexAnalysisZone turn={compareTurn()} onAnalyze={onAnalyze} estimatedCredits={1200} />);
    await userEvent.click(screen.getByRole("button", { name: /Create analysis.*1.2 AI credits estimated/ }));
    expect(onAnalyze).toHaveBeenCalledWith("compare-turn");
  });

  it("keeps a valid analysis visible while an update is pending and after failure", () => {
    const turn = { ...compareTurn(), analysisRuns: [analysisRun()], analysisStatus: "processing" as const };
    const { rerender } = render(<CortexAnalysisZone turn={turn} onAnalyze={vi.fn()} estimatedCredits={1000} />);
    expect(screen.getByText(/Your previous analysis remains below/)).toBeVisible();
    expect(screen.getByText(turn.analysisRuns[0]!.recommendedAnswer)).toBeVisible();
    rerender(<CortexAnalysisZone turn={{ ...turn, analysisStatus: "failed" }} onAnalyze={vi.fn()} estimatedCredits={1000} />);
    expect(screen.getByText(turn.analysisRuns[0]!.recommendedAnswer)).toBeVisible();
    expect(screen.getByRole("button", { name: /Retry analysis.*1 AI credits estimated/ })).toBeEnabled();
  });

  it("preserves legacy history without inventing evidence or confidence badges", () => {
    render(<CortexAnalysisZone turn={{ ...compareTurn(), analysisRuns: [analysisRun()] }} onAnalyze={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "The takeaway" })).toBeVisible();
    expect(screen.getByText(/No evidence quotations were stored/)).toBeVisible();
    expect(screen.queryByText(/moderate confidence/i)).not.toBeInTheDocument();
  });

  it("shows visible attributed exact quotations and navigates by revision", async () => {
    const run = v2Run();
    const onReadPassage = vi.fn();
    render(<CortexAnalysisZone turn={{ ...compareTurn(), analysisRuns: [run] }} onAnalyze={vi.fn()} onReadPassage={onReadPassage} />);
    expect(screen.getByRole("heading", { name: "In their words" })).toBeVisible();
    expect(screen.getByText("“openai response”")).toBeVisible();
    await userEvent.click(screen.getAllByRole("button", { name: "Read in context" })[1]!);
    expect(onReadPassage).toHaveBeenCalledWith(run.structuredResult!.passages[1]);
  });

  it("marks stale revisions and discloses an explicit update cost", async () => {
    const turn = compareTurn(); turn.responses[0] = { ...turn.responses[0]!, response_version: 2 };
    render(<CortexAnalysisZone turn={{ ...turn, analysisRuns: [v2Run()] }} onAnalyze={vi.fn()} estimatedCredits={2300} />);
    expect(screen.getByText(/This analysis uses earlier revisions/)).toBeVisible();
    expect(screen.getByRole("button", { name: /Update analysis.*2.3 AI credits estimated/ })).toBeEnabled();
  });

  it("renders the editorial overview, expandable difference and real model identities", async () => {
    const run = v2Run();
    run.structuredResult!.headline = "Start small. Keep recovery observable.";
    run.structuredResult!.introduction = "A phased rollout keeps the migration reversible.";
    run.structuredResult!.differences = [{ title: "How far the estimate goes.",
      text: "One answer quantifies the estimate; the other leaves it open.",
      positions: run.structuredResult!.passages.map(passage => ({ text: passage.quote, evidence: [passage] })) }];
    const onSources = vi.fn();
    const onReadPassage = vi.fn();
    const { container } = render(<CortexAnalysisZone turn={{ ...compareTurn(), analysisRuns: [run] }} onAnalyze={vi.fn()} onSources={onSources} onReadPassage={onReadPassage} linkedSourceCount={2} />);
    expect(screen.getByRole("heading", { name: run.structuredResult!.headline! })).toBeVisible();
    expect(screen.getByText(run.structuredResult!.introduction!)).toBeVisible();
    expect(screen.getByRole("heading", { name: "How far the estimate goes." })).toBeVisible();
    expect(container.querySelectorAll('img[data-provider-logo="openai"]').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('img[data-provider-logo="claude"]').length).toBeGreaterThan(0);
    const disclosure = screen.getByText("See both passages").closest("details")!;
    expect(disclosure).not.toHaveAttribute("open");
    await userEvent.click(screen.getByText("See both passages"));
    expect(disclosure).toHaveAttribute("open");
    await userEvent.click(screen.getAllByRole("button", { name: "Read in context" })[0]!);
    expect(onReadPassage).toHaveBeenCalledWith(run.structuredResult!.passages[0]);
    expect(screen.getByText("2 linked sources")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Sources & model passages" }));
    expect(onSources).toHaveBeenCalledOnce();
  });

  it("does not authorize paid work on render or saved-run selection", async () => {
    const onAnalyze = vi.fn();
    render(<CortexAnalysisZone turn={{ ...compareTurn(), analysisRuns: [v2Run(), analysisRun({ analysisId: "older" })] }} onAnalyze={onAnalyze} />);
    await userEvent.selectOptions(screen.getByLabelText("Saved analysis"), "older");
    expect(onAnalyze).not.toHaveBeenCalled();
  });

  it("retains feedback for the saved run across view unmounts", async () => {
    const props = { turn: { ...compareTurn(), analysisRuns: [v2Run()] }, onAnalyze: vi.fn() };
    const first = render(<CortexAnalysisZone {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "Good analysis" }));
    first.unmount(); render(<CortexAnalysisZone {...props} />);
    expect(screen.getByRole("button", { name: "Good analysis" })).toHaveAttribute("aria-pressed", "true");
  });
});

function v2Run(): CortexAnalysisRun {
  return analysisRun({ structuredResult: { schemaVersion: 2,
    takeaway: "Use a staged approach based on the shared constraints.",
    passages: [
      { requestId: "response-a", responseVersion: 1, provider: "openai", model: "gpt-5.1", quote: "openai response" },
      { requestId: "response-b", responseVersion: 1, provider: "claude", model: "claude-sonnet-4-5", quote: "claude response" },
    ], points: [], differences: [], limitations: [], linkedSources: [] } });
}

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
