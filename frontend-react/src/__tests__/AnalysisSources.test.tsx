import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalysisSources } from "../components/results/AnalysisSources";
import { collectAnalysisSources } from "../analysis/analysisSources";
import type { ChatResponse } from "../types";

const response = {
  request_id: "claude",
  response_version: 3,
  provider: "claude",
  model: "claude-sonnet-4-5",
  text: "Investment measures money raised, not infrastructure spending.[1]",
  web_source_items: [
    { title: "Private markets report", url: "https://example.com/report?year=2026" },
    { title: "Background metadata", url: "https://example.com/background" },
  ],
} as ChatResponse;

describe("AnalysisSources", () => {
  afterEach(cleanup);
  it("shows Cited for with the exact model context and opens its immutable revision", async () => {
    const onRead = vi.fn();
    render(
      <AnalysisSources
        sources={collectAnalysisSources([response], undefined, true)}
        passages={[]}
        onRead={onRead}
      />,
    );
    const card = screen.getByRole("article", { name: "Private markets report" });
    expect(within(card).getByText("Cited for")).toBeVisible();
    expect(
      within(card).getByText("Investment measures money raised, not infrastructure spending."),
    ).toBeVisible();
    expect(within(card).getByRole("link", { name: "Open original source" })).toHaveAttribute(
      "href",
      "https://example.com/report?year=2026",
    );
    await userEvent.click(
      within(card).getByRole("button", { name: /View the model’s answer.*revision 3/ }),
    );
    expect(onRead).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "claude", responseVersion: 3, quote: response.text }),
    );
  });
  it("keeps an attached uncited link honest and still opens its model answer", async () => {
    const onRead = vi.fn();
    render(
      <AnalysisSources
        sources={collectAnalysisSources([response], undefined, true)}
        passages={[]}
        onRead={onRead}
      />,
    );
    const card = screen.getByRole("article", { name: "Background metadata" });
    expect(within(card).queryByText("Cited for")).not.toBeInTheDocument();
    expect(within(card).getByText(/No specific cited passage/)).toBeVisible();
    await userEvent.click(within(card).getByRole("button", { name: /View the model’s answer/ }));
    expect(onRead).toHaveBeenCalledWith(
      expect.objectContaining({ requestId: "claude", responseVersion: 3, passages: [] }),
    );
  });
  it("disables navigation when the referenced response text is unavailable", () => {
    const details = collectAnalysisSources([response], undefined, true);
    details[0]!.references[0]!.available = false;
    details[0]!.references[0]!.passages = [];
    render(<AnalysisSources sources={details} passages={[]} onRead={vi.fn()} />);
    expect(
      within(screen.getByRole("article", { name: "Private markets report" })).getByRole("button"),
    ).toBeDisabled();
  });
});
