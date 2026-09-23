import {
    expect,
    expectNoHorizontalOverflow,
    openMobilePanel,
    restoreHistoryThread,
    test,
} from "../fixtures/responsive-e2e.mjs";

test("mobile navigation switches between Ask, Compare, and History", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    const mobileNav = page.getByRole("navigation", { name: "Mobile navigation" });
    const compose = page.getByRole("button", { name: "Start new chat" });

    await expect(mobileNav).toBeVisible();
    await expect(compose).toBeVisible();
    await expect(page.locator("aside[aria-label='Primary navigation']")).toBeHidden();
    await expect(page.locator("#btnSingleMode")).toBeHidden();
    await expect(page.locator("#promptInput")).toHaveAttribute(
        "placeholder",
        "Ask anything…",
    );

    await openMobilePanel(page, "Compare");
    await expect(page.getByLabel("Compare model selectors")).toBeVisible();
    await expect(page.locator("#promptInput")).toHaveAttribute(
        "placeholder",
        "Ask once and compare model responses",
    );

    await openMobilePanel(page, "History");
    await expect(page.getByRole("region", { name: "History" })).toBeVisible();
    await expect(page.getByRole("region", { name: "History" }).getByText("New chat")).toHaveCount(0);
    await expect(compose).toBeVisible();
    await expect(page.locator("#promptInput")).toHaveCount(0);

    await openMobilePanel(page, "Ask");
    await expect(page.locator("#promptInput")).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("mobile compose action starts a fresh Compare session from History", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await openMobilePanel(page, "History");
    await page.getByRole("button", { name: /Architecture decision/i }).first().click();
    await expect(page.locator('article[aria-label="Model comparison"]')).toHaveCount(3);

    await openMobilePanel(page, "History");
    await page.getByRole("button", { name: "Start new chat" }).click();

    await expect(page.getByRole("region", { name: "History" })).toHaveCount(0);
    await expect(page.locator('article[aria-label="Model comparison"]')).toHaveCount(0);
    await expect(page.getByLabel("Compare model selectors")).toBeVisible();
    await expect(page.locator("#promptInput")).toHaveValue("");
    await expect(page.locator("#promptInput")).toHaveAttribute(
        "placeholder",
        "Ask once and compare model responses",
    );
    await expectNoHorizontalOverflow(page);
});

test("mobile compose action clears an Ask thread while preserving Ask mode", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await openMobilePanel(page, "History");
    await page.getByRole("button", { name: /Help debug a FastAPI stream/i }).click();
    await expect(page.getByText("Add a retry strategy")).toBeVisible();
    await expect(page.getByRole("button", { name: "Open follow-up composer" })).toBeVisible();

    await page.getByRole("button", { name: "Open follow-up composer" }).click();
    await expect(page.locator("#promptInput")).toBeVisible();
    await page.locator("#promptInput").fill("Unsent follow-up");
    await page.getByRole("button", { name: "Start new chat" }).click();

    await expect(page.getByText("Add a retry strategy")).toHaveCount(0);
    await expect(page.locator("#promptInput")).toHaveValue("");
    await expect(page.locator("#promptInput")).toHaveAttribute(
        "placeholder",
        "Ask anything…",
    );
    await expectNoHorizontalOverflow(page);
});

test("mobile docked follow-up composer opens and closes on answer screens", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await restoreHistoryThread(page, "Help debug a FastAPI stream");

    const dock = page.getByRole("button", { name: "Open follow-up composer" });
    await expect(dock).toBeVisible();
    await expect(dock).toContainText("Smart");
    await expect(dock).toContainText("Ask a follow-up…");
    await expect(page.getByRole("button", { name: "Open composer" })).toHaveCount(0);
    await expect(page.locator("#promptInput")).not.toBeVisible();

    await dock.click();
    const promptInput = page.locator("#promptInput");
    await expect(promptInput).toBeVisible();
    await expect(promptInput).toBeFocused();
    await page.keyboard.type("Typed without a second tap");
    await expect(promptInput).toHaveValue("Typed without a second tap");

    await page.mouse.click(24, 160);
    await expect(dock).toBeVisible();
    await expect(page.locator("#promptInput")).not.toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("mobile Ask response actions stay above the docked follow-up composer", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await restoreHistoryThread(page, "Help debug a FastAPI stream");

    await expectResponseActionsClearDock(page);
    await expectNoHorizontalOverflow(page);
});

