import {
    expect,
    expectNoHorizontalOverflow,
    openMobilePanel,
    test,
} from "../fixtures/responsive-e2e.mjs";

function toNdjson(events) {
    return events.map(event => `${JSON.stringify(event)}\n`).join("");
}

test("desktop uses the sidebar and top mode navigation", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    await expect(sidebar).toBeVisible();
    await expect(sidebar).not.toContainText("responsive-test-user");
    await expect(sidebar).not.toContainText("Session active");
    const sidebarBounds = await sidebar.boundingBox();
    const historyBounds = await sidebar.locator("[data-sidebar-history]").boundingBox();
    expect(sidebarBounds).not.toBeNull();
    expect(historyBounds).not.toBeNull();
    expect(Math.abs(
        sidebarBounds.y + sidebarBounds.height - (historyBounds.y + historyBounds.height),
    )).toBeLessThanOrEqual(1);
    await expect(sidebar.getByRole("button", { name: "Ask", exact: true })).toBeHidden();
    await expect(sidebar.getByRole("button", { name: "Compare", exact: true })).toBeHidden();
    await expect(sidebar.getByRole("button", { name: "Work", exact: true })).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toBeHidden();
    await expect(page.locator("#btnSingleMode")).toBeVisible();
    await expect(page.locator("#btnSingleMode svg")).toBeVisible();
    await expect(page.locator("#promptInput")).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("desktop keeps workspace action spacing fixed across Ask, Compare, and Work", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    const workspaceActions = () => page.locator('[aria-label="Workspace actions"]');
    const modeNavigation = () => page.getByRole("navigation", { name: "Workspace mode" });
    const expectFixedActionGeometry = async () => {
        const newActionIcon = workspaceActions()
            .getByRole("button", { name: /^New (chat|work)$/ })
            .locator("svg");

        await expect(workspaceActions()).toHaveCSS("column-gap", "24px");
        await expect(newActionIcon).toHaveCSS("width", "18px");
        await expect(newActionIcon).toHaveCSS("height", "18px");
    };

    await expectFixedActionGeometry();

    await modeNavigation().getByRole("button", { name: "Compare" }).click();
    await expectFixedActionGeometry();

    await modeNavigation().getByRole("button", { name: "Work" }).click();
    await expect(page).toHaveURL(/\/work$/);
    await expectFixedActionGeometry();
});

