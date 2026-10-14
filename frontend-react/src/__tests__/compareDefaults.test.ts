import { describe, expect, it } from "vitest";
import {
  removeCompareModelKey,
  resolveAddedCompareModelKey,
  resolveCompareModelKeys,
  newCompareModelKeys,
} from "../config/compareDefaults";
import type { ModelCatalogItem } from "../types";

describe("compare model defaults", () => {
  it("starts new Compare sessions on the Smart-off model, independent of catalog order", () => {
    const models = [
      model("claude", "claude-haiku-4-5"),
      model("deepseek", "deepseek-v4-flash"),
      model("claude", "claude-sonnet-5"),
      model("openai", "gpt-5.6-luna"),
    ];

    expect(newCompareModelKeys(models, "deepseek:deepseek-v4-flash")).toEqual([
      "deepseek:deepseek-v4-flash",
      "claude:claude-sonnet-5",
      "",
    ]);
  });

  it("preserves valid manual selections", () => {
    const models = [
      model("gemini", "gemini-3.6-flash"),
      model("openai", "gpt-5.6-luna"),
      model("claude", "claude-sonnet-5"),
    ];

    expect(
      resolveCompareModelKeys(models, ["gemini:gemini-3.6-flash", "claude:claude-sonnet-5", ""]),
    ).toEqual(["gemini:gemini-3.6-flash", "claude:claude-sonnet-5", ""]);
  });

  it("uses only plan-eligible models when filling empty default slots", () => {
    const models = [
      model("openai", "gpt-5.6-luna", "economical"),
      model("claude", "claude-sonnet-5", "advanced"),
      model("deepseek", "deepseek-v4-flash", "economical"),
    ];
    const eligibleDefaults = models.filter((candidate) =>
      ["economical", "standard"].includes(candidate.billing_class),
    );

    expect(resolveCompareModelKeys(models, ["", "", ""], eligibleDefaults)).toEqual([
      "openai:gpt-5.6-luna",
      "deepseek:deepseek-v4-flash",
      "",
    ]);
    expect(models).toHaveLength(3);
  });

  it("does not rewrite a valid manual selection when defaults are plan-filtered", () => {
    const models = [
      model("openai", "gpt-5.6-luna", "economical"),
      model("claude", "claude-sonnet-5", "advanced"),
      model("deepseek", "deepseek-v4-flash", "economical"),
    ];
    const eligibleDefaults = models.filter((candidate) =>
      ["economical", "standard"].includes(candidate.billing_class),
    );

    expect(
      resolveCompareModelKeys(models, ["claude:claude-sonnet-5", "", ""], eligibleDefaults),
    ).toEqual(["claude:claude-sonnet-5", "openai:gpt-5.6-luna", ""]);
  });

  it("uses DeepSeek V4 Flash when a third model is added", () => {
    const models = [
      model("openai", "gpt-5.6-luna"),
      model("claude", "claude-sonnet-5"),
      model("deepseek", "deepseek-v4-flash"),
    ];

    expect(
      resolveAddedCompareModelKey(models, ["openai:gpt-5.6-luna", "claude:claude-sonnet-5", ""]),
    ).toBe("deepseek:deepseek-v4-flash");
  });

  it("falls back to available distinct models when a preference is disabled", () => {
    const models = [
      model("gemini", "gemini-3.6-flash"),
      model("claude", "claude-haiku-4-5"),
      model("grok", "grok-4"),
    ];

    expect(resolveCompareModelKeys(models, ["", "", ""])).toEqual([
      "gemini:gemini-3.6-flash",
      "claude:claude-haiku-4-5",
      "",
    ]);
    expect(
      resolveAddedCompareModelKey(models, [
        "gemini:gemini-3.6-flash",
        "claude:claude-haiku-4-5",
        "",
      ]),
    ).toBe("grok:grok-4");
  });

  it.each([
    [0, ["claude:claude-sonnet-5", "deepseek:deepseek-v4-flash", ""]],
    [1, ["openai:gpt-5.6-luna", "deepseek:deepseek-v4-flash", ""]],
    [2, ["openai:gpt-5.6-luna", "claude:claude-sonnet-5", ""]],
  ] as const)("removes and compacts compare slot %i", (removeIndex, expected) => {
    expect(
      removeCompareModelKey(
        ["openai:gpt-5.6-luna", "claude:claude-sonnet-5", "deepseek:deepseek-v4-flash"],
        removeIndex,
      ),
    ).toEqual(expected);
  });
});

function model(
  provider: string,
  name: string,
  billingClass: ModelCatalogItem["billing_class"] = "advanced",
): ModelCatalogItem {
  return {
    provider,
    model: name,
    tier: "frontier",
    billing_class: billingClass,
    input_cost_per_1m: 0,
    output_cost_per_1m: 0,
    context_limit: 128000,
    tags: [],
    enabled: true,
    supports_image_input: false,
    supported_attachment_mime_types: [],
  };
}
