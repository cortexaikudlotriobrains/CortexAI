import { describe, expect, it } from "vitest";
import { DEFAULT_MODELS } from "../config/defaultModels";
import {
  compareReasoningLevels,
  generationForReasoningLevel,
  modelForKey,
  modelReasoningLevels,
  reasoningLevelFromResponse,
  smartReasoningLevels,
} from "../reasoning/reasoningLevels";

describe("reasoning levels", () => {
  it("normalizes model, Smart, and Compare capabilities", () => {
    const deepseek = modelForKey("deepseek:deepseek-v4-flash", DEFAULT_MODELS);

    expect(modelReasoningLevels(deepseek)).toEqual(["low", "high", "max"]);
    expect(smartReasoningLevels(DEFAULT_MODELS)).toEqual([
      "low",
      "medium",
      "high",
      "max",
    ]);
    expect(
      compareReasoningLevels(
        ["openai:gpt-5.6-luna", "deepseek:deepseek-v4-flash", ""],
        DEFAULT_MODELS,
      ),
    ).toEqual(["low", "high", "max"]);
  });

  it("builds the public API request without exposing provider-specific aliases", () => {
    expect(generationForReasoningLevel("auto")).toEqual({
      profile: "auto",
      reasoning: { mode: "auto", effort: "auto" },
    });
    expect(generationForReasoningLevel("max", "deep")).toEqual({
      profile: "deep",
      reasoning: { mode: "on", effort: "max" },
    });
  });

  it("maps provider response aliases back to the public scale", () => {
    expect(reasoningLevelFromResponse("minimal")).toBe("low");
    expect(reasoningLevelFromResponse("xhigh")).toBe("max");
    expect(reasoningLevelFromResponse(undefined)).toBe("auto");
  });
});
