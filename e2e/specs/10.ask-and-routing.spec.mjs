/**
 * Tier A and Tier B Ask-mode routing coverage.
 *
 * These scenarios exercise the main smart-routing controls through the live UI and
 * then verify the backend's persisted routing choice instead of trusting the DOM alone.
 */
import { test, expect } from "../fixtures/live-e2e.mjs";
import { promptLibrary } from "../helpers/prompts.mjs";
import { ensureMode, expandSourcesForCard, setToggle, startNewChat, submitAskPrompt } from "../helpers/ui.mjs";
import { routingBoundaryScenarios } from "../test-data/routing-outcomes.mjs";
import { expectSummaryMatchesRequest, latestRequest, waitForSnapshot } from "./_helpers.mjs";

function toNdjson(events) {
    return events.map(event => `${JSON.stringify(event)}\n`).join("");
}

async function routeOnce(page, url, handler) {
    let consumed = false;
    await page.route(url, async route => {
        if (consumed) {
            await route.continue();
            return;
        }
        consumed = true;
        await handler(route);
    });
}

function makeChatStreamBody(text = "Request received.") {
    return toNdjson([
        { type: "start", mode: "single", web_source_items: [] },
        { type: "line", text },
        {
            type: "response_done",
            response: {
                provider: "openai",
                model: "gpt-5.1",
                text,
                finish_reason: "completed",
                token_usage: {
                    prompt_tokens: 12,
                    completion_tokens: 8,
                    total_tokens: 20,
                },
                estimated_cost: 0.0001,
                web_source_items: [],
                session_id: null,
            },
        },
        { type: "done", session_id: null },
    ]);
}

test("ask mode with smart routing returns streamed response and persisted selection", async ({ liveApp }) => {
    const { page, config, runState } = liveApp;

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    const result = await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.smartAsk), config);
    expect(result.stream.distinctSnapshots).toBeGreaterThanOrEqual(2);
    expect(result.text.length).toBeGreaterThan(20);

    const snapshot = await waitForSnapshot(liveApp, { minRequests: 1 });
    const request = latestRequest(snapshot);

    expect(snapshot.responses).toHaveLength(1);
    expect(snapshot.routingDecisions.length).toBeGreaterThanOrEqual(1);
    expect(snapshot.routingAttempts.length).toBeGreaterThanOrEqual(1);
    expect(new Set(snapshot.requests.map(row => row.session_id)).size).toBe(1);
    expectSummaryMatchesRequest(result.summaryText, request);
});

test("prompt intent turns automatic web search on then off in the same session", async ({ liveApp }) => {
    const { page, config } = liveApp;

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeOptimizeBtn", false);

    const first = await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.webResearch), config);
    const firstCardText = await page.locator(`#response-text-${first.index}`).innerText();
    const firstSnapshot = await waitForSnapshot(liveApp, { minRequests: 1 });

    expect(firstSnapshot.requests).toHaveLength(1);
    expect(firstSnapshot.routingDecisions).toHaveLength(1);
    expect(firstSnapshot.routingDecisions[0].research_mode).toBe("on");

    const firstCard = page.locator(`#response-text-${first.index}`).locator("xpath=..");
    const firstSources = firstCard.getByRole("button", { name: /^Sources:/ });
    if (await firstSources.count()) {
        const sourceLinks = await expandSourcesForCard(page, first.index);
        expect(await sourceLinks.count()).toBeGreaterThan(0);
    } else {
        expect(firstCardText.toLowerCase()).not.toMatch(
            /can't browse|cannot browse|do not have access to real-time|don't have access to real-time|real-time web browsing capabilities/,
        );
    }

    const second = await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.noWebFollowUp), config);
    const secondCard = page.locator(`#response-text-${second.index}`).locator("xpath=..");
    await expect(secondCard.getByRole("button", { name: /^Sources:/ })).toHaveCount(0);

    const snapshot = await waitForSnapshot(liveApp, { minRequests: 2 });
    expect(snapshot.requests).toHaveLength(2);
    expect(snapshot.routingDecisions).toHaveLength(2);
    expect(snapshot.routingDecisions.map(row => row.research_mode)).toEqual(["on", "off"]);
    expect(new Set(snapshot.requests.map(row => row.session_id)).size).toBe(1);
    expect(snapshot.requests[0].session_id).toBe(snapshot.requests[1].session_id);
});

