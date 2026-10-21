import { readFileSync } from "node:fs";
import { test, expect, expectNoHorizontalOverflow } from "./responsive-e2e.mjs";

// Exercise the current bundled catalog as well as the generic API-provided list.
const source = readFileSync(new URL("../../../frontend-react/src/config/generated/registryDefaults.ts", import.meta.url), "utf8");
const catalog = JSON.parse(source.match(/export const FALLBACK_MODELS[^=]*= (\[[\s\S]*\]);/)[1]);
const key = model => `${model.provider}:${model.model}`;
const first = key(catalog[0]);
const third = key(catalog.find(model => model.provider === "deepseek"));
async function prepare(app, plan = "pro") {
  app.state.models = [...structuredClone(catalog), ...app.state.models.filter(model => !catalog.some(item => key(item) === key(model)))]; app.state.subscriptionPlan = plan; await app.reload();
}
async function mode(page, name) {
  const desktop = page.getByRole("navigation", { name: "Workspace mode" });
  const nav = await desktop.isVisible() ? desktop : page.getByRole("navigation", { name: "Mobile navigation" });
  await nav.getByRole("button", { name, exact: true }).click();
}
async function pick(page, modelKey) {
  await page.getByRole("button", { name: /^Model:/ }).click();
  await page.getByRole("button", { name: "All models", exact: true }).click();
  await page.locator(`[data-model-key="${modelKey}"]`).click();
}

async function expectCompleteModelViews(page, expected) {
  const expectedKeys = expected.map(key).sort();
  await page.getByRole("button", { name: /^Model:/ }).click();
  await page.getByRole("button", { name: "All models", exact: true }).click();
  const all = page.getByRole("listbox", { name: "All models", exact: true });
  expect(await all.locator("[data-model-key]").evaluateAll(rows => rows.map(row => row.dataset.modelKey).sort())).toEqual(expectedKeys);
  for (const provider of ["openai", "gemini", "claude", "grok", "deepseek"]) await expect(all.getByText(provider.toUpperCase(), { exact: true })).toBeAttached();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /New chats start with/ }).click();
  const preference = page.getByRole("listbox", { name: "New chats start with", exact: true });
  expect(await preference.locator("[data-model-key]").evaluateAll(rows => rows.map(row => row.dataset.modelKey).sort())).toEqual(expectedKeys);
  await page.keyboard.press("Escape"); await page.keyboard.press("Escape");
  await mode(page, "Compare");
  await page.getByRole("button", { name: "Models to compare" }).click();
  expect(await page.getByRole("listbox", { name: "Compare models" }).locator("[data-model-key]").evaluateAll(rows => rows.map(row => row.dataset.modelKey).sort())).toEqual(expectedKeys);
  await page.keyboard.press("Escape"); await mode(page, "Ask");
}

test("all model views retain all five providers when the catalog request fails", async ({ responsiveApp }, testInfo) => {
  const { page, state } = responsiveApp;
  state.subscriptionPlan = "pro";
  await page.route("**/v1/model-options", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ code: "catalog_unavailable", message: "Temporarily unavailable" }) }));
  await responsiveApp.reload();
  await expectCompleteModelViews(page, catalog);
  await page.getByRole("button", { name: /^Model:/ }).click(); await page.getByRole("button", { name: "All models", exact: true }).click();
  await page.screenshot({ path: testInfo.outputPath("complete-catalog-top.png") });
  await page.getByRole("listbox", { name: "All models", exact: true }).locator("[data-model-key]").last().scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("complete-catalog-bottom.png") });
});

test("all model views retain every row of a 21-model server catalog", async ({ responsiveApp }) => {
  const { state } = responsiveApp;
  await prepare(responsiveApp);
  state.models = state.models.slice(0, 21);
  expect(state.models).toHaveLength(21);
  await responsiveApp.reload();
  await expectCompleteModelViews(responsiveApp.page, state.models);
});

test("lean model menu preserves chat selection, recents and new-chat preference boundaries", async ({ responsiveApp }) => {
  const { page } = responsiveApp; await prepare(responsiveApp);
  await page.getByRole("button", { name: "Model: Smart" }).click();
  await page.getByRole("button", { name: /New chats start with/ }).click();
  await expect(page.getByText("Applies to new chats. This chat keeps what you picked above.")).toBeVisible();
  await page.locator(`[data-model-key="${third}"]`).click();
  await expect(page.getByRole("button", { name: "Model: Smart" })).toBeAttached();
  await page.keyboard.press("Escape");
  await pick(page, first);
  await expect(page.locator("#promptInput")).toBeFocused();
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem("cortex.composerPrefs.v1"))["responsive-test-user"]);
  expect(saved.newChatStart).toBe(third); expect(saved.recentModels[0]).toBe(first); expect(saved.recentModels.length).toBe(3);
  const newChat = page.getByRole("button", { name: "New chat", exact: true }).first();
  if (await newChat.isVisible()) await newChat.click(); else await page.getByRole("button", { name: "Start new chat", exact: true }).click();
  await expect(page.getByRole("button", { name: `Model: ${catalog.find(model => key(model) === third).display_name}` })).toBeVisible();
  await mode(page, "Compare"); await page.getByRole("button", { name: "Models to compare" }).click();
  await expect(page.locator(`[data-model-key="${third}"]`)).toHaveAttribute("aria-selected", "true");
});

