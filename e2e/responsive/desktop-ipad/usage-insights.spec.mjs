import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

for (const byok of [false, true]) {
    test(`desktop Usage ${byok ? "shows confirmed BYOK costs" : "hides costs for paid B2C users"}`, async ({ responsiveApp }, testInfo) => {
        const { page, state } = responsiveApp;
        state.subscriptionPlan = "plus";
        state.byokProviders = byok ? [{ provider: "openai", configured: true }] : [];
        await page.goto("/usage");
        const metrics = page.getByRole("region", { name: "Usage key metrics" });
        await expect(metrics).toBeVisible();
        if (byok) {
            await expect(page.getByText("AVG COST / REQ", { exact: true })).toBeVisible();
            await expect(page.getByText("$12.16 total spend", { exact: true })).toBeVisible();
        } else {
            await expect(page.getByText("AVG COST / REQ", { exact: true })).toHaveCount(0);
            await expect(page.getByText("AVG COST", { exact: true })).toHaveCount(0);
            await expect(page.getByText(/total spend/)).toHaveCount(0);
        }
        await expect(metrics.locator("article")).toHaveCount(byok ? 4 : 3);
        const geometry = await metrics.locator("article").evaluateAll(cards => {
            const rects = cards.map(card => card.getBoundingClientRect());
            return { tops: rects.map(rect => rect.top), widths: rects.map(rect => rect.width) };
        });
        expect(Math.max(...geometry.tops) - Math.min(...geometry.tops)).toBeLessThanOrEqual(1);
        expect(Math.max(...geometry.widths) - Math.min(...geometry.widths)).toBeLessThanOrEqual(1);
        await metrics.screenshot({ path: testInfo.outputPath(`${byok ? "byok" : "b2c"}-usage-metrics.png`) });
        await expectNoHorizontalOverflow(page);
    });
}
