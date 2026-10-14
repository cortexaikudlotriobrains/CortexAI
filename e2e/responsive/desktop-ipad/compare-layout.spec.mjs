import {
    expect,
    expectNoHorizontalOverflow,
    restoreHistoryThread,
    test,
} from "../fixtures/responsive-e2e.mjs";

test("desktop multi-turn Compare uses tall natural cards and scrolls the transcript", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    await restoreHistoryThread(page, "Architecture decision");

    const metrics = await page.evaluate(() => {
        const transcript = document.querySelector('section[aria-label="Chat transcript"]');
        const transcriptGrid = transcript?.firstElementChild;
        const turns = [...document.querySelectorAll('article[aria-label="Model comparison"]')];
        return {
            transcriptClientHeight: transcript?.clientHeight ?? 0,
            transcriptScrollHeight: transcript?.scrollHeight ?? 0,
            transcriptPaddingBottom: transcriptGrid
                ? Number.parseFloat(getComputedStyle(transcriptGrid).paddingBottom)
                : 0,
            turns: turns.map(turn => {
                const grid = turn.querySelector("[data-response-panel]")?.parentElement;
                const cards = [...turn.querySelectorAll("[data-response-panel] > article")];
                const bodies = cards.map(card => card.querySelector("[id^='response-text-']"));
                return {
                    height: Math.round(turn.getBoundingClientRect().height),
                    cardCount: cards.length,
                    gridOverflowX: grid ? getComputedStyle(grid).overflowX : "",
                    gridClientWidth: grid?.clientWidth ?? 0,
                    gridScrollWidth: grid?.scrollWidth ?? 0,
                    cardHeights: cards.map(card => Math.round(card.getBoundingClientRect().height)),
                    cardWidths: cards.map(card => Math.round(card.getBoundingClientRect().width)),
                    bodies: bodies.map((body, index) => ({
                        clientHeight: body?.clientHeight ?? 0,
                        scrollHeight: body?.scrollHeight ?? 0,
                        overflowY: body ? getComputedStyle(body).overflowY : "",
                        headerTop: cards[index]?.querySelector("header")?.getBoundingClientRect().top ?? 0,
                        cardTop: cards[index]?.getBoundingClientRect().top ?? 0,
                        footerBottom:
                            cards[index]?.querySelector("footer")?.getBoundingClientRect().bottom ?? 0,
                        cardBottom: cards[index]?.getBoundingClientRect().bottom ?? 0,
                    })),
                };
            }),
        };
    });

    expect(metrics.transcriptScrollHeight).toBeGreaterThan(metrics.transcriptClientHeight);
    expect(metrics.transcriptPaddingBottom).toBeGreaterThanOrEqual(72);
    for (const turn of metrics.turns) {
        expect(turn.height).toBeGreaterThanOrEqual(640);
        expect(turn.cardCount).toBe(2);
        expect(turn.gridOverflowX).toBe("visible");
        expect(turn.gridScrollWidth).toBeLessThanOrEqual(turn.gridClientWidth + 1);
        expect(Math.max(...turn.cardHeights) - Math.min(...turn.cardHeights)).toBeLessThanOrEqual(2);
        for (const height of turn.cardHeights) {
            expect(height).toBeGreaterThanOrEqual(640);
            expect(height).toBeLessThanOrEqual(860);
        }
        expect(Math.max(...turn.cardWidths) - Math.min(...turn.cardWidths)).toBeLessThanOrEqual(2);
        for (const body of turn.bodies) {
            expect(body.scrollHeight).toBeGreaterThan(body.clientHeight);
            expect(body.overflowY).toBe("auto");
            expect(Math.abs(body.headerTop - body.cardTop)).toBeLessThanOrEqual(4);
            expect(Math.abs(body.footerBottom - body.cardBottom)).toBeLessThanOrEqual(2);
        }
    }
    await expectNoHorizontalOverflow(page);
});