test("desktop sidebar collapses to an icon rail and expands again", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    await expect(sidebar).toBeVisible();
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");

    const expandedWidth = await sidebar.evaluate(element => element.getBoundingClientRect().width);
    await page.getByRole("button", { name: "Collapse sidebar" }).click();

    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect(sidebar.getByRole("textbox", { name: "Search chats" })).toBeHidden();
    await expect.poll(
        () => sidebar.evaluate(element => element.getBoundingClientRect().width),
    ).toBeLessThanOrEqual(90);
    const collapsedWidth = await sidebar.evaluate(element => element.getBoundingClientRect().width);
    expect(collapsedWidth).toBeLessThan(expandedWidth);

    await page
        .getByRole("navigation", { name: "Workspace mode" })
        .getByRole("button", { name: "Compare" })
        .click();
    await expect(page.locator("#btnCompareMode")).toHaveClass(/activeTab/);

    await page.getByRole("button", { name: "Expand sidebar" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await expect(sidebar.getByRole("textbox", { name: "Search chats" })).toBeVisible();

    await page.setViewportSize({ width: 820, height: 1180 });
    await expect(sidebar).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("desktop history keeps compact title, mode, and date rows", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 520 });

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    const rows = sidebar.locator("button[data-history-thread]");
    await expect(rows).toHaveCount(5);

    const rowMetrics = await rows.evaluateAll(elements =>
        elements.map(element => {
            const title = element.querySelector("[data-history-title]");
            const surface = element.parentElement ?? element;
            return {
                height: surface.getBoundingClientRect().height,
                titleWidth: title?.getBoundingClientRect().width ?? 0,
                titleFontSize: title ? getComputedStyle(title).fontSize : "",
                titleWhiteSpace: title ? getComputedStyle(title).whiteSpace : "",
                titleOverflow: title ? getComputedStyle(title).textOverflow : "",
            };
        }),
    );
    for (const row of rowMetrics) {
        expect(row.height).toBeLessThanOrEqual(38);
        expect(row.titleWidth).toBeGreaterThanOrEqual(140);
        expect(row.titleFontSize).toBe("11.5px");
        expect(row.titleWhiteSpace).toBe("nowrap");
        expect(row.titleOverflow).toBe("ellipsis");
    }

    const longRow = sidebar
        .locator("button[data-history-thread]")
        .filter({ hasText: "Plan a multi-region platform migration" });
    await expect(longRow).toHaveAttribute(
        "aria-label",
        /Ask,/,
    );
    const truncation = await longRow.evaluate(element => {
        const title = element.querySelector("[data-history-title]");
        return {
            titleTruncated: Boolean(title && title.scrollWidth > title.clientWidth),
            hasModel: element.textContent?.includes(
                "gpt-5.4-mini-enterprise-preview-with-extended-context",
            ),
            hasTurnCount: element.textContent?.includes("1 turn"),
        };
    });
    expect(truncation.titleTruncated).toBe(true);
    expect(truncation.hasModel).toBe(false);
    expect(truncation.hasTurnCount).toBe(false);

    const historyList = sidebar.locator("ul").first();
    const listMetrics = await historyList.evaluate(element => ({
        overflowY: getComputedStyle(element).overflowY,
        scrollHeight: element.scrollHeight,
        clientHeight: element.clientHeight,
    }));
    expect(listMetrics.overflowY).toBe("auto");
    expect(listMetrics.scrollHeight).toBeGreaterThan(listMetrics.clientHeight);

    const longRowSurface = longRow.locator("xpath=..");
    await longRowSurface.hover();
    await expect.poll(
        () => longRowSurface.evaluate(element => getComputedStyle(element).backgroundColor),
    ).toBe("rgb(240, 242, 244)");
    const optionsButton = sidebar.getByRole("button", {
        name: /Chat options for Plan a multi-region platform migration/,
    });
    await optionsButton.click();
    const rowMenu = sidebar.getByRole("menu", {
        name: /Options for Plan a multi-region platform migration/,
    });
    await expect(rowMenu.getByRole("menuitem")).toHaveCount(2);
    await expect(rowMenu.getByRole("menuitem", { name: "Delete" })).toHaveCSS(
        "color",
        "rgb(220, 38, 38)",
    );
    await page.keyboard.press("Escape");
    await expect(rowMenu).toBeHidden();
    await longRow.click();
    await expect(longRow).toHaveAttribute("aria-current", "page");

    const search = sidebar.getByRole("textbox", { name: "Search chats" });
    await search.fill("multi-region");
    await expect(rows).toHaveCount(1);
    await expect(longRow).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

test("desktop sidebar stays consistent across Chat, Work, Usage, and AI credits", async ({
    responsiveApp,
}) => {
    const { page, state } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    state.workSessions = [{
        id: "work-session-sidebar",
        session_id: "work-history-sidebar",
        title: "Prepare the sidebar regression report",
        status: "completed",
        agent_provider: "fake",
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:05:00Z",
        latest_run_status: "completed",
    }];
    await page.reload();

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    await expect(
        sidebar.getByRole("button", {
            name: "Prepare the sidebar regression report. Work, completed",
        }),
    ).toBeVisible();

    await sidebar.getByRole("button", { name: "Collapse sidebar" }).click();
    await sidebar.getByRole("button", { name: "AI credits" }).click();
    await expect(page).toHaveURL(/\/credits$/);
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect(sidebar.getByRole("button", { name: "Work", exact: true })).toBeHidden();

    await sidebar.getByRole("button", { name: "Expand sidebar" }).click();
    await expect(
        sidebar.getByRole("button", {
            name: "Prepare the sidebar regression report. Work, completed",
        }),
    ).toBeVisible();

    await sidebar.getByRole("button", { name: "New chat" }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(
        page
            .getByRole("navigation", { name: "Workspace mode" })
            .getByRole("button", { name: "Ask", exact: true }),
    ).toHaveAttribute(
        "aria-pressed",
        "true",
    );

    await sidebar.getByRole("button", { name: "Usage" }).click();
    await expect(page).toHaveURL(/\/usage$/);
    await expect(sidebar.getByRole("button", { name: "Work", exact: true })).toBeHidden();

    await sidebar.getByRole("button", { name: "New chat" }).click();
    await page
        .getByRole("navigation", { name: "Workspace mode" })
        .getByRole("button", { name: "Work", exact: true })
        .click();
    await expect(page).toHaveURL(/\/work$/);
    const chatHistory = sidebar.getByRole("button", {
        name: /Plan a multi-region platform migration.*Ask,/,
    });
    await expect(chatHistory).toBeVisible();
    await chatHistory.click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator('section[aria-label="Chat transcript"]')).toBeVisible();
});

test("desktop sidebar shows a new chat before the response is persisted", async ({
    responsiveApp,
}) => {
    const { page, state } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    state.history = [{
        ...state.history[0],
        id: 9001,
        request_id: "existing-today-request",
        session_id: "existing-today-session",
        timestamp: new Date().toISOString(),
        prompt: "How much would an annuity pay",
        response: "Existing response",
    }, ...state.history];
    state.workSessions = [{
        id: "work-session-pending-chat",
        session_id: "work-history-pending-chat",
        title: "Older Work history",
        status: "completed",
        agent_provider: "fake",
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:05:00Z",
        latest_run_status: "completed",
    }];
    await page.reload();

    const prompt = "how should a proper chicken biryani be cooked";
    let releaseStream;
    const streamGate = new Promise(resolve => {
        releaseStream = resolve;
    });
    await page.route("**/v1/chat/stream", async route => {
        await streamGate;
        state.history = [{
            ...state.history[0],
            id: 9002,
            request_id: "pending-sidebar-request",
            session_id: "pending-sidebar-session",
            timestamp: new Date().toISOString(),
            prompt,
            response: "Use layered rice and dum cooking.",
        }, ...state.history];
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", provider: "openai", model: "gpt-5.1" },
                { type: "line", text: "Use layered rice and dum cooking." },
                {
                    type: "response_done",
                    response: {
                        request_id: "pending-sidebar-request",
                        provider: "openai",
                        model: "gpt-5.1",
                        text: "Use layered rice and dum cooking.",
                        latency_ms: 300,
                        estimated_cost: 0.001,
                        token_usage: {
                            prompt_tokens: 10,
                            completion_tokens: 10,
                            total_tokens: 20,
                        },
                        web_source_items: [],
                    },
                },
                { type: "done", session_id: "pending-sidebar-session" },
            ]),
        });
    });

    await page.locator("#promptInput").fill(prompt);
    await page.locator("#submitBtn").click();

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    const preview = sidebar.locator("[data-current-chat-preview]");
    const sourcesEnabled = await page
        .getByRole("switch", { name: "Research mode" })
        .getAttribute("aria-checked") === "true";
    const loadingOrb = page.locator("canvas.response-loading-orb");
    const workRow = sidebar.getByRole("button", {
        name: "Older Work history. Work, completed",
    });
    try {
        await expect(loadingOrb).toBeVisible();
        await expect(loadingOrb).toHaveAttribute(
            "data-loading-state",
            sourcesEnabled ? "searching" : "working",
        );
        await expect(loadingOrb).toHaveAttribute("aria-hidden", "true");
        await expect(
            preview.getByRole("button", { name: new RegExp(`${prompt}\\. Ask,`) }),
        ).toBeVisible();
        await expect(preview.getByRole("button")).toHaveAttribute("aria-current", "page");
        await expect(sidebar.getByText("Today", { exact: true })).toHaveCount(1);
        const previewBox = await preview.boundingBox();
        const workBox = await workRow.boundingBox();
        expect(previewBox?.y).toBeLessThan(workBox?.y ?? Number.POSITIVE_INFINITY);
    } finally {
        releaseStream();
    }

    await expect(page.getByText("Use layered rice and dum cooking.")).toBeVisible();
    await expect(preview).toHaveCount(0);
    await expect(sidebar.getByText("Today", { exact: true })).toHaveCount(1);
    await expect(sidebar.getByText(prompt, { exact: true })).toHaveCount(1);
});