test("prompt optimizer on then off in the same session changes request flow", async ({ liveApp }) => {
    const { page, config, network } = liveApp;

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", true);

    const optimizeCallsBefore = network.countByPath("/v1/optimize");
    await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.optimizer), config);
    const optimizeCallsAfterFirst = network.countByPath("/v1/optimize");
    expect(optimizeCallsAfterFirst).toBeGreaterThan(optimizeCallsBefore);

    await setToggle(page, "#routeOptimizeBtn", false);
    const optimizeCallsBeforeSecond = network.countByPath("/v1/optimize");
    await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.tokensTwo), config);
    expect(network.countByPath("/v1/optimize")).toBe(optimizeCallsBeforeSecond);

    const snapshot = await waitForSnapshot(liveApp, {
        minRequests: 2,
        predicate: current => current.requests.length >= 2,
    });
    expect(snapshot.requests.length).toBeGreaterThanOrEqual(2);
    const sessionId = snapshot.requests[0].session_id;
    expect(snapshot.requests.every(row => row.session_id === sessionId)).toBe(true);
});

test("improve flow shows optimization status and sends optimized prompt", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const optimizeUrl = `${config.apiBaseUrl}/v1/optimize`;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;
    const rawPrompt = liveApp.withPromptMarker("Turn this loose ask into a crisp implementation prompt.");
    const optimizedPrompt = `${rawPrompt}\n\nReturn an implementation-ready answer with concrete next steps.`;
    let optimizeRequestBody = null;
    let chatRequestBody = null;
    let releaseOptimize;
    const optimizeGate = new Promise(resolve => {
        releaseOptimize = resolve;
    });

    await routeOnce(page, optimizeUrl, async route => {
        optimizeRequestBody = JSON.parse(route.request().postData() || "{}");
        await optimizeGate;
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                original_prompt: rawPrompt,
                optimized_prompt: optimizedPrompt,
                was_optimized: true,
                server_optimization_enabled: true,
                optimization_status: "optimized",
                fallback_reason: null,
            }),
        });
    });

    await routeOnce(page, chatStreamUrl, async route => {
        chatRequestBody = JSON.parse(route.request().postData() || "{}");
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", web_source_items: [] },
                { type: "line", text: "Optimized " },
                { type: "line", text: "request received." },
                {
                    type: "response_done",
                    response: {
                        provider: "openai",
                        model: "gpt-5.1",
                        text: "Optimized request received.",
                        finish_reason: "completed",
                        token_usage: {
                            prompt_tokens: 12,
                            completion_tokens: 8,
                            total_tokens: 20,
                        },
                        estimated_cost: 0.0001,
                        web_source_items: [],
                        session_id: null,
                    },
                },
                { type: "done", session_id: null },
            ]),
        });
    });

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", true);

    const nextIndex = await page.locator("[id^='response-text-']").count();
    await page.locator("#promptInput").fill(rawPrompt);
    await page.locator("#submitBtn").click();

    await expect(page.getByRole("status")).toContainText("Improving your prompt");
    expect(optimizeRequestBody?.prompt).toBe(rawPrompt);
    expect(optimizeRequestBody?.context_hint).toBeUndefined();
    expect(chatRequestBody).toBeNull();
    await expect(page.locator("[id^='response-text-']")).toHaveCount(nextIndex);

    releaseOptimize();

    await expect.poll(
        async () => String(await page.locator(".optimization-user-text").textContent() || ""),
    ).toBe(optimizedPrompt);
    await expect(page.locator(".chat-message-user")).toHaveCount(1);
    await expect.poll(() => chatRequestBody?.prompt || "").toBe(optimizedPrompt);
    await expect(page.locator(`#response-text-${nextIndex}`)).toContainText("Optimized request received.");
    await expect(page.locator("#submitBtn")).toHaveAttribute(
        "aria-label",
        "Send message",
        { timeout: 15_000 },
    );
});

test("improve flow explains when original prompt is kept", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const optimizeUrl = `${config.apiBaseUrl}/v1/optimize`;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;
    const rawPrompt = liveApp.withPromptMarker("Summarize this clearly in one paragraph.");
    let chatRequestBody = null;

    await routeOnce(page, optimizeUrl, async route => {
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                original_prompt: rawPrompt,
                optimized_prompt: rawPrompt,
                was_optimized: false,
                server_optimization_enabled: true,
                optimization_status: "kept_original",
                fallback_reason: "already_clear",
            }),
        });
    });

    await routeOnce(page, chatStreamUrl, async route => {
        chatRequestBody = JSON.parse(route.request().postData() || "{}");
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: makeChatStreamBody("Original prompt received."),
        });
    });

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", true);

    await page.locator("#promptInput").fill(rawPrompt);
    await page.locator("#submitBtn").click();

    await expect(page.locator(".optimization-user-text")).toHaveText(rawPrompt);
    await expect(page.locator(".optimization-result-note")).toHaveText("Already clear — sent as-is");
    await expect.poll(() => chatRequestBody?.prompt || "").toBe(rawPrompt);
});

