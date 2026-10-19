import { expect, expectNoHorizontalOverflow, restoreHistoryThread } from "./fixtures/responsive-e2e.mjs";

export async function checkSourceAttribution({ responsiveApp }, testInfo, width, heightOverride) {
  const { page, state } = responsiveApp;
  await page.setViewportSize({ width, height: heightOverride ?? (width < 768 ? 844 : 1000) });
  const privateSource = { title: "Private markets investment report", url: "https://example.com/private?year=2026" };
  const borrowingSource = { title: "Financing the infrastructure buildout", url: "https://example.com/borrowing" };
  const backgroundSource = { title: "Attached background metadata", url: "https://example.com/background" };
  const rows = state.history.filter(row => row.request_group_id === "compare-group-1");
  const openai = rows.find(row => row.provider === "openai");
  const claude = rows.find(row => row.provider === "claude");
  const oldText = "The private investment figure measures money raised, not infrastructure spending.[1]\n\nBorrowing, leases and guarantees differ from cash already spent.[2]";
  const claudeText = "The announcement includes commitments rather than completed spending.[1]";
  openai.response_version = 2;
  openai.response = "The revised answer discusses borrowing first.[1]\n\nThe revised private total remains money raised.[2]";
  openai.web_source_items = [borrowingSource, privateSource];
  claude.response = claudeText;
  claude.web_source_items = [privateSource, backgroundSource, { title: "Unsafe", url: "javascript:alert(1)" }];
  const run = state.analysisRuns.find(item => item.requestGroupId === "compare-group-1");
  run.sourceResponses = [
    { requestId: openai.request_id, responseVersion: 1, provider: openai.provider, model: openai.model, responseName: "GPT-5.1", content: oldText, webSources: [privateSource, borrowingSource] },
    { requestId: claude.request_id, responseVersion: 1, provider: claude.provider, model: claude.model, responseName: "Claude Sonnet", content: claudeText, webSources: [privateSource, backgroundSource] },
  ];
  run.structuredResult.linkedSources = [];
  run.structuredResult.passages = run.sourceResponses.map(source => ({ requestId: source.requestId, responseVersion: source.responseVersion,
    provider: source.provider, model: source.model, quote: source.content.split("\n\n")[0] }));
  await restoreHistoryThread(page, "Architecture decision");
  const turn = page.locator('article[aria-label="Model comparison"]').first();
  const sourcesTab = turn.getByRole("tab", { name: "Sources 3", exact: true });
  await sourcesTab.click();
  await expect(turn.getByRole("heading", { name: "Sources & model passages" })).toBeVisible();
  const card = turn.getByRole("article", { name: privateSource.title, exact: true });
  await expect(card.getByText("Cited for", { exact: true })).toHaveCount(3);
  await expect(card.getByText("The private investment figure measures money raised, not infrastructure spending.", { exact: true })).toBeVisible();
  await expect(card.getByText("The revised private total remains money raised.", { exact: true })).toBeVisible();
  await expect(card.getByText("The announcement includes commitments rather than completed spending.", { exact: true })).toBeVisible();
  await expect(card.getByRole("link", { name: "Open original source" })).toHaveAttribute("href", privateSource.url);
  await expect(turn.locator('a[href^="javascript:"]')).toHaveCount(0);
  await page.locator('section[aria-label="Chat transcript"]').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`sources-${width}-light.png`), fullPage: true });

  await card.locator(`[data-source-response="${openai.request_id}:1"]`).getByRole("button").click();
  await expect(turn.getByRole("tab", { name: /Model responses/ })).toHaveAttribute("aria-selected", "true");
  await expect(turn.locator("mark")).toHaveText("The private investment figure measures money raised, not infrastructure spending.[1]");
  await expect(turn.getByText("Earlier revision used by this analysis.", { exact: false })).toBeVisible();
  await expect(turn.locator('[id^="response-text-"]', { hasText: "The revised private total" })).toHaveCount(0);
  await expect(turn.locator("mark").locator("xpath=ancestor::div[@tabindex='-1']")).toBeFocused();
  await turn.getByRole("button", { name: "Back to Sources", exact: true }).click();
  await expect(sourcesTab).toBeFocused();

  await card.locator(`[data-source-response="${claude.request_id}:1"]`).getByRole("button").click();
  await expect(turn.locator("mark")).toHaveText(claudeText);
  if (width <= 760) {
    await expect(turn.getByRole("tablist", { name: "Compare model responses" }).getByRole("tab").nth(1)).toHaveAttribute("aria-selected", "true");
    await expect(turn.locator("[data-response-panel]").nth(0)).toBeHidden();
  }
  await turn.getByRole("button", { name: "Back to Sources", exact: true }).click();
  const metadata = turn.getByRole("article", { name: backgroundSource.title, exact: true });
  await expect(metadata.getByText("Cited for", { exact: true })).toHaveCount(0);
  await expect(metadata.getByText(/No specific cited passage/)).toBeVisible();
  await metadata.getByRole("button", { name: /View the model’s answer/ }).click();
  await expect(turn.locator("mark")).toHaveCount(0);
  await turn.getByRole("button", { name: "Back to Sources", exact: true }).click();
  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
  await page.locator('section[aria-label="Chat transcript"]').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: testInfo.outputPath(`sources-${width}-dark.png`), fullPage: true });
  await expectNoHorizontalOverflow(page);
  expect(state.analysisRequests).toHaveLength(0);
}
