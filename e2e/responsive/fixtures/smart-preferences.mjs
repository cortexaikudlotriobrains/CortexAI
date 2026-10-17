import { test, expect, expectNoHorizontalOverflow, restoreHistoryThread } from "./responsive-e2e.mjs";

const luna = "openai:gpt-5.6-luna";
const sonnet = "claude:claude-sonnet-5";
const flash = "deepseek:deepseek-v4-flash";

async function prepare(app, plan = "pro") {
    const template = app.state.models[0];
    app.state.models = [
        { ...template, provider: "openai", model: "gpt-5.6-luna", billing_class: "economical", display_name: "GPT-5.6 Luna", credit_usage_label: "Very low" },
        { ...template, provider: "claude", model: "claude-sonnet-5", billing_class: "advanced", display_name: "Claude Sonnet 5", credit_usage_label: "Medium" },
        { ...template, provider: "deepseek", model: "deepseek-v4-flash", billing_class: "economical", display_name: "DeepSeek V4 Flash", credit_usage_label: "Low" },
    ];
    app.state.subscriptionPlan = plan;
    await app.reload();
}

async function mode(page, name) {
    const desktop = page.getByRole("navigation", { name: "Workspace mode" });
    const nav = await desktop.isVisible() ? desktop : page.getByRole("navigation", { name: "Mobile navigation" });
    await nav.getByRole("button", { name, exact: true }).click();
}

async function saveModel(page, provider, model) {
    await page.getByRole("button", { name: /When Smart is off/ }).click();
    await page.getByRole("listbox", { name: "Providers", exact: true }).getByRole("option", { name: new RegExp(provider) }).click();
    await page.getByRole("option", { name: new RegExp(model) }).click();
    await expect(page.getByRole("status").filter({ hasText: "Saved" })).toBeVisible();
}

test("Smart menu updates the current chat and new chats, independently of the composer picker", async ({ responsiveApp }) => {
    const app = responsiveApp;
    const { page } = app;
    await prepare(app);
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await page.getByRole("switch", { name: /Smart in new chats/ }).click();
    const options = page.getByRole("dialog", { name: "More options" });
    await expect(options.getByText("Manual Ask", { exact: true })).toHaveCount(0);
    await expect(options.getByText("Smart Ask", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("switch", { name: "Smart routing" })).toHaveAttribute("aria-checked", "false");
    await expect(page.getByRole("switch", { name: /Smart in new chats/ })).toContainText("New chats start with Smart on");
    await expect(page.locator("#singleModel")).toHaveValue(luna);
    await saveModel(page, "DeepSeek", "DeepSeek V4 Flash");
    await expect(page.getByRole("switch", { name: "Smart routing" })).toHaveAttribute("aria-checked", "false");
    await expect(page.getByRole("switch", { name: /Smart in new chats/ })).not.toContainText("DeepSeek");
    await page.keyboard.press("Escape");
    const newChat = page.getByRole("button", { name: "New chat", exact: true }).first();
    if (await newChat.isVisible()) await newChat.click();
    else await page.getByRole("button", { name: "Start new chat", exact: true }).click();
    await expect(page.getByRole("switch", { name: "Smart routing" })).toHaveAttribute("aria-checked", "false");
    await expect(page.locator("#singleModel")).toHaveValue(flash);
    await page.locator("#singleModel").selectOption(luna);
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(page.getByRole("button", { name: /When Smart is off/ })).toContainText("DeepSeek V4 Flash");
    await page.keyboard.press("Escape");
    await mode(page, "Analyze");
    await expect(page.locator("#compareModel1")).toHaveValue(flash);
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(page.getByRole("switch", { name: /Smart in new chats/ })).toHaveCount(0);
    await saveModel(page, "Claude", "Claude Sonnet 5");
    await expect(page.locator("#compareModel1")).toHaveValue(sonnet);
    await expect(page.locator("#compareModel2")).toHaveValue(luna);
    await page.keyboard.press("Escape");
    await page.locator("#promptInput").fill("Compare this choice");
    await page.locator("#submitBtn").click();
    await expect(page.locator("#submitBtn")).toHaveAttribute("aria-label", "Send message");
    const expand = page.getByRole("button", { name: "Open follow-up composer" });
    if (await expand.isVisible()) await expand.click();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await saveModel(page, "DeepSeek", "DeepSeek V4 Flash");
    await expect(page.locator("#compareModel1")).toHaveValue(sonnet);
});

test("Smart menu toggles an existing chat immediately and restores its manual model after reload", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await prepare(responsiveApp);
    await restoreHistoryThread(page, "Plan a multi-region");
    const expand = page.getByRole("button", { name: "Open follow-up composer" });
    if (await expand.isVisible()) await expand.click();
    const smart = page.getByRole("switch", { name: "Smart routing" });
    await expect(smart).toHaveAttribute("aria-checked", "true");
    await smart.click();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(page.getByRole("switch", { name: /Smart in new chats/ })).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Escape");
    await page.locator("#singleModel").selectOption(flash);
    await smart.click();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    const preference = page.getByRole("switch", { name: /Smart in new chats/ });
    await expect(preference).toHaveAttribute("aria-checked", "true");
    await preference.click();
    await expect(smart).toHaveAttribute("aria-checked", "false");
    await expect(page.locator("#singleModel")).toHaveValue(flash);
    await expect(preference).toContainText("New chats start with Smart on");
    await expect(preference).not.toContainText("DeepSeek");
    await preference.click();
    await expect(smart).toHaveAttribute("aria-checked", "true");
    await preference.click();
    await page.keyboard.press("Escape");
    await responsiveApp.reload();
    await expect(smart).toHaveAttribute("aria-checked", "false");
    await expect(page.locator("#singleModel")).toHaveValue(flash);
    await expect(page.locator('section[aria-label="Chat transcript"]')).toBeVisible();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(preference).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Escape");
    await smart.click();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(preference).toHaveAttribute("aria-checked", "true");
    const savedSmart = await page.evaluate(() => JSON.parse(localStorage.getItem("cortex.composerPrefs.v1"))["responsive-test-user"].smartForNewChats);
    expect(savedSmart).toBe(true);
});