test("desktop composer uses the refresh hairline shell and soft textarea focus state", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    await expectSoftComposerShell(page);
    await page.locator("#btnCompareMode").click();
    await expectSoftComposerShell(page);
});

test("desktop feature chips show accessible tooltips in Ask and Compare", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    await expectChipTooltip(
        page,
        "Smart routing",
        "Gets you the best answer automatically",
    );
    await expectChipTooltip(
        page,
        "Research mode",
        "Uses latest information from the web",
    );
    await expectChipTooltip(
        page,
        "Prompt optimization",
        "Helps you ask better for better results",
    );

    await page.locator("#btnCompareMode").click();
    await expectChipTooltip(
        page,
        "Research mode",
        "Uses latest information from the web",
    );
    await expectChipTooltip(
        page,
        "Prompt optimization",
        "Helps you ask better for better results",
    );
    await expectNoHorizontalOverflow(page);
});

test("dark theme gives enabled Ask feature chips a distinct accent state", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    const switches = [
        page.getByRole("switch", { name: "Smart routing" }),
        page.getByRole("switch", { name: "Research mode" }),
        page.getByRole("switch", { name: "Prompt optimization" }),
    ];
    for (const featureSwitch of switches) {
        if ((await featureSwitch.getAttribute("aria-checked")) !== "true") {
            await featureSwitch.click();
        }
        await page.getByRole("navigation", { name: "Workspace mode" }).hover();
        await expect(featureSwitch).toHaveCSS("background-color", "rgb(52, 52, 103)");
        await expect(featureSwitch).toHaveCSS("color", "rgb(255, 255, 255)");
        await expect(featureSwitch).toHaveCSS("box-shadow", /rgb\(139, 139, 240\)/);
    }

    await switches[0].click();
    await expect(switches[0]).not.toHaveCSS("background-color", "rgb(52, 52, 103)");
    await expect(switches[0]).not.toHaveCSS("color", "rgb(255, 255, 255)");
});

