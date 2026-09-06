import {
    expect,
    expectNoHorizontalOverflow,
    openMobilePanel,
    restoreHistoryThread,
    test,
} from "../fixtures/responsive-e2e.mjs";

test("mobile Compare opens the visible model picker and updates its selection", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await openMobilePanel(page, "Compare");

    const connector = page.getByTestId("compare-connector");
    await expect(connector).toHaveCount(1);
    await expect(connector).toBeVisible();
    await expect(connector).toHaveCSS("border-top-width", "0px");
    await expect(connector).toHaveCSS("pointer-events", "none");

    const nativeSelect = page.locator("#compareModel1");
    const currentValue = await nativeSelect.inputValue();
    const target = await nativeSelect.locator("option:not(:disabled)").evaluateAll(
        (options, selectedValue) => {
            const option = options.find(candidate => candidate.value !== selectedValue);
            if (!option) return null;
            const separator = option.value.indexOf(":");
            return {
                value: option.value,
                provider: separator >= 0 ? option.value.slice(0, separator) : "",
                modelId: separator >= 0 ? option.value.slice(separator + 1) : option.value,
            };
        },
        currentValue,
    );
    expect(target).not.toBeNull();

    const trigger = page.getByRole("button", { name: /Compare model 1:/ });
    await trigger.click();
    const listbox = page.getByRole("listbox", { name: "Compare model 1 options" });
    await expect(listbox).toBeVisible();
    expect(await listbox.evaluate(element => element.parentElement === document.body)).toBe(true);
    await expect(listbox).toHaveAttribute("data-picker-view", "providers");
    await expect(listbox).toHaveAttribute("data-picker-interaction", "drilldown");
    await listbox.locator(`[data-provider-key="${target.provider}"]`).click();
    await expect(listbox).toHaveAttribute("data-picker-view", "models");

    const geometry = await listbox.evaluate(element => {
        const rect = element.getBoundingClientRect();
        const hit = document.elementFromPoint(rect.left + 24, rect.top + 24);
        return {
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
            left: rect.left,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            hitInside: Boolean(hit && element.contains(hit)),
        };
    });
    expect(geometry.left).toBeGreaterThanOrEqual(0);
    expect(geometry.top).toBeGreaterThanOrEqual(0);
    expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight);
    expect(geometry.hitInside).toBe(true);

    const targetOption = listbox
        .locator('[role="option"]')
        .filter({ hasText: target.modelId })
        .first();
    await targetOption.click();
    await expect(nativeSelect).toHaveValue(target.value);
    await expect(listbox).toBeHidden();
});

test("mobile Compare adds and removes a third model without page overflow", async ({ responsiveApp }) => {
    const { page, state, reload } = responsiveApp;
    state.subscriptionPlan = "pro";
    await reload();
    await openMobilePanel(page, "Compare");

    await page.getByRole("button", { name: "Add model to comparison" }).click();
    await expect(page.locator("#compareModel3Wrap")).toBeVisible();
    await expect(page.getByTestId("compare-connector")).toHaveCount(2);

    const scrollMetrics = await page.locator("#compareModel1Wrap").evaluate(element => {
        const chips = element.parentElement?.parentElement;
        return {
            clientWidth: chips?.clientWidth ?? 0,
            scrollWidth: chips?.scrollWidth ?? 0,
            overflowX: chips ? getComputedStyle(chips).overflowX : "",
        };
    });
    expect(scrollMetrics.scrollWidth).toBeGreaterThan(scrollMetrics.clientWidth);
    expect(scrollMetrics.overflowX).toBe("auto");
    await expectNoHorizontalOverflow(page);

    await page.getByRole("button", { name: /Remove DeepSeek Chat/i }).click();
    await expect(page.locator("#compareModel3Wrap")).toHaveCount(0);
    await expect(page.getByTestId("compare-connector")).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Add model to comparison" })).toBeVisible();
});

test("small mobile keeps the Compare connector inside the model scroller", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 320, height: 568 });
    await openMobilePanel(page, "Compare");

    const metrics = await page.evaluate(() => {
        const first = document.querySelector("#compareModel1Wrap")?.getBoundingClientRect();
        const second = document.querySelector("#compareModel2Wrap")?.getBoundingClientRect();
        const connector = document.querySelector("[data-testid='compare-connector']");
        const connectorRect = connector?.getBoundingClientRect();
        const chips = connector?.parentElement?.parentElement;
        return {
            ordered:
                Boolean(first && connectorRect && second)
                && first.right <= connectorRect.left
                && connectorRect.right <= second.left,
            connectorWidth: connectorRect?.width ?? 0,
            overflowX: chips ? getComputedStyle(chips).overflowX : "",
            scrollWidth: chips?.scrollWidth ?? 0,
            clientWidth: chips?.clientWidth ?? 0,
        };
    });

    expect(metrics.ordered).toBe(true);
    expect(metrics.connectorWidth).toBe(18);
    expect(metrics.overflowX).toBe("auto");
    expect(metrics.scrollWidth).toBeGreaterThan(metrics.clientWidth);
    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("mobile History search restores a grouped Compare session", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await openMobilePanel(page, "History");

    const search = page.getByRole("textbox", { name: "Search history" });
    await search.fill("Architecture decision");
    const thread = page.getByRole("button", { name: /Architecture decision/i });
    await expect(thread).toHaveCount(1);
    await thread.click();

    await expect(page.locator('article[aria-label="Model comparison"]')).toHaveCount(3);
    await expect(page.locator("#promptInput")).toHaveAttribute(
        "placeholder",
        "Ask once and compare model responses",
    );
    await expectNoHorizontalOverflow(page);
});

