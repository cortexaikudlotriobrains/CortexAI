import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposerOptionsMenu } from "../components/composer/ComposerOptionsMenu";
import { DEFAULT_MODELS } from "../config/defaultModels";
import type { ReasoningLevel } from "../types";

const lockedSelect = vi.fn();
const models = DEFAULT_MODELS.filter((model) =>
  ["openai:gpt-5.6-luna", "claude:claude-sonnet-5", "deepseek:deepseek-v4-flash"].includes(
    `${model.provider}:${model.model}`,
  ),
);

function Menu({ compare = false, prefs = true }: { compare?: boolean; prefs?: boolean }) {
  const [smart, setSmart] = useState(true);
  const [model, setModel] = useState("openai:gpt-5.6-luna");
  const [reasoning, setReasoning] = useState<ReasoningLevel>("auto");
  return (
    <ComposerOptionsMenu
      optimizeMode={false}
      onOptimizeToggle={vi.fn()}
      reasoningValue={reasoning}
      supportedReasoningLevels={["low", "medium", "high"]}
      reasoningContextLabel={compare ? "Selected models" : "Smart Ask"}
      onReasoningChange={setReasoning}
      modeLabel={compare ? "Compare" : ""}
      {...(prefs
        ? {
            models,
            smartOffModel: model,
            onSmartOffModelChange: setModel,
            currentModel: compare ? undefined : "deepseek:deepseek-v4-flash",
            lockedKeys: ["claude:claude-sonnet-5"],
            lockedLabels: { "claude:claude-sonnet-5": "Plus" },
            onLockedSelect: lockedSelect,
            ...(compare ? {} : { smartForNewChats: smart, onSmartForNewChatsChange: setSmart }),
          }
        : {})}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Smart-off options menu", () => {
  it("steps back with Escape, restores focus, and resets after closing", async () => {
    const user = userEvent.setup();
    render(<Menu />);
    const trigger = screen.getByRole("button", { name: "More options" });
    await user.click(trigger);
    const off = screen.getByRole("button", { name: /When Smart is off/ });
    await user.click(off);
    expect(screen.getByRole("button", { name: "Back to More options" })).toHaveFocus();
    const provider = screen.getByRole("option", { name: /ChatGPT/ });
    await user.click(provider);
    expect(screen.getByRole("button", { name: "Back to Providers" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("option", { name: /ChatGPT/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: /When Smart is off/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    await user.click(trigger);
    expect(screen.getByRole("switch", { name: /Smart in new chats/ })).toBeVisible();
  });

  it("saves the current model, shows confirmation for 1800ms, and updates exact copy", () => {
    vi.useFakeTimers();
    render(<Menu />);
    fireEvent.click(screen.getByRole("button", { name: "More options" }));
    fireEvent.click(screen.getByRole("button", { name: /When Smart is off/ }));
    fireEvent.click(
      within(screen.getByRole("listbox", { name: "In use now" })).getByRole("option"),
    );
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
    expect(screen.getByRole("button", { name: /When Smart is off/ })).toHaveTextContent(
      /Start on DeepSeek/,
    );
    fireEvent.click(screen.getByRole("switch", { name: /Smart in new chats/ }));
    expect(screen.getByRole("switch", { name: /Smart in new chats/ })).toHaveTextContent(
      "New chats start with Smart on",
    );
    expect(screen.getByRole("switch", { name: /Smart in new chats/ })).not.toHaveTextContent(
      "DeepSeek",
    );
    act(() => vi.advanceTimersByTime(1799));
    expect(screen.getByRole("status")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("opens upgrade for locked models, retaining the list and saved selection", async () => {
    const user = userEvent.setup();
    render(<Menu />);
    await user.click(screen.getByRole("button", { name: "More options" }));
    await user.click(screen.getByRole("button", { name: /When Smart is off/ }));
    await user.click(screen.getByRole("option", { name: /Claude/ }));
    await user.click(screen.getByRole("option", { name: /Claude Sonnet/ }));
    expect(lockedSelect).toHaveBeenCalledWith("claude:claude-sonnet-5");
    expect(screen.getByRole("listbox", { name: "Claude models" })).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows only the model preference in Compare and keeps other menu usages unchanged", async () => {
    const user = userEvent.setup();
    const view = render(<Menu compare />);
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.queryByRole("switch", { name: /Smart in new chats/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /When Smart is off/ })).toBeVisible();
    expect(screen.getByText("Shared across selected models")).toBeVisible();
    expect(screen.getByText("Compare", { exact: true })).toBeVisible();
    view.unmount();
    render(<Menu prefs={false} />);
    await user.click(screen.getByRole("button", { name: "More options" }));
    expect(screen.queryByText("When Smart is off")).not.toBeInTheDocument();
  });

  it("groups mobile options, uses five segments, traps focus, and always ends with the credit note", async () => {
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    const user = userEvent.setup();
    render(<Menu />);
    const trigger = screen.getByRole("button", { name: "More options" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "More options" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("Smart", { exact: true })).toBeVisible();
    expect(screen.getByText("This message")).toBeVisible();
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
    expect(dialog.textContent).toMatch(
      /Higher reasoning may take longer and use more AI credits\.$/,
    );
    screen.getByRole("radio", { name: "Medium" }).focus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Done" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(screen.getByRole("radio", { name: "Medium" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(document.body.style.overflow).toBe("");
  });
});