test("dark theme keeps the top Ask and Compare tabs legible", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    const modeNavigation = page.getByRole("navigation", { name: "Workspace mode" });
    const askTab = modeNavigation.getByRole("button", { name: "Ask" });
    const compareTab = modeNavigation.getByRole("button", { name: "Compare" });

    await expect(askTab).toHaveCSS("color", "rgb(241, 243, 246)");
    await expect(askTab).toHaveCSS("border-bottom-color", "rgb(139, 139, 240)");
    await expect(compareTab).toHaveCSS("color", "rgb(174, 182, 194)");

    await compareTab.click();
    await expect(compareTab).toHaveCSS("color", "rgb(241, 243, 246)");
    await expect(compareTab).toHaveCSS("border-bottom-color", "rgb(139, 139, 240)");
    await expect(askTab).toHaveCSS("color", "rgb(174, 182, 194)");
});

test("desktop centers the empty Ask and Compare composer, then docks it after submit", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    const composer = page.locator("[data-composer-placement]");
    await expect(composer).toHaveAttribute("data-composer-placement", "center");
    await expectCenteredComposer(page);
    await expect(page.getByRole("heading", { name: "Ask anything" })).toHaveCount(0);
    await expect(page.getByText("Hi, how can I help?", { exact: true })).toBeVisible();

    await page.locator("#btnCompareMode").click();
    await expect(composer).toHaveAttribute("data-composer-placement", "center");
    await expectCenteredComposer(page);
    await expect(page.getByRole("heading", { name: "Compare answers" })).toHaveCount(0);
    await expect(
        page.getByText("Hi, what would you like to compare?", { exact: true }),
    ).toBeVisible();

    await page.locator("#promptInput").fill("Compare two rollout strategies");
    await page.locator("#submitBtn").click();
    await expect(composer).toHaveAttribute("data-composer-placement", "bottom");
    await expect(page.getByText("Hi, what would you like to compare?", { exact: true })).toHaveCount(0);
});