test("mobile composer uses the refresh hairline shell with a soft focus state", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 390, height: 844 });

    for (const mode of ["Ask", "Compare"]) {
        await openMobilePanel(page, mode);
        const textarea = page.locator("#promptInput");
        const composer = textarea.locator("xpath=../..");
        await page.evaluate(() => {
            if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        });
        await expect
            .poll(async () => composer.evaluate(element => getComputedStyle(element).borderColor))
            .toBe("rgb(235, 237, 240)");
        const idle = await composer.evaluate(element => {
            const style = getComputedStyle(element);
            return {
                borderColor: style.borderColor,
                boxShadow: style.boxShadow,
            };
        });
        expect(idle.borderColor).toBe("rgb(235, 237, 240)");
        expect(idle.boxShadow).not.toBe("none");

        await textarea.focus();
        const focused = await textarea.evaluate(element => ({
            outlineStyle: getComputedStyle(element).outlineStyle,
            boxShadow: getComputedStyle(element).boxShadow,
            shellShadow: getComputedStyle(element.parentElement?.parentElement).boxShadow,
        }));
        expect(focused.outlineStyle).toBe("none");
        expect(focused.boxShadow).toBe("none");
        expect(focused.shellShadow).not.toBe(idle.boxShadow);
        await expectNoHorizontalOverflow(page);
    }
});

test("mobile hides the attachment count and file-size hint", async ({ responsiveApp }) => {
    const { page } = responsiveApp;

    await expect(page.locator("#attachmentPlanLimit")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Attach files" })).toBeVisible();
});

test("mobile Ask and Compare keep feature controls beside Attach", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 320, height: 568 });

    for (const [mode, switchNames] of [
        ["Ask", ["Smart routing", "Research mode", "Prompt optimization"]],
        ["Compare", ["Research mode", "Prompt optimization"]],
    ]) {
        await openMobilePanel(page, mode);
        await expectComposerToolbarOrder(page, switchNames);
        await expectNoHorizontalOverflow(page);
    }
});

for (const viewport of [
    { width: 320, height: 568 },
    { width: 390, height: 844 },
]) {
    test(`mobile composer clears bottom navigation at ${viewport.width}px`, async ({ responsiveApp }) => {
        const { page } = responsiveApp;
        await page.setViewportSize(viewport);

        const metrics = await composerMetrics(page);
        expect(metrics.composerBottom).toBeLessThanOrEqual(metrics.navTop + 1);
        expect(metrics.sendVisible).toBe(true);
        expect(metrics.textareaFontSize).toBeGreaterThanOrEqual(16);
        expect(metrics.textareaHeight).toBeGreaterThanOrEqual(44);
        await expectMobileComposerControlsFullyVisible(page);
        await expectNoHorizontalOverflow(page);
    });
}

test("mobile composer auto-grows and keeps Send accessible", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    const textarea = page.locator("#promptInput");
    await textarea.fill(Array.from({ length: 18 }, (_, index) => `Line ${index + 1}`).join("\n"));

    await expect.poll(async () => {
        return textarea.evaluate(element => ({
            height: element.getBoundingClientRect().height,
            overflowY: getComputedStyle(element).overflowY,
        }));
    }).toMatchObject({
        height: 160,
        overflowY: "auto",
    });

    const metrics = await composerMetrics(page);
    expect(metrics.composerBottom).toBeLessThanOrEqual(metrics.navTop + 1);
    expect(metrics.sendVisible).toBe(true);
    await expectNoHorizontalOverflow(page);
});

