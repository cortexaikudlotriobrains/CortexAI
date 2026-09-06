import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

for (const width of [1440, 390]) {
    for (const code of ["plus", "pro"]) {
        test(`Cortex ${code} grant shows current access at ${width}px with Stripe disabled`, async ({ responsiveApp }) => {
            const { page, state } = responsiveApp;
            state.subscriptionPlan = code;
            state.subscriptionSource = "cortex_grant";
            state.billingEnabled = false;
            state.billingPlans = ["free", "plus", "pro"].map((plan, index) => ({
                code: plan,
                display_name: plan[0].toUpperCase() + plan.slice(1),
                monthly_price: [0, 6.99, 12.99][index],
                recommended: plan === "plus",
                features: {
                    max_compare_models: plan === "pro" ? 3 : 2,
                    allowed_billing_classes: ["economical", "standard"],
                },
                allowances: { ai_credits: [100000, 1000000, 3000000][index] },
            }));
            await page.setViewportSize({ width, height: 900 });
            await page.goto("/pricing");
            const current = page.getByRole("article").filter({ has: page.getByRole("heading", { name: code === "plus" ? "Plus" : "Pro", exact: true }) });
            await expect(current.getByRole("button", { name: "Current plan", exact: true })).toBeDisabled();
            await expect(page.getByRole("button", { name: "Unavailable", exact: true })).toHaveCount(2);
            await expectNoHorizontalOverflow(page);

            await page.goto("/account/billing");
            await expect(page.getByText(/Your plan access is provided by CortexAI/)).toBeVisible();
            await expect(page.getByText("Usage resets August 1, 2026", { exact: true })).toBeVisible();
            await expect(page.getByRole("button", { name: /Manage subscription|Update payment method/ })).toHaveCount(0);
            await expect(page.getByText(/Free allowances|Renews/)).toHaveCount(0);
            await expectNoHorizontalOverflow(page);
        });
    }
}