test("Compare Web and Improve use the same styling for matching states", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.locator("#btnCompareMode").click();

    const web = page.getByRole("switch", { name: "Research mode" });
    const improve = page.getByRole("switch", { name: "Prompt optimization" });
    const promptInput = page.locator("#promptInput");

    await expect(web).toContainText("Web");
    await expect(web.locator("svg circle")).toBeVisible();
    await web.click();
    await promptInput.hover();
    await expectMatchingChipStyles(web, improve);

    await web.click();
    await improve.click();
    await promptInput.hover();
    await expectMatchingChipStyles(web, improve);

    await page.getByRole("button", { name: "Account" }).click();
    await page.getByRole("menuitem", { name: "Switch to dark theme" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expectMatchingChipStyles(web, improve);

    await web.click();
    await improve.click();
    await promptInput.hover();
    await expectMatchingChipStyles(web, improve);
});

test("desktop Ask and Compare keep feature controls beside Attach", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    for (const [mode, switchNames] of [
        ["Ask", ["Smart routing", "Research mode", "Prompt optimization"]],
        ["Compare", ["Research mode", "Prompt optimization"]],
    ]) {
        await page
            .getByRole("navigation", { name: "Workspace mode" })
            .getByRole("button", { name: mode })
            .click();
        await expectComposerToolbarOrder(page, switchNames);
        await expectNoHorizontalOverflow(page);
    }
});

test("Improve clears the composer and keeps response cards hidden until optimization resolves", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    let releaseOptimization;
    const optimizationGate = new Promise(resolve => {
        releaseOptimization = resolve;
    });
    await page.route("**/v1/optimize", async route => {
        await optimizationGate;
        await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
                original_prompt: "rough browser prompt",
                optimized_prompt: "Clear browser prompt",
                was_optimized: true,
                server_optimization_enabled: true,
                optimization_status: "optimized",
            }),
        });
    });
    await page.route("**/v1/chat/stream", route => route.fulfill({
        status: 200,
        headers: { "content-type": "application/x-ndjson" },
        body: [
            JSON.stringify({ type: "start", provider: "openai", model: "gpt-5.1" }),
            JSON.stringify({ type: "line", text: "Optimized browser answer." }),
            JSON.stringify({
                type: "response_done",
                response: {
                    provider: "openai",
                    model: "gpt-5.1",
                    text: "Optimized browser answer.",
                    latency_ms: 300,
                    estimated_cost: 0.001,
                    token_usage: {
                        prompt_tokens: 10,
                        completion_tokens: 10,
                        total_tokens: 20,
                    },
                    web_source_items: [],
                },
            }),
            JSON.stringify({ type: "done", session_id: "optimized-session" }),
            "",
        ].join("\n"),
    }));

    await page.getByRole("switch", { name: "Prompt optimization" }).click();
    await page.locator("#promptInput").fill("rough browser prompt");
    await page.locator("#submitBtn").click();

    const pendingTurn = page.locator("[data-turn-id]").last();
    await expect(pendingTurn.getByRole("status")).toContainText("Improving your prompt");
    await expect(page.locator("#promptInput")).toHaveValue("");
    await expect(pendingTurn.locator("article")).toHaveCount(0);
    await expect(page.getByRole("tablist", { name: "Compare model responses" })).toHaveCount(0);

    releaseOptimization();

    await expect(pendingTurn.locator("article")).toHaveCount(1);
    await expect(pendingTurn).toContainText("Optimized browser answer.");
});