test("mobile Enter inserts a newline instead of sending", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    const textarea = page.locator("#promptInput");

    await textarea.fill("First line");
    await textarea.press("Enter");
    await page.keyboard.type("Second line");

    await expect(textarea).toHaveValue("First line\nSecond line");
    await expect(page.getByText("First line", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Send message" })).toBeEnabled();
});

test("small mobile keeps focused feature tooltips inside the viewport", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 320, height: 568 });

    for (const [switchName, tooltipText] of [
        ["Smart routing", "Gets you the best answer automatically"],
        ["Research mode", "Uses latest information from the web"],
        ["Prompt optimization", "Helps you ask better for better results"],
    ]) {
        const chip = page.getByRole("switch", { name: switchName });
        const tooltip = page.locator('[role="tooltip"]').filter({ hasText: tooltipText });
        await chip.focus();
        await expect(tooltip).toBeVisible();
        const bounds = await tooltip.evaluate(element => {
            const rect = element.getBoundingClientRect();
            return {
                left: rect.left,
                right: rect.right,
                viewportWidth: window.innerWidth,
            };
        });
        expect(bounds.left, `${switchName} tooltip left edge`).toBeGreaterThanOrEqual(0);
        expect(bounds.right, `${switchName} tooltip right edge`).toBeLessThanOrEqual(
            bounds.viewportWidth,
        );
    }

    await openMobilePanel(page, "Compare");
    const web = page.getByRole("switch", { name: "Research mode" });
    const webTooltip = page
        .locator('[role="tooltip"]')
        .filter({ hasText: "Uses latest information from the web" });
    await expect(web).toContainText("Web");
    await expect(web.locator("svg circle")).toBeVisible();
    await expect(web).toHaveAttribute(
        "aria-describedby",
        await webTooltip.getAttribute("id"),
    );
    await expectNoHorizontalOverflow(page);
});

test("mobile tap toggles a feature chip and shows its tooltip briefly", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 390, height: 844 });

    const research = page.getByRole("switch", { name: "Research mode" });
    const tooltip = page
        .locator('[role="tooltip"]')
        .filter({ hasText: "Uses latest information from the web" });
    await expect(research).toHaveAttribute("aria-checked", "true");

    await research.evaluate(element => {
        element.dispatchEvent(
            new PointerEvent("pointerup", {
                bubbles: true,
                pointerType: "touch",
            }),
        );
        element.click();
    });

    await expect(research).toHaveAttribute("aria-checked", "false");
    await expect(tooltip).toHaveAttribute("data-touch-visible", "true");
    await expect(tooltip).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await expect(tooltip).toHaveAttribute("data-touch-visible", "false", {
        timeout: 3000,
    });
    await expect(tooltip).toBeHidden();
});

test("mobile attachment chips stay inside the composer without narrowing input", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    const textarea = page.locator("#promptInput");
    const widthBefore = await textarea.evaluate(element => element.getBoundingClientRect().width);

    await page.locator("#attachmentInput").setInputFiles({
        name: "very-long-mobile-product-design-reference-document.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("Responsive attachment fixture"),
    });

    const fileName = page.getByText("very-long-mobile-product-design-reference-document.txt");
    await expect(fileName).toBeVisible();
    const widthAfter = await textarea.evaluate(element => element.getBoundingClientRect().width);
    expect(Math.abs(widthAfter - widthBefore)).toBeLessThanOrEqual(1);
    await expect(page.getByRole("button", { name: "Send message" })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page
        .getByRole("button", {
            name: "Remove very-long-mobile-product-design-reference-document.txt",
        })
        .click();
    await expect(fileName).toHaveCount(0);
});

test("mobile Ask and Compare empty states share the upper-quarter greeting line", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    const promptInput = page.locator("#promptInput");
    for (const viewport of [
        { width: 390, height: 844 },
        { width: 390, height: 700 },
        { width: 320, height: 568 },
    ]) {
        await page.setViewportSize(viewport);
        await openMobilePanel(page, "Ask");
        await expect(promptInput).toBeVisible();
        await expect(promptInput).toHaveAttribute("placeholder", "Ask anything…");
        await expectGreetingAtMobileEmptyHeadingLine(page, "Hi, how can I help?");
        await expect(page.locator('[aria-label="Prompt examples"]')).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "Ask anything" })).toHaveCount(0);

        await openMobilePanel(page, "Compare");
        await expect(promptInput).toBeVisible();
        await expect(promptInput).toHaveAttribute(
            "placeholder",
            "Ask once and compare model responses",
        );
        await expect(page.getByText("Hi, how can I help?", { exact: true })).toHaveCount(0);
        await expectGreetingAtMobileEmptyHeadingLine(
            page,
            "Hi, what would you like to compare?",
        );
        await expect(page.getByRole("heading", { name: "Compare answers" })).toHaveCount(0);
        await expectNoHorizontalOverflow(page);
    }
});

