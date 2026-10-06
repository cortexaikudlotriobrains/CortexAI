import { describe, expect, it } from "vitest";
import { terminalResponseText } from "../streaming/terminalResponse";

describe("terminalResponseText", () => {
  it("preserves streamed text when the terminal response is empty", () => {
    expect(terminalResponseText("", "Gemini streamed answer")).toBe(
      "Gemini streamed answer",
    );
  });

  it("uses a non-empty authoritative terminal response", () => {
    expect(terminalResponseText("Final answer", "Partial answer")).toBe("Final answer");
  });

  it("returns an empty string when neither response contains text", () => {
    expect(terminalResponseText(undefined, undefined)).toBe("");
  });
});
