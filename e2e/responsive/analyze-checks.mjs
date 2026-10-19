import { expect, expectNoHorizontalOverflow, restoreHistoryThread } from "./fixtures/responsive-e2e.mjs";

// Drive the real transcript reducer and read-only polling with controlled events.
// No simulated timing, model calls or progress logic is added to product code.
export async function checkAnalyzeProgress({ responsiveApp }, testInfo, width, heightOverride) {
  const { page, state } = responsiveApp;
  await page.setViewportSize({ width, height: heightOverride ?? (width < 768 ? 844 : 1000) });
  await restoreHistoryThread(page, "Architecture decision");
  const saved = state.analysisRuns.find(run => run.requestGroupId === "compare-group-history");
  const group = saved?.requestGroupId ?? state.analysisRuns[0].requestGroupId;
  const originalRuns = [...state.analysisRuns];
  state.analysisRuns = state.analysisRuns.filter(run => run.requestGroupId !== group);
  state.analysisStates.set(group, { state: "waiting", automatic: true, estimatedAiCredits: 1234 });
  await page.evaluate(async groupId => {
    const { useChatStore } = await import("/src/store/chatStore.ts");
    useChatStore.setState(current => ({ streaming: true, turns: current.turns.map(turn =>
      turn.requestGroupId !== groupId ? turn : { ...turn, analysisRuns: [], analysisStatus: "idle", status: "streaming",
        responses: turn.responses.map(response => ({ ...response, text: "", ui_status: "queued" })) }) }));
  }, group);
  const turn = page.locator('article[aria-label="Model comparison"]').first();
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();
  const progress = turn.getByRole("region", { name: "Cortex Analysis progress" });
  await expect(progress.getByText("Waiting to start")).toHaveCount(2);
  await expect(progress.locator("[data-provider-logo]")).toHaveCount(2);
  await page.evaluate(async groupId => {
    const { useChatStore } = await import("/src/store/chatStore.ts");
    useChatStore.setState(current => ({ turns: current.turns.map(turn => turn.requestGroupId !== groupId ? turn : {
      ...turn, responses: turn.responses.map((response, index) => ({ ...response, ui_status: index ? "streaming" : "requesting",
        text: index ? "An answer is arriving." : "", activity: index ? undefined : {
          request_id: response.request_id, mode: "compare", event_type: "SEARCH_STARTED", phase: "search",
          display_message: "Searching the web…", timestamp: new Date().toISOString(), sequence_number: 1, metadata: {},
        } })) }) }));
  }, group);
  await expect(progress.getByText("Searching the web…")).toBeVisible();
  await expect(progress.getByText("Writing its answer…")).toBeVisible();
  const spinner = progress.locator('[class*="spinner"]').first();
  await expect(spinner).toHaveCSS("animation-name", /analysis-spin/);
  await page.screenshot({ path: testInfo.outputPath(`waiting-${width}-light.png`), fullPage: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(spinner).toHaveCSS("animation-name", "none");
  await progress.getByRole("button", { name: "Model responses", exact: true }).click();
  await expect(turn.getByRole("tab", { name: /Model responses/ })).toHaveAttribute("aria-selected", "true");
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();

  state.analysisStates.set(group, { state: "queued", automatic: true, estimatedAiCredits: 1234 });
  await page.evaluate(async groupId => {
    const { useChatStore } = await import("/src/store/chatStore.ts");
    useChatStore.setState(current => ({ streaming: false, turns: current.turns.map(turn => turn.requestGroupId !== groupId ? turn : {
      ...turn, status: "complete", analysisStatus: "processing",
      responses: turn.responses.map(response => ({ ...response, text: "A complete model answer.", ui_status: "complete", activity: undefined })) }) }));
  }, group);
  const workflow = progress.getByRole("list", { name: "Analysis workflow" });
  await expect(workflow.locator('[aria-current="step"]')).toContainText("Comparing the answers");
  for (const [stage, title] of [["reading", "Reading the supporting passages"], ["synthesizing", "Bringing the main points together"], ["validating", "Checking the attributed quotes before saving"]]) {
    state.analysisStates.set(group, { state: "processing", progressStage: stage, automatic: true, estimatedAiCredits: 1234 });
    await expect(workflow.locator('[aria-current="step"]')).toContainText(title);
  }
  await page.screenshot({ path: testInfo.outputPath(`synthesis-${width}-light.png`), fullPage: true });
  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`synthesis-${width}-dark.png`), fullPage: true });
  state.analysisRuns = originalRuns;
  state.analysisStates.set(group, { state: "complete", automatic: true,
    analysisId: originalRuns.find(run => run.requestGroupId === group).analysisId, estimatedAiCredits: 1234 });
  await expect(turn.getByRole("heading", { name: "The takeaway" })).toBeVisible();
  await expect(progress).toHaveCount(0);
  await expect(turn.getByLabel("Analysis contributors")).toHaveCount(0);
  await expect(page.getByText(/Models \+ analysis:/)).toHaveCount(0);
  expect(state.analysisRequests).toHaveLength(0);
}