async function expectGreetingAtMobileEmptyHeadingLine(page, text) {
    const greeting = page.getByText(text, { exact: true });
    await expect(greeting).toBeVisible();
    const bounds = await greeting.boundingBox();
    const composerBounds = await page.locator("#promptInput").locator("xpath=../..").boundingBox();
    expect(bounds).not.toBeNull();
    expect(composerBounds).not.toBeNull();
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    expect(Math.abs(bounds.x + bounds.width / 2 - viewport.width / 2)).toBeLessThanOrEqual(1);
    expect(Math.abs(bounds.y + bounds.height / 2 - viewport.height * 0.25)).toBeLessThanOrEqual(1);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(composerBounds.y - 12);
}

async function composerMetrics(page) {
    return page.evaluate(() => {
        const textarea = document.querySelector("#promptInput");
        const card = textarea?.parentElement?.parentElement;
        const composer = card?.parentElement;
        const nav = document.querySelector("nav[aria-label='Mobile navigation']");
        const send = document.querySelector("#submitBtn");
        const textareaRect = textarea?.getBoundingClientRect();
        const sendRect = send?.getBoundingClientRect();
        return {
            composerBottom: composer?.getBoundingClientRect().bottom ?? Number.POSITIVE_INFINITY,
            navTop: nav?.getBoundingClientRect().top ?? 0,
            sendVisible:
                !!sendRect
                && sendRect.width >= 36
                && sendRect.height >= 36
                && sendRect.right <= window.innerWidth,
            textareaFontSize: Number.parseFloat(
                textarea ? getComputedStyle(textarea).fontSize : "0",
            ),
            textareaHeight: textareaRect?.height ?? 0,
        };
    });
}

async function expectMobileComposerControlsFullyVisible(page) {
    const featureControls = page.locator("#promptFeatureControls");
    const featureBounds = await featureControls.boundingBox();
    expect(featureBounds).not.toBeNull();

    for (const name of ["Smart routing", "Research mode", "Prompt optimization"]) {
        const control = page.getByRole("switch", { name });
        await expect(control).toBeVisible();
        const bounds = await control.boundingBox();
        expect(bounds, `${name} bounds`).not.toBeNull();
        expect(bounds.x, `${name} left edge`).toBeGreaterThanOrEqual(featureBounds.x - 1);
        expect(bounds.x + bounds.width, `${name} right edge`).toBeLessThanOrEqual(
            featureBounds.x + featureBounds.width + 1,
        );
    }

    for (const name of ["Attach files", "Send message"]) {
        const control = page.getByRole("button", { name });
        await expect(control).toBeVisible();
        const bounds = await control.boundingBox();
        expect(bounds, `${name} bounds`).not.toBeNull();
        expect(bounds.x, `${name} left edge`).toBeGreaterThanOrEqual(0);
        expect(bounds.x + bounds.width, `${name} right edge`).toBeLessThanOrEqual(
            page.viewportSize().width,
        );
    }
}

async function expectComposerToolbarOrder(page, switchNames) {
    const attach = page.getByRole("button", { name: "Attach files" });
    const send = page.getByRole("button", { name: "Send message" });
    const controls = [attach, ...switchNames.map(name => page.getByRole("switch", { name })), send];
    const bounds = [];

    for (const control of controls) {
        await expect(control).toBeVisible();
        const box = await control.boundingBox();
        expect(box).not.toBeNull();
        bounds.push(box);
    }

    const attachCenter = bounds[0].y + bounds[0].height / 2;
    for (const box of bounds.slice(1, -1)) {
        expect(Math.abs(box.y + box.height / 2 - attachCenter)).toBeLessThanOrEqual(1);
    }
    for (let index = 1; index < bounds.length; index += 1) {
        expect(bounds[index - 1].x + bounds[index - 1].width).toBeLessThanOrEqual(
            bounds[index].x + 1,
        );
    }
}

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
