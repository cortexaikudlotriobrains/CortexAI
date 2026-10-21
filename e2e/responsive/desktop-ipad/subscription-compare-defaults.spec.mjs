import {
    expect,
    test,
} from "../fixtures/responsive-e2e.mjs";

test("Free Compare defaults use allowed models and keep upgraded models offered", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    await page.locator("#btnCompareMode").click();

    await page.getByRole("button", { name: "Models to compare" }).click();
    await expect(page.locator('[data-model-key="openai:gpt-5.1"]')).toHaveAttribute("aria-selected", "true");
    await expect(page.locator('[data-model-key="deepseek:deepseek-chat"]')).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("option")).toHaveCount(state.models.length);
    await expect(
        page.locator('[data-model-key="claude:claude-sonnet-4-5"]'),
    ).toHaveAttribute("data-locked", "true");
    await expect(
        page.locator('[data-model-key="grok:grok-4"]'),
    ).toHaveAttribute("data-locked", "true");
});

test("Ask and Compare retain Gemini and Grok while the rich catalogue is disabled", async ({
    responsiveApp,
}) => {
    const { page, state, reload } = responsiveApp;
    state.modelsCatalogEnabled = false;
    await reload();

    await page.locator("#btnCompareMode").click();
    await page.getByRole("button", { name: "Models to compare" }).click();

    const listbox = page.getByRole("listbox", { name: "Compare models" });
    await expect(listbox.getByRole("option", { name: /Gemini/ })).toBeVisible();
    await expect(listbox.getByRole("option", { name: /Grok/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Models", exact: true })).toHaveCount(0);
});

test("Plus Compare skips a premium fallback while retaining the full offering", async ({ responsiveApp }) => {
    const { page, state, reload } = responsiveApp;
    const providerOrder = ["openai", "grok", "claude", "deepseek", "gemini"];
    state.models = [...state.models].sort(
        (left, right) => providerOrder.indexOf(left.provider) - providerOrder.indexOf(right.provider),
    );
    state.subscriptionPlan = "plus";
    await reload();
    await page.locator("#btnCompareMode").click();

    await page.getByRole("button", { name: "Models to compare" }).click();
    await expect(page.locator('[data-model-key="openai:gpt-5.1"]')).toHaveAttribute("aria-selected", "true");
    await expect(page.locator('[data-model-key="claude:claude-sonnet-4-5"]')).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("option")).toHaveCount(state.models.length);
    await expect(
        page.locator('[data-model-key="grok:grok-4"]'),
    ).toHaveAttribute("data-locked", "true");
});
