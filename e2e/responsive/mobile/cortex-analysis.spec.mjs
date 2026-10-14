import {
  expect,
  expectNoHorizontalOverflow,
  restoreHistoryThread,
  test,
} from "../fixtures/responsive-e2e.mjs";

test("mobile keeps Cortex Analysis below the active answer without adding a model tab", async ({
  responsiveApp,
}, testInfo) => {
  const { page } = responsiveApp;
  await page.setViewportSize({ width: 390, height: 844 });
  await restoreHistoryThread(page, "Architecture decision");

  const firstTurn = page
    .locator('article[aria-label="Model comparison"]')
    .first();
  const tabs = firstTurn.getByRole("tablist", {
    name: "Compare model responses",
  });
  const analysisHeading = firstTurn.getByRole("heading", {
    name: "Cortex Analysis",
  });

  await expect(tabs.getByRole("tab")).toHaveCount(2);
  await expect(tabs.getByRole("tab", { name: /Cortex/i })).toHaveCount(0);
  await expect(analysisHeading).toBeVisible();
  const analysisCredits = firstTurn.getByText("1.234 credits", { exact: true });
  await expect(analysisCredits).toBeVisible();
  await analysisCredits.locator("..").screenshot({ path: testInfo.outputPath("analysis-credit-usage.png") });

  const positions = await firstTurn.evaluate((element) => {
    const visiblePanel = [
      ...element.querySelectorAll('[role="tabpanel"]'),
    ].find((panel) => !panel.hasAttribute("hidden"));
    const analysis = [...element.querySelectorAll("h3")]
      .find((heading) => heading.textContent === "Cortex Analysis")
      ?.closest("section");
    return {
      answerBottom: visiblePanel?.getBoundingClientRect().bottom ?? 0,
      analysisTop: analysis?.getBoundingClientRect().top ?? 0,
    };
  });
  expect(positions.analysisTop).toBeGreaterThanOrEqual(positions.answerBottom);
  await expectNoHorizontalOverflow(page);
});
