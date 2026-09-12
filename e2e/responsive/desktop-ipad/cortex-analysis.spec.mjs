import {
  expect,
  expectNoHorizontalOverflow,
  restoreHistoryThread,
  test,
} from "../fixtures/responsive-e2e.mjs";

test("desktop restores Cortex Analysis below Compare responses and retains every run", async ({
  responsiveApp,
}) => {
  const { page, reload } = responsiveApp;
  await page.setViewportSize({ width: 1440, height: 900 });
  await restoreHistoryThread(page, "Architecture decision");

  const firstTurn = page
    .locator('article[aria-label="Model comparison"]')
    .first();
  const responsePanels = firstTurn.locator("[data-response-panel]");
  const analysisHeading = firstTurn.getByRole("heading", {
    name: "Cortex Analysis",
  });

  await expect(responsePanels).toHaveCount(2);
  await expect(responsePanels.nth(0)).toBeVisible();
  await expect(responsePanels.nth(1)).toBeVisible();
  await expect(analysisHeading).toBeVisible();
  const analysisCredits = firstTurn.getByText("1.234 credits", { exact: true });
  await expect(analysisCredits).toBeVisible();
  await expect(analysisCredits.locator("..").locator("svg")).toHaveCount(1);
  await expect(
    firstTurn.getByText("Use a phased gateway rollout", { exact: false }),
  ).toBeVisible();

  const positions = await firstTurn.evaluate((element) => {
    const responseGrid = element.querySelector(
      "[data-response-panel]",
    )?.parentElement;
    const analysis = [...element.querySelectorAll("h3")]
      .find((heading) => heading.textContent === "Cortex Analysis")
      ?.closest("section");
    return {
      responseBottom: responseGrid?.getBoundingClientRect().bottom ?? 0,
      analysisTop: analysis?.getBoundingClientRect().top ?? 0,
    };
  });
  expect(positions.analysisTop).toBeGreaterThanOrEqual(
    positions.responseBottom,
  );

  await firstTurn
    .getByRole("button", { name: "Regenerate analysis" })
    .click();
  await expect(firstTurn.getByLabel("Analysis history")).toBeVisible();
  await expect(
    firstTurn.getByLabel("Analysis history").locator("option"),
  ).toHaveCount(2);
  await expect(responsePanels.nth(0)).toBeVisible();
  await expect(responsePanels.nth(1)).toBeVisible();

  await reload();
  await expect(
    page
      .locator('article[aria-label="Model comparison"]')
      .first()
      .getByRole("heading", { name: "Cortex Analysis" }),
  ).toBeVisible();
  await expect(
    page
      .locator('article[aria-label="Model comparison"]')
      .first()
      .getByText("1.234 credits", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