test("Cortex-managed Auto budget preserves incomplete output for retry", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });

    const requestBodies = [];
    await page.route("**/v1/chat/stream", async route => {
        const body = JSON.parse(route.request().postData() || "{}");
        requestBodies.push(body);
        const retry = requestBodies.length > 1;
        const text = retry ? "Completed answer with more room." : "Partial answer kept for the user.";
        const profile = retry ? "deep" : "auto";
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", provider: "openai", model: "gpt-5.1" },
                { type: "line", text },
                {
                    type: "response_done",
                    response: {
                        request_id: retry ? "retry-budget-request" : "initial-budget-request",
                        provider: "openai",
                        model: "gpt-5.1",
                        text,
                        finish_reason: retry ? "stop" : "length",
                        completion_status: retry ? "complete" : "incomplete",
                        stop_cause: retry ? "natural" : "token_limit",
                        generation_budget: {
                            profile,
                            requested_max_output_tokens: retry ? 12288 : 8192,
                            effective_max_output_tokens: retry ? 12288 : 8192,
                            requested_reasoning_mode: "on",
                            effective_reasoning_mode: "standard",
                            requested_reasoning_effort: "low",
                            effective_reasoning_effort: "low",
                            reasoning_disable_supported: true,
                            reasoning_counts_against_output: true,
                            policy_version: "generation-budget-v3",
                        },
                        retry_with_more_room: {
                            available: !retry,
                            recommended_profile: retry ? null : "deep",
                        },
                        latency_ms: 300,
                        estimated_cost: 0.001,
                        token_usage: {
                            prompt_tokens: 10,
                            completion_tokens: retry ? 20 : 8192,
                            total_tokens: retry ? 30 : 8202,
                        },
                        web_source_items: [],
                    },
                },
                { type: "done", session_id: "generation-budget-session" },
            ]),
        });
    });

    const smart = page.getByRole("switch", { name: "Smart routing" });
    if ((await smart.getAttribute("aria-checked")) === "true") await smart.click();
    await expect(page.getByRole("combobox", { name: "Answer depth" })).toHaveCount(0);

    await page.locator("#promptInput").fill("Explain the provider budget contract");
    await page.locator("#submitBtn").click();

    await expect(page.getByText("Partial answer kept for the user.")).toBeVisible();
    await expect(page.getByText("Response stopped at its token limit.")).toBeVisible();
    expect(requestBodies[0].generation).toEqual({
        profile: "auto",
        reasoning: { mode: "on", effort: "low" },
    });

    await page.getByRole("button", { name: "Retry with more room" }).click();
    await expect.poll(() => requestBodies.length).toBe(2);
    expect(requestBodies[1].generation).toEqual({
        profile: "deep",
        reasoning: { mode: "on", effort: "low" },
    });
    await expect(page.getByText("Completed answer with more room.")).toBeVisible();
});

test("desktop Compare picker remains visible and selectable", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.locator("#btnCompareMode").click();

    const connector = page.getByTestId("compare-connector");
    await expect(connector).toHaveCount(1);
    await expect(connector).toHaveCSS("width", "26px");
    await expect(connector).toHaveCSS("border-top-width", "1px");
    await expect(connector).toHaveCSS("border-radius", "999px");

    const connectorGeometry = await connector.evaluate(element => {
        const connectorRect = element.getBoundingClientRect();
        const firstModelRect = document.querySelector("#compareModel1Wrap")?.getBoundingClientRect();
        const secondModelRect = document.querySelector("#compareModel2Wrap")?.getBoundingClientRect();
        return {
            connectorCenter: connectorRect.left + connectorRect.width / 2,
            firstRight: firstModelRect?.right ?? 0,
            secondLeft: secondModelRect?.left ?? 0,
        };
    });
    expect(connectorGeometry.connectorCenter).toBeGreaterThan(connectorGeometry.firstRight);
    expect(connectorGeometry.connectorCenter).toBeLessThan(connectorGeometry.secondLeft);

    const select = page.locator("#compareModel2");
    const currentValue = await select.inputValue();
    const target = await select.locator("option:not(:disabled)").evaluateAll(
        (options, selectedValue) => {
            const option = options.find(candidate => candidate.value !== selectedValue);
            if (!option) return null;
            const separator = option.value.indexOf(":");
            return {
                value: option.value,
                provider: separator >= 0 ? option.value.slice(0, separator) : "",
            };
        },
        currentValue,
    );
    expect(target).not.toBeNull();

    await page.getByRole("button", { name: /Compare model 2:/ }).click();
    const listbox = page.getByRole("listbox", { name: "Compare model 2 options" });
    await expect(listbox).toBeVisible();
    await expect(listbox).toHaveAttribute("data-picker-view", "providers");
    await expect(listbox).toHaveAttribute("data-picker-interaction", "hover");
    const providerOption = listbox.locator(`[data-provider-key="${target.provider}"]`);
    await providerOption.hover();
    await expect(listbox).toHaveAttribute("data-picker-view", "models");
    await expect(listbox.getByRole("group", { name: "Providers" })).toBeVisible();
    await expect(listbox.locator(`[data-model-key="${target.value}"]`)).toBeVisible();

    await page.mouse.move(2, 2);
    await expect(listbox).toHaveAttribute("data-picker-view", "providers");

    await providerOption.hover();
    await expect(listbox).toHaveAttribute("data-picker-view", "models");
    await listbox.locator(`[data-model-key="${target.value}"]`).click();
    await expect(select).toHaveValue(target.value);
});

