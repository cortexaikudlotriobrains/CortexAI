import { test } from "../fixtures/responsive-e2e.mjs";
import { checkAnalyze, checkAnalyzeProgress } from "../analyze-checks.mjs";
import { checkSourceAttribution } from "../source-checks.mjs";
for (const width of [768, 1024, 1440]) {
  test(`Analyze preserves evidence and actions at ${width}px`, async ({ responsiveApp }, info) => checkAnalyze({ responsiveApp }, info, width));
  test(`Analyze shows live model and synthesis progress at ${width}px`, async ({ responsiveApp }, info) => checkAnalyzeProgress({ responsiveApp }, info, width));
  test(`Analyze sources show attributed citation passages at ${width}px`, async ({ responsiveApp }, info) => checkSourceAttribution({ responsiveApp }, info, width));
}
