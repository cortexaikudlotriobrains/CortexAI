import { describe, expect, it } from "vitest";
import {
  CREDIT_UNITS_PER_DISPLAY_CREDIT,
  formatAiCredits,
  toDisplayAiCredits,
} from "../utils/aiCredits";

describe("AI-credit presentation", () => {
  it("uses one displayed AI credit per 1,000 backend metering units", () => {
    expect(CREDIT_UNITS_PER_DISPLAY_CREDIT).toBe(1_000);
    expect(toDisplayAiCredits(1_000)).toBe(1);
  });

  it.each([
    [0, "0"],
    [1, "0.001"],
    [916, "0.916"],
    [999, "0.999"],
    [1_000, "1"],
    [1_234, "1.234"],
    [10_000, "10"],
    [100_000, "100"],
    [1_000_000, "1,000"],
    [3_000_000, "3,000"],
  ])("formats %i raw units as %s AI credits", (creditUnits, expected) => {
    expect(formatAiCredits(creditUnits)).toBe(expected);
  });
});