test("iPad landscape keeps the desktop workspace usable", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 1024, height: 768 });

    const sidebar = page.locator("aside[aria-label='Primary navigation']");
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Ask", exact: true })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Compare", exact: true })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Work", exact: true })).toBeVisible();
    await expect(page.locator("#btnSingleMode")).toBeVisible();
    await expect(page.locator("#btnSingleMode svg")).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toBeHidden();
    const composerWidth = await page.locator("#promptInput").evaluate(element => {
        return element.parentElement?.parentElement?.getBoundingClientRect().width ?? 0;
    });
    expect(composerWidth).toBeLessThanOrEqual(860);
    await expectNoHorizontalOverflow(page);
});

async function expectChipTooltip(page, switchName, tooltipText) {
    const chip = page.getByRole("switch", { name: switchName });
    const tooltip = page.locator('[role="tooltip"]').filter({ hasText: tooltipText });
    await expect(tooltip).toHaveAttribute("id", await chip.getAttribute("aria-describedby"));
    await chip.hover();
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toHaveCSS("opacity", "1");
}

async function expectCenteredComposer(page) {
    const composer = page.locator("[data-composer-placement='center']");
    const card = page.locator("#promptInput").locator("xpath=../..");
    await expect.poll(async () => {
        const composerBounds = await composer.boundingBox();
        const cardBounds = await card.boundingBox();
        if (!composerBounds || !cardBounds) return Number.POSITIVE_INFINITY;
        const composerCenter = composerBounds.y + composerBounds.height / 2;
        const cardCenter = cardBounds.y + cardBounds.height / 2;
        return Math.abs(composerCenter - cardCenter);
    }).toBeLessThanOrEqual(1);
}

async function chipVisualStyle(chip) {
    return chip.evaluate(element => {
        const style = getComputedStyle(element);
        return {
            backgroundColor: style.backgroundColor,
            borderColor: style.borderColor,
            boxShadow: style.boxShadow,
            color: style.color,
        };
    });
}

async function expectMatchingChipStyles(first, second) {
    await expect.poll(async () => {
        const firstStyle = await chipVisualStyle(first);
        const secondStyle = await chipVisualStyle(second);
        return JSON.stringify(firstStyle) === JSON.stringify(secondStyle);
    }).toBe(true);
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

async function expectSoftComposerShell(page) {
    const textarea = page.locator("#promptInput");
    const composer = textarea.locator("xpath=../..");
    // Blur any prior focus and poll: the shell transitions border-color and
    // box-shadow for 160ms after focus moves, so a one-shot snapshot races it.
    await page.evaluate(() => document.activeElement?.blur?.());
    await expect.poll(
        () => composer.evaluate(element => getComputedStyle(element).borderColor),
    ).toBe("rgb(235, 237, 240)");
    const idle = await composer.evaluate(element => ({
        boxShadow: getComputedStyle(element).boxShadow,
    }));
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
}

test("iPad portrait switches to mobile navigation without overlap", async ({ responsiveApp }) => {
    const { page } = responsiveApp;
    await page.setViewportSize({ width: 820, height: 1180 });

    await expect(page.locator("aside[aria-label='Primary navigation']")).toBeHidden();
    await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toBeVisible();
    await openMobilePanel(page, "Compare");

    const metrics = await page.evaluate(() => {
        const textarea = document.querySelector("#promptInput");
        const composer = textarea?.parentElement?.parentElement?.parentElement;
        const nav = document.querySelector("nav[aria-label='Mobile navigation']");
        return {
            composerBottom: composer?.getBoundingClientRect().bottom ?? Number.POSITIVE_INFINITY,
            navTop: nav?.getBoundingClientRect().top ?? 0,
        };
    });
    expect(metrics.composerBottom).toBeLessThanOrEqual(metrics.navTop + 1);
    await expectNoHorizontalOverflow(page);
});