export async function checkAnalyze({ responsiveApp }, testInfo, width, heightOverride) {
  const { page, state } = responsiveApp;
  await page.setViewportSize({ width, height: heightOverride ?? (width < 768 ? 844 : 1000) });
  await restoreHistoryThread(page, "Architecture decision");
  const shellTheme = () => page.locator("main").evaluate(main => {
    const root = main.parentElement;
    const style = getComputedStyle(root);
    const sidebar = root.querySelector('aside[aria-label="Primary navigation"]');
    const mobileNav = root.querySelector('nav[aria-label="Mobile navigation"]');
    return {
      fontFamily: style.fontFamily,
      tokens: Object.fromEntries(["--font-sans", "--font-display", "--cx-canvas", "--cx-sidebar", "--cx-accent", "--cx-ink-900"].map(name => [name, style.getPropertyValue(name).trim()])),
      brandAssets: [...root.querySelectorAll('img[src*="brand-mark"]')].map(image => image.getAttribute("src")),
      sidebarWidth: sidebar.getBoundingClientRect().width,
      mobileNavigation: getComputedStyle(mobileNav).display,
    };
  });
  const analyzeTheme = await shellTheme();
  expect(analyzeTheme.tokens["--font-sans"]).toContain("Manrope");
  expect(analyzeTheme.tokens["--font-display"]).toContain("Space Grotesk");
  expect(analyzeTheme.brandAssets.length).toBeGreaterThan(0);
  const modes = page.getByRole("navigation", { name: width > 900 ? "Workspace mode" : "Mobile navigation" });
  await modes.getByRole("button", { name: "Ask", exact: true }).click();
  expect(await shellTheme(), "Analyze must preserve the original Cortex shell and theme").toEqual(analyzeTheme);
  await modes.getByRole("button", { name: "Analyze", exact: true }).click();
  const turn = page.locator('article[aria-label="Model comparison"]').first();
  await expect(turn.getByRole("tab", { name: "Analysis", exact: true })).toHaveAttribute("aria-selected", "true");
  await turn.getByRole("tab", { name: /Model responses/ }).click();
  await modes.getByRole("button", { name: "Ask", exact: true }).click();
  await modes.getByRole("button", { name: "Analyze", exact: true }).click();
  await expect(turn.getByRole("tab", { name: "Analysis", exact: true })).toHaveAttribute("aria-selected", "true");
  await turn.getByRole("tab", { name: /Model responses/ }).click();
  await page.locator('section[aria-label="Chat transcript"]').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`responses-${width}-light.png`), fullPage: true });
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();
  await expect(turn.getByRole("heading", { name: "The takeaway" })).toBeVisible();
  await expect(turn.getByRole("heading", { name: "In their words" })).toBeVisible();
  await expect(turn.getByRole("heading", { name: "Roll out in phases. Keep recovery observable." })).toBeVisible();
  await expect(turn.getByLabel("Analysis contributors")).toHaveCount(0);
  await expect(page.getByText(/Models \+ analysis:|Estimating model answers \+ automatic Cortex Analysis/)).toHaveCount(0);
  await expect(turn.getByRole("region", { name: "In their words", exact: true }).locator("[data-provider-logo]")).toHaveCount(2);
  const takeaway = turn.getByRole("region", { name: "The takeaway", exact: true });
  await expect(takeaway).toHaveCSS("background-color", "rgb(241, 238, 252)");
  const quotes = turn.getByRole("region", { name: "In their words", exact: true }).locator("blockquote");
  const quoteBounds = await quotes.evaluateAll(elements => elements.map(element => {
    const rect = element.getBoundingClientRect(); return { top: rect.top, left: rect.left };
  }));
  expect(quoteBounds).toHaveLength(2);
  if (width > 1000) expect(Math.abs(quoteBounds[0].top - quoteBounds[1].top)).toBeLessThanOrEqual(1);
  if (width < 700) expect(quoteBounds[1].top).toBeGreaterThan(quoteBounds[0].top);
  const difference = turn.getByRole("region", { name: "Meaningful difference" });
  await expect(difference.getByRole("heading", { name: "Where the rollout estimate stops." })).toBeVisible();
  await difference.getByText("See both passages", { exact: true }).click();
  await expect(difference.locator("details")).toHaveAttribute("open", "");
  await difference.getByRole("button", { name: "Read in context" }).first().click();
  await expect(turn.locator("mark")).toBeVisible();
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();
  await difference.getByText("See both passages", { exact: true }).click();
  expect(state.analysisRequests).toHaveLength(0);
  await expect(turn.getByText("1.234 AI credits", { exact: true })).toBeVisible();
  await page.locator('section[aria-label="Chat transcript"]').evaluate((element) => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`analyze-${width}-light.png`), fullPage: true });
  await turn.getByRole("heading", { name: "In their words" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath(`analyze-details-${width}-light.png`), fullPage: true });

  await turn.getByRole("tab", { name: /Sources/ }).click();
  await expect(turn.getByRole("heading", { name: "No linked sources supplied", exact: true })).toBeVisible();
  await expect(turn.getByRole("heading", { name: "Model passages", exact: true })).toBeVisible();
  await turn.getByRole("button", { name: "Read in context" }).last().click();
  await expect(turn.locator("mark")).toBeVisible();
  await expect(turn.getByRole("tab", { name: /Model responses/ })).toHaveAttribute("aria-selected", "true");
  if (width <= 760) {
    await expect(turn.getByRole("tablist", { name: "Compare model responses" }).getByRole("tab").nth(1)).toHaveAttribute("aria-selected", "true");
    await expect(turn.locator("[data-response-panel]").nth(0)).toBeHidden();
  } else if (width > 1000) {
    await expect(turn.locator("[data-response-panel]").nth(0)).toBeVisible();
    await expect(turn.locator("[data-response-panel]").nth(1)).toBeVisible();
  }
  const selectedResponse = turn.locator("[data-response-panel]").nth(width <= 760 ? 1 : 0);
  await selectedResponse.getByRole("button", { name: "Helpful response", exact: true }).click();
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();
  await turn.getByRole("tab", { name: /Model responses/ }).click();
  await expect(selectedResponse.getByRole("button", { name: "Helpful response", exact: true })).toHaveClass(/selectedAction/);
  await expectNoHorizontalOverflow(page);
  expect(state.analysisRequests).toHaveLength(0);

  await turn.getByRole("tab", { name: "Analysis", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(turn.getByRole("tab", { name: /Model responses/ })).toBeFocused();
  await page.keyboard.press("End");
  await expect(turn.getByRole("tab", { name: /Sources/ })).toBeFocused();
  expect(state.analysisRequests).toHaveLength(0);

  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
  await turn.getByRole("tab", { name: "Analysis", exact: true }).click();
  await expect(takeaway).toHaveCSS("background-color", "rgb(45, 40, 69)");
  await page.locator('section[aria-label="Chat transcript"]').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`analyze-${width}-dark.png`), fullPage: true });
  await turn.getByRole("tab", { name: /Model responses/ }).click();
  await page.locator('section[aria-label="Chat transcript"]').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`responses-${width}-dark.png`), fullPage: true });
  await expectNoHorizontalOverflow(page);
}