test("desktop three-model Compare keeps all cards visible in one grid row", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    await restoreHistoryThread(page, "Three model platform comparison");

    const headers = page.locator("[data-response-index] > header");
    await expect(headers).toHaveCount(3);
    await expect(headers.getByRole("heading")).toHaveCount(3);
    await expect(headers.getByText(/^(MODEL|DEEP|FASTEST|RAW|ADVANCED|SMART · MODEL)$/i)).toHaveCount(0);
    await expect(headers.locator('[id^="response-stats-"]')).toHaveCount(3);

    const responseGrid = page
        .locator('article[aria-label="Model comparison"]')
        .first()
        .locator("[data-response-panel]")
        .first()
        .locator("xpath=..");
    const responseMetrics = await responseGrid.evaluate(grid => {
        const panels = [...grid.querySelectorAll(":scope > [data-response-panel]")];
        const rects = panels.map(panel => panel.getBoundingClientRect());
        return {
            clientWidth: grid.clientWidth,
            scrollWidth: grid.scrollWidth,
            overflowX: getComputedStyle(grid).overflowX,
            columns: getComputedStyle(grid).gridTemplateColumns.split(" ").length,
            panelWidths: rects.map(rect => Math.round(rect.width)),
            panelTops: rects.map(rect => Math.round(rect.top)),
        };
    });
    expect(responseMetrics.panelWidths).toHaveLength(3);
    expect(responseMetrics.columns).toBe(3);
    expect(responseMetrics.scrollWidth).toBeLessThanOrEqual(responseMetrics.clientWidth + 1);
    expect(responseMetrics.overflowX).toBe("visible");
    expect(Math.max(...responseMetrics.panelTops) - Math.min(...responseMetrics.panelTops)).toBeLessThanOrEqual(2);
    expect(Math.max(...responseMetrics.panelWidths) - Math.min(...responseMetrics.panelWidths)).toBeLessThanOrEqual(2);
    for (const width of responseMetrics.panelWidths) {
        expect(width).toBeGreaterThanOrEqual(280);
    }
    await expectNoHorizontalOverflow(page);
});

test("iPad landscape wraps three Compare cards into a two-column grid", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1024, height: 768 });
    await restoreHistoryThread(page, "Three model platform comparison");

    const grid = page
        .locator('article[aria-label="Model comparison"]')
        .first()
        .locator("[data-response-panel]")
        .first()
        .locator("xpath=..");
    const metrics = await grid.evaluate(element => {
        const panels = [...element.querySelectorAll(":scope > [data-response-panel]")];
        const rects = panels.map(panel => panel.getBoundingClientRect());
        return {
            columns: getComputedStyle(element).gridTemplateColumns.split(" ").length,
            overflowX: getComputedStyle(element).overflowX,
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
            tops: rects.map(rect => Math.round(rect.top)),
        };
    });

    expect(metrics.columns).toBe(2);
    expect(metrics.overflowX).toBe("visible");
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
    expect(metrics.tops[0]).toBe(metrics.tops[1]);
    expect(metrics.tops[2]).toBeGreaterThan(metrics.tops[1]);
    await expectNoHorizontalOverflow(page);
});

test("desktop Compare renders Markdown tables inside each response column", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    await restoreHistoryThread(page, "Compare deployment options table");

    const tables = page.getByRole("table");
    await expect(tables).toHaveCount(2);
    await expect(page.getByRole("columnheader", { name: "Recommendation" }).first()).toBeVisible();

    const metrics = await page
        .getByRole("region", { name: "Response table" })
        .first()
        .evaluate(wrapper => {
            const table = wrapper.querySelector("table");
            const header = table?.querySelector("thead");
            return {
                overflowX: getComputedStyle(wrapper).overflowX,
                tableDisplay: table ? getComputedStyle(table).display : "",
                tableMinWidth: table ? getComputedStyle(table).minWidth : "",
                headerPosition: header ? getComputedStyle(header).position : "",
            };
        });

    expect(metrics.overflowX).toBe("auto");
    expect(metrics.tableDisplay).toBe("table");
    expect(metrics.tableMinWidth).toBe("520px");
    expect(metrics.headerPosition).toBe("static");
    await expectNoHorizontalOverflow(page);
});

test("iPad portrait stacks all tall Compare cards with independent scrolling", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 820, height: 1180 });
    await restoreHistoryThread(page, "Architecture decision");

    const firstTurn = page.locator('article[aria-label="Model comparison"]').first();
    const panels = firstTurn.locator("[data-response-panel]");

    await expect(panels).toHaveCount(2);
    await expect(panels.nth(0)).toBeVisible();
    await expect(panels.nth(1)).toBeVisible();

    const metrics = await panels.evaluateAll(elements => {
        const rects = elements.map(element => element.getBoundingClientRect());
        return {
            stacked: rects[1].top > rects[0].bottom,
            heights: rects.map(rect => Math.round(rect.height)),
            bodies: elements.map(element => {
                const body = element.querySelector("[id^='response-text-']");
                return {
                    clientHeight: body?.clientHeight ?? 0,
                    scrollHeight: body?.scrollHeight ?? 0,
                    overflowY: body ? getComputedStyle(body).overflowY : "",
                };
            }),
        };
    });
    expect(metrics.stacked).toBe(true);
    for (const height of metrics.heights) expect(height).toBeGreaterThanOrEqual(520);
    for (const body of metrics.bodies) {
        expect(body.scrollHeight).toBeGreaterThan(body.clientHeight);
        expect(body.overflowY).toBe("auto");
    }
    await expectNoHorizontalOverflow(page);
});
