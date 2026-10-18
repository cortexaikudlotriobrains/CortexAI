import { expect, restoreHistoryThread, test } from "../fixtures/responsive-e2e.mjs";

for (const width of [1440, 390]) {
    test(`Compare follow-up preserves three-model provenance at ${width}px`, async ({ responsiveApp }) => {
        const { page, state, reload } = responsiveApp;
        state.subscriptionPlan = "pro";
        await page.setViewportSize({ width, height: 900 });
        await reload();
        await restoreHistoryThread(page, "Three model platform comparison", { view: "responses" });

        let payload;
        await page.route("**/v1/compare/stream", async route => {
            payload = route.request().postDataJSON();
            // Stop before any provider work while exercising the real submit path.
            await route.fulfill({ status: 402, json: { detail: {
                code: "insufficient_credits", message: "Provider-free request capture.",
            } } });
        });
        const dock = page.getByRole("button", { name: "Open follow-up composer" });
        if (await dock.isVisible()) await dock.click();
        await page.locator("#promptInput").fill("Expand the launch strategy within the $500 budget.");
        await page.getByRole("button", { name: "Send message" }).click();
        await expect.poll(() => payload).toBeTruthy();
        expect(payload.targets).toHaveLength(3);
        const history = payload.context.conversation_history;
        const assistant = history.find(item => item.response_variants?.length === 3);
        expect(assistant).toBeTruthy();
        expect(assistant.response_variants.map(item => item.content).join("\n\n")).toBe(assistant.content);
        expect(new Set(assistant.response_variants.map(item => `${item.provider}:${item.model}`)).size).toBe(3);
        expect(history.some(item => item.role === "user" && item.content)).toBeTruthy();
        expect(payload.automatic_analysis).toBe(true);
        expect(payload.routing.web_mode).toBe("auto");
    });
}