test("mobile Compare stacks Markdown table rows with visible column labels", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await restoreHistoryThread(page, "Compare deployment options table");

    const responseTabs = page.getByRole("tablist", { name: "Compare model responses" });
    await expect(responseTabs).toBeVisible();
    const switcherChrome = await responseTabs.evaluate(element => {
        const active = element.querySelector('[aria-selected="true"]');
        const icon = active?.children[0];
        const dot = active?.children[2];
        return {
            background: getComputedStyle(element).backgroundColor,
            borderRadius: getComputedStyle(element).borderRadius,
            activeBackground: active ? getComputedStyle(active).backgroundColor : "",
            iconBackground: icon ? getComputedStyle(icon).backgroundColor : "",
            dotBackground: dot ? getComputedStyle(dot).backgroundColor : "",
            dotWidth: dot ? dot.getBoundingClientRect().width : 0,
        };
    });
    expect(switcherChrome.background).toBe("rgb(244, 245, 247)");
    expect(switcherChrome.borderRadius).toBe("12px");
    expect(switcherChrome.activeBackground).toBe("rgb(255, 255, 255)");
    expect(switcherChrome.iconBackground).toBe("rgb(238, 240, 251)");
    expect(switcherChrome.dotBackground).toBe("rgb(91, 91, 214)");
    expect(switcherChrome.dotWidth).toBe(6);
    await expect(page.getByRole("table")).toHaveCount(1);
    const metrics = await page
        .getByRole("region", { name: "Response table" })
        .evaluate(wrapper => {
            const table = wrapper.querySelector("table");
            const head = table?.querySelector("thead");
            const body = table?.querySelector("tbody");
            const firstCell = table?.querySelector("tbody td");
            return {
                wrapperOverflow: getComputedStyle(wrapper).overflowX,
                tableDisplay: table ? getComputedStyle(table).display : "",
                headPosition: head ? getComputedStyle(head).position : "",
                bodyDisplay: body ? getComputedStyle(body).display : "",
                cellDisplay: firstCell ? getComputedStyle(firstCell).display : "",
                cellLabel: firstCell?.getAttribute("data-label") ?? "",
                pseudoLabel: firstCell
                    ? getComputedStyle(firstCell, "::before").content.replaceAll('"', "")
                    : "",
            };
        });

    expect(metrics.wrapperOverflow).toBe("visible");
    expect(metrics.tableDisplay).toBe("block");
    expect(metrics.headPosition).toBe("absolute");
    expect(metrics.bodyDisplay).toBe("grid");
    expect(metrics.cellDisplay).toBe("grid");
    expect(metrics.cellLabel).toBe("Option");
    expect(metrics.pseudoLabel).toBe("Option");

    await responseTabs.getByRole("tab", { name: "Claude Sonnet" }).click();
    await expect(responseTabs.getByRole("tab", { name: "Claude Sonnet" })).toHaveAttribute(
        "aria-selected",
        "true",
    );
    const claudeChrome = await responseTabs
        .getByRole("tab", { name: "Claude Sonnet" })
        .evaluate(element => {
            const icon = element.children[0];
            const dot = element.children[2];
            return {
                iconBackground: icon ? getComputedStyle(icon).backgroundColor : "",
                dotBackground: dot ? getComputedStyle(dot).backgroundColor : "",
            };
        });
    expect(claudeChrome.iconBackground).toBe("rgb(251, 238, 230)");
    expect(claudeChrome.dotBackground).toBe("rgb(224, 122, 77)");
    await expect(page.getByRole("table")).toHaveCount(1);
    await expectNoHorizontalOverflow(page);
});

