import { test } from "../fixtures/responsive-e2e.mjs";
import { checkAnalyze, checkAnalyzeProgress } from "../analyze-checks.mjs";
import { checkSourceAttribution } from "../source-checks.mjs";
for (const width of [320, 390]) {
  test(`Analyze preserves evidence and actions at ${width}px`, async ({ responsiveApp }, info) => checkAnalyze({ responsiveApp }, info, width));
  test(`Analyze shows live model and synthesis progress at ${width}px`, async ({ responsiveApp }, info) => checkAnalyzeProgress({ responsiveApp }, info, width));
  test(`Analyze sources show attributed citation passages at ${width}px`, async ({ responsiveApp }, info) => checkSourceAttribution({ responsiveApp }, info, width));
}

test("Analyze keeps actions accessible in phone landscape", async ({ responsiveApp }, info) =>
  checkAnalyze({ responsiveApp }, info, 844, 390));
test("Analyze keeps live progress accessible in phone landscape", async ({ responsiveApp }, info) =>
  checkAnalyzeProgress({ responsiveApp }, info, 844, 390));
test("Analyze sources keep citation passages accessible in phone landscape", async ({ responsiveApp }, info) =>
  checkSourceAttribution({ responsiveApp }, info, 844, 390));