test("lean catalog retains all providers, locks and the existing upgrade dialog", async ({ responsiveApp }) => {
  const { page, state } = responsiveApp; await prepare(responsiveApp, "free");
  await page.getByRole("button", { name: "Model: Smart" }).click(); await page.getByRole("button", { name: "All models", exact: true }).click();
  await expect(page.getByRole("option")).toHaveCount(state.models.length);
  for (const provider of new Set(state.models.map(model => model.provider))) await expect(page.getByText(provider.toUpperCase(), { exact: true })).toBeVisible();
  const locked = catalog.find(model => ["advanced", "premium"].includes(model.billing_class));
  await page.locator(`[data-model-key="${key(locked)}"]`).click();
  await expect(page.getByRole("dialog", { name: "This model is locked on your plan" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Model: Smart" })).toBeAttached();
});

test("lean Compare enforces two to three models and stays open while toggling", async ({ responsiveApp }) => {
  const { page } = responsiveApp; await prepare(responsiveApp); await mode(page, "Compare");
  await page.getByRole("button", { name: "Models to compare" }).click();
  const list = page.getByRole("listbox", { name: "Compare models" });
  await expect(list.getByRole("option", { selected: true })).toHaveCount(2);
  for (const row of await list.getByRole("option", { selected: true }).all()) await expect(row).toBeDisabled();
  await expect(list.getByRole("option", { name: /^Smart/ })).toHaveCount(0); await expect(list.getByText("New chats start with")).toHaveCount(0);
  await page.locator(`[data-model-key="${third}"]`).click(); await expect(page.getByText("3/3", { exact: true })).toBeVisible();
  for (const row of await list.getByRole("option", { selected: false }).all()) await expect(row).toBeDisabled();
  await page.locator(`[data-model-key="${third}"]`).click(); await expect(page.getByText("2/3", { exact: true })).toBeVisible();
});

test("lean desktop wraps stably and 320px mobile keeps every control inside the card", async ({ responsiveApp }, testInfo) => {
  const { page } = responsiveApp; await prepare(responsiveApp);
  await page.setViewportSize({ width: 1440, height: 900 });
  const card = page.locator("[data-composer-card]");
  expect((await card.boundingBox()).height).toBeLessThanOrEqual(58);
  await page.screenshot({ path: testInfo.outputPath("lean-desktop-empty.png") });
  const text = page.locator("#promptInput"); await text.fill("A short prompt"); await expect(card).toHaveAttribute("data-tall", "false");
  await text.fill("A lengthy prompt that wraps around the compact textarea width. ".repeat(6)); await expect(card).toHaveAttribute("data-tall", "true");
  await expect.poll(() => text.evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThan(800);
  await page.screenshot({ path: testInfo.outputPath("lean-desktop-tall.png") });
  await text.fill("Short again"); await expect(card).toHaveAttribute("data-tall", "false");
  await page.getByRole("button", { name: "More options", exact: true }).click();
  await page.getByRole("radio", { name: /^High/ }).click(); await page.getByRole("switch", { name: /Improve prompt/ }).click(); await page.keyboard.press("Escape");
  await pick(page, first);
  await page.screenshot({ path: testInfo.outputPath("lean-desktop-chips.png") });
  for (const width of [760, 390, 340, 320]) {
    await page.setViewportSize({ width, height: 844 }); await expectNoHorizontalOverflow(page);
    const bounds = await card.boundingBox();
    for (const button of await card.getByRole("button").all()) {
      const box = await button.boundingBox(); expect(box.x).toBeGreaterThanOrEqual(bounds.x); expect(box.x + box.width).toBeLessThanOrEqual(bounds.x + bounds.width + 1); expect(box.height).toBeGreaterThanOrEqual(40);
    }
    await expect(text).toHaveCSS("font-size", "16px");
  }
  await page.screenshot({ path: testInfo.outputPath("lean-mobile-320.png") });
});

test("lean menus step back, trap sheet focus and use theme tokens and reduced motion", async ({ responsiveApp }, testInfo) => {
  const { page } = responsiveApp; await prepare(responsiveApp); await page.emulateMedia({ reducedMotion: "reduce" });
  for (const width of [760, 761]) {
    await page.setViewportSize({ width, height: 1180 });
    const trigger = page.getByRole("button", { name: "Model: Smart" }); await trigger.click();
    const menu = page.getByRole("dialog", { name: "Choose a model" });
    await expect(menu).toHaveAttribute("data-layout", width === 760 ? "sheet" : "popover"); await expect(menu).toHaveCSS("animation-name", "none");
    if (width === 760) {
      await page.keyboard.press("Shift+Tab"); await expect(page.getByRole("button", { name: /New chats start with/ })).toBeFocused();
      await page.keyboard.press("Tab"); await expect(page.getByRole("option", { name: /^Smart / })).toBeFocused();
    }
    await page.getByRole("button", { name: "All models", exact: true }).click(); await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "All models", exact: true })).toBeVisible(); await page.keyboard.press("Escape"); await expect(trigger).toBeFocused();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    const options = page.getByRole("dialog", { name: "More options" });
    await expect(options).toHaveAttribute("data-layout", width === 760 ? "sheet" : "popover"); await expect(options).toHaveCSS("animation-name", "none");
    await expect(options.getByText("Smart in new chats")).toHaveCount(0);
    if (width === 760) { await expect(options.getByText("THIS MESSAGE")).toBeVisible(); await page.getByRole("button", { name: "Done" }).click(); }
    else { expect((await options.boundingBox()).width).toBe(360); await page.keyboard.press("Escape"); }
  }
  await page.getByRole("button", { name: "Account", exact: true }).click(); await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
  await page.getByRole("button", { name: "More options", exact: true }).click(); await expect(page.getByRole("dialog", { name: "More options" })).toHaveCSS("background-color", "rgb(17, 21, 27)");
  await page.screenshot({ path: testInfo.outputPath("lean-options-dark.png") });
});