test("mobile multi-turn Compare switches one natural-height response card at a time", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await restoreHistoryThread(page, "Architecture decision");

    const firstTurn = page.locator('article[aria-label="Model comparison"]').first();
    const tabs = firstTurn.getByRole("tablist", { name: "Compare model responses" });
    const gptTab = tabs.getByRole("tab", { name: "GPT-5.1" });
    const claudeTab = tabs.getByRole("tab", { name: "Claude Sonnet" });
    const panels = firstTurn.locator('[role="tabpanel"]');

    await expect(panels).toHaveCount(2);
    await expect(gptTab).toHaveAttribute("aria-selected", "true");
    await expect(panels.nth(0)).toBeVisible();
    await expect(panels.nth(1)).toBeHidden();
    const initialActiveTabLeft = await gptTab.evaluate(
        element => element.getBoundingClientRect().left,
    );
    await page.locator('section[aria-label="Chat transcript"]').evaluate(element => {
        element.scrollTop = 260;
        element.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await expect
        .poll(async () => tabs.evaluate(element => element.className))
        .toContain("mobileResponseTabsStuck");
    await expect
        .poll(async () => tabs.evaluate(element => getComputedStyle(element).boxShadow))
        .not.toBe("none");
    await expect
        .poll(async () => Math.abs(
            (await gptTab.evaluate(element => element.getBoundingClientRect().left))
            - initialActiveTabLeft,
        ))
        .toBeLessThanOrEqual(0.5);

    await page.locator('section[aria-label="Chat transcript"]').evaluate(element => {
        element.scrollTop = 0;
        element.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await expect
        .poll(async () => tabs.evaluate(element => element.className))
        .not.toContain("mobileResponseTabsStuck");
    await expect
        .poll(async () => Math.abs(
            (await gptTab.evaluate(element => element.getBoundingClientRect().left))
            - initialActiveTabLeft,
        ))
        .toBeLessThanOrEqual(0.5);

    await expect(panels.nth(0).getByRole("button", { name: /run details/i })).toHaveCount(0);
    const runDetails = panels.nth(0).locator('[id^="response-stats-"]');
    await expect(runDetails).toBeVisible();
    await expect(runDetails).toContainText("0.9s");
    await expect(runDetails).not.toContainText("tok");
    const runDetailsGeometry = await panels.nth(0).evaluate(panel => {
        const header = panel.querySelector("header");
        const titleRow = header?.firstElementChild;
        const detailsRow = panel.querySelector('[id^="response-stats-"]');
        const titleRect = titleRow?.getBoundingClientRect();
        const detailsRect = detailsRow?.getBoundingClientRect();
        const headerRect = header?.getBoundingClientRect();

        return {
            detailsBelowTitle:
                Boolean(titleRect && detailsRect) && detailsRect.top >= titleRect.bottom - 0.5,
            detailsWithinHeader:
                Boolean(headerRect && detailsRect)
                && detailsRect.left >= headerRect.left - 0.5
                && detailsRect.right <= headerRect.right + 0.5,
        };
    });
    expect(runDetailsGeometry.detailsBelowTitle).toBe(true);
    expect(runDetailsGeometry.detailsWithinHeader).toBe(true);

    const activeBodyMetrics = await panels.nth(0).locator("[id^='response-text-']").evaluate(body => {
        return {
            clientHeight: body.clientHeight,
            scrollHeight: body.scrollHeight,
            overflowY: getComputedStyle(body).overflowY,
        };
    });
    expect(activeBodyMetrics.clientHeight).toBe(activeBodyMetrics.scrollHeight);
    expect(activeBodyMetrics.overflowY).toBe("visible");

    await claudeTab.click();
    await expect(claudeTab).toHaveAttribute("aria-selected", "true");
    await expect(panels.nth(0)).toBeHidden();
    await expect(panels.nth(1)).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("mobile Compare response actions stay above the docked follow-up composer", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await restoreHistoryThread(page, "Architecture decision");

    await expectResponseActionsClearDock(page);
    await expectNoHorizontalOverflow(page);
});

async function expectResponseActionsClearDock(page) {
    const transcript = page.locator('section[aria-label="Chat transcript"]');
    const dock = page.getByRole("button", { name: "Open follow-up composer" });

    await expect(dock).toBeVisible();
    await transcript.evaluate(element => {
        element.scrollTop = element.scrollHeight;
    });

    for (const name of [
        "Copy response",
        "Regenerate response",
        "Helpful response",
        "Not helpful response",
    ]) {
        await expect(page.getByRole("button", { name }).last()).toBeVisible();
    }

    const metrics = await page.evaluate(() => {
        const isVisible = element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
        };
        const dockRect = document
            .querySelector('button[aria-label="Open follow-up composer"]')
            ?.getBoundingClientRect();
        const copyButtons = Array.from(document.querySelectorAll('button[aria-label="Copy response"]'))
            .filter(isVisible);
        const footerRect = copyButtons.at(-1)?.closest("footer")?.getBoundingClientRect();
        const buttonRects = [
            "Copy response",
            "Regenerate response",
            "Helpful response",
            "Not helpful response",
        ].map(name => {
            const button = Array.from(document.querySelectorAll(`button[aria-label="${name}"]`))
                .filter(isVisible)
                .at(-1);
            return button?.getBoundingClientRect();
        });
        return {
            dockTop: dockRect?.top ?? 0,
            footerBottom: footerRect?.bottom ?? Number.POSITIVE_INFINITY,
            allButtonsAboveDock: buttonRects.every(rect => Boolean(rect) && rect.bottom <= (dockRect?.top ?? 0) - 8),
        };
    });

    expect(metrics.footerBottom).toBeLessThanOrEqual(metrics.dockTop - 8);
    expect(metrics.allButtonsAboveDock).toBe(true);
}
