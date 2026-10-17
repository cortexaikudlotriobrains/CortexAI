import {
    expect,
    test,
} from "../fixtures/responsive-e2e.mjs";

// Plan defaults come from config/model_registry.yaml ui_defaults.ask_by_plan through
// frontend-react/src/config/generated/registryDefaults.ts.
for (const expectation of [
    { planCode: "free", expectedKey: "openai:gpt-6-luna" },
    { planCode: "plus", expectedKey: "claude:claude-sonnet-5-5" },
    { planCode: "pro", expectedKey: "openai:gpt-6.1-sol" },
]) {
    test(`${expectation.planCode} Ask shows its plan default after Smart is turned off`, async ({
        responsiveApp,
    }) => {
        const { page, state, reload } = responsiveApp;
        state.models = withAskDefaultModels(state.models);
        state.subscriptionPlan = expectation.planCode;
        await reload();

        const smartSwitch = page.getByRole("switch", { name: "Smart routing" });
        await expect(smartSwitch).toHaveAttribute("aria-checked", "true");
        await smartSwitch.click();

        await expect(smartSwitch).toHaveAttribute("aria-checked", "false");
        await expect(page.locator("#singleModel")).toHaveValue(expectation.expectedKey);
        await expect(page.locator("#singleModel option")).toHaveCount(state.models.length);
        await expect(
            page.locator(`#singleModel option[value="${expectation.expectedKey}"]`),
        ).toBeEnabled();
    });
}

function withAskDefaultModels(models) {
    const openai = models.find(model => model.provider === "openai");
    const claude = models.find(model => model.provider === "claude");
    if (!openai || !claude) throw new Error("Responsive model fixture is incomplete.");

    return [
        {
            ...openai,
            model: "gpt-6-luna",
            billing_class: "economical",
            access_category: "economical",
        },
        {
            ...claude,
            model: "claude-sonnet-5-5",
            billing_class: "advanced",
            access_category: "advanced",
        },
        {
            ...openai,
            model: "gpt-6.1-sol",
            billing_class: "advanced",
            access_category: "advanced",
        },
        ...models,
    ];
}