test("improve flow explains fallback when prompt refinement is unavailable", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const optimizeUrl = `${config.apiBaseUrl}/v1/optimize`;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;
    const rawPrompt = liveApp.withPromptMarker("Draft a short launch announcement.");
    let chatRequestBody = null;

    await routeOnce(page, optimizeUrl, async route => {
        await route.fulfill({
            status: 503,
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                detail: {
                    code: "optimization_unavailable",
                    message: "Prompt refinement unavailable.",
                },
            }),
        });
    });

    await routeOnce(page, chatStreamUrl, async route => {
        chatRequestBody = JSON.parse(route.request().postData() || "{}");
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: makeChatStreamBody("Fallback prompt received."),
        });
    });

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", true);

    await page.locator("#promptInput").fill(rawPrompt);
    await page.locator("#submitBtn").click();

    await expect(page.locator(".optimization-user-text")).toHaveText(rawPrompt);
    await expect(page.locator(".optimization-result-note")).toHaveText("Already clear — sent as-is");
    await expect.poll(() => chatRequestBody?.prompt || "").toBe(rawPrompt);
});

test("improve flow sends compact context for follow-up prompts", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const optimizeUrl = `${config.apiBaseUrl}/v1/optimize`;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;
    const firstPrompt = liveApp.withPromptMarker("List practical reasons humans may mine asteroids.");
    const followUpPrompt = "I was talking about mining the asteroids";
    const optimizedFollowUp = "Explain practical reasons humans may mine asteroids.";
    let optimizeRequestBody = null;
    const chatRequestBodies = [];

    await page.route(chatStreamUrl, async route => {
        chatRequestBodies.push(JSON.parse(route.request().postData() || "{}"));
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: makeChatStreamBody(chatRequestBodies.length === 1
                ? "Asteroid mining context saved."
                : "Follow-up prompt received."),
        });
    });

    await routeOnce(page, optimizeUrl, async route => {
        optimizeRequestBody = JSON.parse(route.request().postData() || "{}");
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
                original_prompt: followUpPrompt,
                optimized_prompt: optimizedFollowUp,
                was_optimized: true,
                server_optimization_enabled: true,
                optimization_status: "optimized",
                fallback_reason: null,
            }),
        });
    });

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    await page.locator("#promptInput").fill(firstPrompt);
    await page.locator("#submitBtn").click();
    await expect(page.getByText("Asteroid mining context saved.")).toBeVisible();

    await setToggle(page, "#routeOptimizeBtn", true);
    await page.locator("#promptInput").fill(followUpPrompt);
    await page.locator("#submitBtn").click();

    await expect.poll(() => optimizeRequestBody?.prompt || "").toBe(followUpPrompt);
    expect(optimizeRequestBody?.context_hint).toContain("asteroids");
    expect(optimizeRequestBody?.context?.conversation_history.length).toBeLessThanOrEqual(4);
    await expect.poll(() => chatRequestBodies.at(-1)?.prompt || "").toBe(optimizedFollowUp);
});

test("routing policy boundary coverage uses allowed outcomes and varies across prompt classes", async ({ liveApp }) => {
    const { page, config, runState } = liveApp;
    const seenRequestIds = new Set();
    const outcomes = [];

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    for (let index = 0; index < routingBoundaryScenarios.length; index += 1) {
        const scenario = routingBoundaryScenarios[index];
        if (index > 0) {
            await startNewChat(page);
        }

        const result = await submitAskPrompt(page, liveApp.withPromptMarker(scenario.prompt), config);
        const snapshot = await waitForSnapshot(liveApp, {
            sessionId: null,
            predicate: current => current.requests.length > seenRequestIds.size,
            timeoutMs: config.timeouts.askMs,
        });

        const newRequests = snapshot.requests.filter(row => !seenRequestIds.has(row.id));
        expect(newRequests).toHaveLength(1);

        const request = newRequests[0];
        seenRequestIds.add(request.id);
        outcomes.push(`${request.provider}:${request.model}`);

        expect(scenario.allowedProviders).toContain(request.provider);
        expectSummaryMatchesRequest(result.summaryText, request);
    }

    expect(new Set(outcomes).size).toBeGreaterThanOrEqual(2);
});
