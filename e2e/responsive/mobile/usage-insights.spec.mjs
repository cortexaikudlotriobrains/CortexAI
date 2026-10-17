import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

test("mobile Usage route renders the compact dashboard and footer navigation", async ({
    responsiveApp,
}, testInfo) => {
    const { page } = responsiveApp;

    await page.goto("/usage");
    await expect(page.getByRole("heading", { name: "Usage & insights" })).toBeVisible();

    const visibleTitle = await page.locator("main h1").evaluate(element =>
        Array.from(element.children)
            .filter(child => window.getComputedStyle(child).display !== "none")
            .map(child => child.textContent?.trim())
            .join(""),
    );
    expect(visibleTitle).toBe("Usage");

    await expect(page.getByText("TOKENS", { exact: true })).toBeVisible();
    await expect(page.getByText("TOTAL TOKENS", { exact: true })).toBeHidden();
    await expect(page.getByText("AVG COST", { exact: true })).toHaveCount(0);
    await expect(page.getByText("AVG COST / REQ", { exact: true })).toHaveCount(0);
    await expect(page.getByText("$12.16 total", { exact: true })).toHaveCount(0);
    const metrics = page.getByRole("region", { name: "Usage key metrics" });
    await expect(metrics.locator("article")).toHaveCount(3);
    const widths = await metrics.locator("article").evaluateAll(cards =>
        cards.map(card => card.getBoundingClientRect().width),
    );
    expect(widths[2]).toBeGreaterThan(widths[0] * 1.9);
    await metrics.screenshot({ path: testInfo.outputPath("b2c-usage-metrics.png") });
    await expect(page.getByText("720 via Smart", { exact: true })).toBeVisible();
    await expect(page.getByText("1,336 total", { exact: true })).toBeHidden();
    await expect(page.getByText(/top Smart pick/)).toBeVisible();
    await expect(page.getByText("GPT-5.4 Mini").first()).toBeVisible();
    await expect(page.locator("#usage-activity-title")).toBeHidden();

    const modelList = page.locator('[aria-label="Models that replied list"]');
    const modelListMetrics = await modelList.evaluate(element => {
        const style = window.getComputedStyle(element);
        return {
            overflowX: style.overflowX,
            overflowY: style.overflowY,
            scrollHeight: element.scrollHeight,
            clientHeight: element.clientHeight,
        };
    });
    expect(modelListMetrics.overflowX).toBe("hidden");
    expect(modelListMetrics.overflowY).toBe("auto");
    expect(modelListMetrics.scrollHeight).toBeGreaterThan(modelListMetrics.clientHeight);

    const mobileNav = page.getByRole("navigation", { name: "Mobile navigation" });
    await expect(mobileNav).toBeVisible();
    await expect(mobileNav.getByRole("button", { name: "Ask" })).toBeVisible();
    await expect(mobileNav.getByRole("button", { name: "Analyze" })).toBeVisible();
    await expect(mobileNav.getByRole("button", { name: "History" })).toBeVisible();

    await expectNoHorizontalOverflow(page);
});

test("mobile Usage shows costs only for configured BYOK", async ({ responsiveApp }, testInfo) => {
    const { page, state } = responsiveApp;
    state.byokProviders = [{ provider: "openai", configured: true }];
    await page.goto("/usage");

    await expect(page.getByText("AVG COST", { exact: true })).toBeVisible();
    await expect(page.getByText("$12.16 total", { exact: true })).toBeVisible();
    const metrics = page.getByRole("region", { name: "Usage key metrics" });
    await expect(metrics.locator("article")).toHaveCount(4);
    await metrics.screenshot({ path: testInfo.outputPath("byok-usage-metrics.png") });

    state.byokProviders = [];
    await page.reload();
    await expect(page.getByRole("region", { name: "Usage summary loaded" })).toBeVisible();
    await expect(page.getByText("AVG COST", { exact: true })).toHaveCount(0);
    await expect(metrics.locator("article")).toHaveCount(3);
    await expectNoHorizontalOverflow(page);
});

test("mobile account menu exposes the Usage and AI credits entry points", async ({ responsiveApp }) => {
    const { page } = responsiveApp;

    await page.goto("/");
    await page.getByRole("button", { name: /^(Account|Guest account)$/ }).click();
    await expect(page.getByRole("menuitem", { name: "Usage & insights" })).toBeVisible();
    await page.getByRole("menuitem", { name: "Usage & insights" }).click();

    await expect(page).toHaveURL(/\/usage$/);
    await expect(page.getByRole("heading", { name: "Usage & insights" })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: /^(Account|Guest account)$/ }).click();
    await expect(page.getByRole("menuitem", { name: "AI credits" })).toBeVisible();
    await page.getByRole("menuitem", { name: "AI credits" }).click();

    await expect(page).toHaveURL(/\/credits$/);
    await expect(
        page.getByRole("heading", { name: "See what each question cost." }),
    ).toBeVisible();
    await expect(
        page.getByRole("progressbar", { name: "AI credits: 90 left of 100" }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Recent activity" })).toBeVisible();
    await expect(page.getByText("How do atomic credit reservations work?")).toBeVisible();
    await expect(page.getByText("13 credits")).toBeVisible();
    await expect(
      page.locator('section[aria-labelledby="recent-credit-activity-title"] article'),
    ).toHaveCount(1);
    await page.getByText("View credit breakdown").click();
    await expect(
      page.getByRole("listitem").filter({ hasText: "Final optimized AI answer" }),
    ).toContainText("Includes Prompt Optimizer");
    await expect(page.getByRole("listitem").filter({ hasText: "Web Search" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
});
