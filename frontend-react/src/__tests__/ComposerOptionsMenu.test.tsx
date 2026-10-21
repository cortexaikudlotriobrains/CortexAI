import { useState } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposerOptionsMenu } from "../components/composer/ComposerOptionsMenu";
import type { ReasoningLevel } from "../types";

function Menu({ context = "Smart Ask" }: { context?: string }) {
  const [reasoning, setReasoning] = useState<ReasoningLevel>("auto");
  const [improve, setImprove] = useState(false);
  return (
    <ComposerOptionsMenu
      optimizeMode={improve}
      onOptimizeToggle={setImprove}
      reasoningValue={reasoning}
      supportedReasoningLevels={["low", "medium", "high"]}
      reasoningContextLabel={context}
      onReasoningChange={setReasoning}
    />
  );
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
describe("More options", () => {
  it.each([
    ["Smart Ask", "Applied to Smart routing"],
    ["Selected models", "Applied to every compared model"],
    ["Gemini Flash", "Applied to Gemini Flash"],
  ])("uses the exact %s reasoning context", async (context, text) => {
    render(<Menu context={context} />);
    await userEvent.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.getByText(text)).toBeVisible();
    expect(screen.queryByText("Smart in new chats")).not.toBeInTheDocument();
    expect(screen.queryByText("When Smart is off")).not.toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(5);
  });
  it("supports keyboard reasoning selection and restores trigger focus", async () => {
    const user = userEvent.setup();
    render(<Menu />);
    const trigger = screen.getByRole("button", { name: "More options" });
    await user.click(trigger);
    screen.getByRole("radio", { name: /^Auto/ }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("radio", { name: /^Low/ })).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });
  it("shows mobile segments, traps focus, and closes with Done", async () => {
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    const user = userEvent.setup();
    render(<Menu />);
    const trigger = screen.getByRole("button", { name: "More options" });
    await user.click(trigger);
    expect(screen.getByRole("dialog")).toHaveAttribute("data-layout", "sheet");
    expect(screen.getByText("THIS MESSAGE")).toBeVisible();
    expect(screen.getAllByRole("radio").map((radio) => radio.textContent)).toEqual([
      "Auto",
      "Low",
      "Med",
      "High",
      "Max",
    ]);
    expect(screen.getByRole("radio", { name: "Maximum" })).toBeDisabled();
    await user.click(screen.getByRole("radio", { name: "Medium" }));
    expect(screen.getByText("Balanced speed and depth")).toBeVisible();
    await user.tab();
    expect(screen.getByRole("button", { name: "Done" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("radio", { name: "Medium" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(document.body.style.overflow).toBe("");
  });
});