test("Smart-off model locks open the upgrade dialog above the retained list", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await prepare(responsiveApp, "free");
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await page.getByRole("button", { name: /When Smart is off/ }).click();
    await page.getByRole("option", { name: /Claude/ }).click();
    await page.getByRole("option", { name: /Claude Sonnet 5/ }).click();
    const close = page.getByRole("button", { name: "Close subscription message" });
    await expect(close).toBeVisible();
    const topLayer = await close.evaluate(element => {
        const rect = element.getBoundingClientRect();
        return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
    });
    expect(topLayer).toBe(true);
    await page.keyboard.press("Escape");
    await expect(close).toHaveCount(0);
    await expect(page.getByRole("listbox", { name: "Claude models" })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: /When Smart is off/ })).toContainText("GPT-5.6 Luna");
});

test("Smart menu matches its layout, keyboard contract, theme, and reduced motion", async ({ responsiveApp }, testInfo) => {
    const { page } = responsiveApp;
    await prepare(responsiveApp);
    const phone = page.viewportSize().width <= 760;
    await page.emulateMedia({ reducedMotion: "reduce" });
    const trigger = page.getByRole("button", { name: "More options", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "More options" });
    await expect(dialog.getByText("Smart Ask", { exact: true })).toHaveCount(0);
    await expect(dialog.getByText("Manual Ask", { exact: true })).toHaveCount(0);
    await expect(dialog).toHaveCSS("animation-name", "none");
    await expect(dialog).toHaveAttribute("data-layout", phone ? "sheet" : "popover");
    expect(await dialog.innerText()).not.toMatch(/\b(default|favorite|pin)\b/i);
    const bounds = await dialog.boundingBox();
    expect(bounds.width).toBe(phone ? page.viewportSize().width : 400);
    if (!phone) {
        const triggerBounds = await trigger.boundingBox();
        expect(bounds.y + bounds.height).toBeLessThanOrEqual(triggerBounds.y - 9);
    }
    if (phone) {
        await expect(dialog).toHaveAttribute("aria-modal", "true");
        await expect(dialog.getByRole("radio", { name: "Medium", exact: true })).toHaveText("Med");
        await expect(dialog.getByRole("radio", { name: "Maximum", exact: true })).toHaveText("Max");
        await expect(dialog.getByText(/Higher reasoning may take longer and use more AI credits\.$/)).toBeVisible();
        await dialog.getByRole("radio", { name: "High", exact: true }).click();
        await page.keyboard.press("Tab");
        await expect(dialog.getByRole("button", { name: "Done" })).toBeFocused();
    }
    await page.screenshot({ path: testInfo.outputPath("smart-menu-light.png") });
    await trigger.evaluate(element => {
        const body = element.closest('[data-composer-mode]').parentElement;
        if (getComputedStyle(body).zIndex !== "4") throw new Error("Composer options stacking was not raised");
    });
    await page.getByRole("button", { name: /When Smart is off/ }).click();
    await expect(page.getByRole("button", { name: "Back to More options" })).toBeFocused();
    await page.getByRole("option", { name: /ChatGPT/ }).click();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("option", { name: /ChatGPT/ })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: /When Smart is off/ })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
    await trigger.click();
    await expect(dialog).toHaveCSS("background-color", "rgb(17, 21, 27)");
    await expect(dialog.getByRole("switch", { name: /Smart in new chats/ }).locator(":scope > span:last-child > span")).toHaveCSS("background-color", "rgb(255, 255, 255)");
    await page.screenshot({ path: testInfo.outputPath("smart-menu-dark.png") });
    await expectNoHorizontalOverflow(page);
});

test("More options uses the exact 760px sheet breakpoint", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    for (const width of [760, 761]) {
        await page.setViewportSize({ width, height: 1180 });
        await page.getByRole("button", { name: "More options", exact: true }).click();
        await expect(page.getByRole("dialog", { name: "More options" })).toHaveAttribute("data-layout", width === 760 ? "sheet" : "popover");
        await page.keyboard.press("Escape");
    }
});

test("a saved Smart-off model falls back after downgrade and is retained for upgrade", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    await prepare(responsiveApp);
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await saveModel(page, "Claude", "Claude Sonnet 5");
    await page.keyboard.press("Escape");
    state.subscriptionPlan = "free";
    await responsiveApp.reload();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(page.getByRole("button", { name: /When Smart is off/ })).toContainText("GPT-5.6 Luna");
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem("cortex.composerPrefs.v1"))["responsive-test-user"].smartOffModelId);
    expect(saved).toBe(sonnet);
    state.subscriptionPlan = "pro";
    await responsiveApp.reload();
    await page.getByRole("button", { name: "More options", exact: true }).click();
    await expect(page.getByRole("button", { name: /When Smart is off/ })).toContainText("Claude Sonnet 5");
});
