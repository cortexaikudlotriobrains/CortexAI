import { useState } from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CompareSelector } from "../components/composer/CompareSelector";
import { DEFAULT_MODELS } from "../config/defaultModels";

const keys: [string, string, string] = ["openai:gpt-5.6-luna", "claude:claude-sonnet-5", ""];
afterEach(cleanup);
function Selector({
  maxTargets = 3,
  onLimit = vi.fn(),
}: {
  maxTargets?: number;
  onLimit?: () => void;
}) {
  const [selected, setSelected] = useState(keys);
  return (
    <CompareSelector
      models={DEFAULT_MODELS}
      keys={selected}
      maxTargets={maxTargets}
      onTargetLimit={onLimit}
      onChange={(index, key) =>
        setSelected((current) => {
          const next = [...current] as [string, string, string];
          next[index] = key;
          const compact = next.filter(Boolean);
          return [compact[0] || "", compact[1] || "", compact[2] || ""];
        })
      }
    />
  );
}
describe("Compare model menu", () => {
  it("keeps restored targets visible and lets a locked third target be removed", async () => {
    const locked = vi.fn();
    const change = vi.fn();
    render(
      <CompareSelector
        models={DEFAULT_MODELS}
        keys={[keys[0], keys[1], "grok:retired-model"]}
        onChange={change}
        lockedKeys={["grok:retired-model"]}
        onLockedModel={locked}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Models to compare" }));
    await userEvent.click(screen.getByRole("option", { name: /retired-model/ }));
    expect(change).toHaveBeenCalledWith(2, "");
    expect(locked).not.toHaveBeenCalled();
  });
  it("shows every selectable model and no Smart or preference row", async () => {
    render(<Selector />);
    await userEvent.click(screen.getByRole("button", { name: "Models to compare" }));
    const list = screen.getByRole("listbox", { name: "Compare models" });
    expect(within(list).getAllByRole("option")).toHaveLength(
      DEFAULT_MODELS.filter((model) => model.enabled && model.selectable !== false).length,
    );
    expect(screen.queryByRole("option", { name: /^Smart/ })).not.toBeInTheDocument();
    expect(screen.queryByText("New chats start with")).not.toBeInTheDocument();
    expect(screen.getByText("2/3")).toBeVisible();
    expect(
      screen.getByText(
        "Smart isn't available in Compare. Each model answers the same prompt side by side.",
      ),
    ).toBeVisible();
  });
  it("enforces two to three models while keeping the menu open", async () => {
    const user = userEvent.setup();
    render(<Selector />);
    await user.click(screen.getByRole("button", { name: "Models to compare" }));
    const selected = screen.getAllByRole("option", { selected: true });
    selected.forEach((row) => expect(row).toBeDisabled());
    const third = screen.getByRole("option", { name: /deepseek-v4-flash/ });
    await user.click(third);
    expect(screen.getByText("3/3")).toBeVisible();
    expect(screen.getByRole("button", { name: "Models to compare" })).toHaveTextContent("3 models");
    expect(
      screen
        .getAllByRole("option", { selected: false })
        .every((row) => row.hasAttribute("disabled")),
    ).toBe(true);
    await user.click(third);
    expect(screen.getByText("2/3")).toBeVisible();
  });
  it("keeps the third-slot upgrade flow available at the plan limit", async () => {
    const onLimit = vi.fn();
    render(<Selector maxTargets={2} onLimit={onLimit} />);
    await userEvent.click(screen.getByRole("button", { name: "Models to compare" }));
    await userEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(onLimit).toHaveBeenCalledOnce();
    expect(screen.getByText("2/3")).toBeVisible();
  });
  it("opens upgrade for a locked model without selecting it", async () => {
    const locked = vi.fn();
    const change = vi.fn();
    render(
      <CompareSelector
        models={DEFAULT_MODELS}
        keys={keys}
        onChange={change}
        lockedKeys={["deepseek:deepseek-v4-flash"]}
        onLockedModel={locked}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Models to compare" }));
    await userEvent.click(screen.getByRole("option", { name: /deepseek-v4-flash/ }));
    expect(locked).toHaveBeenCalledWith("deepseek:deepseek-v4-flash");
    expect(change).not.toHaveBeenCalled();
  });
});
