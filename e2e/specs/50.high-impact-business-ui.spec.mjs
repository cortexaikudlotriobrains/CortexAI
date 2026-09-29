/**
 * High-impact UI E2E coverage for business-critical flows.
 *
 * Each test includes a top comment that states the exact use case it protects.
 */
import { test, expect } from "../fixtures/live-e2e.mjs";
import { promptLibrary } from "../helpers/prompts.mjs";
import {
    chooseDistinctCompareModels,
    ensureMode,
    setToggle,
    startNewChat,
    submitAskPrompt,
    submitComparePrompt,
} from "../helpers/ui.mjs";
import { latestRequest, waitForSnapshot } from "./_helpers.mjs";

function parseModelKey(value) {
    const raw = String(value || "");
    const [provider, ...rest] = raw.split(":");
    return {
        provider: String(provider || "").trim().toLowerCase(),
        model: rest.join(":").trim(),
    };
}

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

async function submitPromptDirect(page, prompt) {
    await page.locator("#promptInput").fill(prompt);
    await page.locator("#submitBtn").click();
}

async function waitForNonEmptyCardText(page, index, timeoutMs = 20_000) {
    const locator = page.locator(`#response-text-${index}`);
    await expect(locator).toBeVisible({ timeout: timeoutMs });
    await expect.poll(
        async () => String(await locator.textContent() || "").trim(),
        { timeout: timeoutMs, message: `Timed out waiting for text in response card ${index}` },
    ).not.toBe("");
    return String(await locator.textContent() || "").trim();
}

function makeChatDoneResponse({
    provider = "openai",
    model = "gpt-5.1",
    text = "",
    sessionId = null,
    error = null,
    webSourceItems = [],
} = {}) {
    return {
        provider,
        model,
        text,
        finish_reason: error ? "error" : "completed",
        token_usage: error
            ? null
            : {
                prompt_tokens: 120,
                completion_tokens: 140,
                total_tokens: 260,
            },
        estimated_cost: error ? 0 : 0.0001,
        error,
        web_source_items: webSourceItems,
        session_id: sessionId,
    };
}

/**
 * Use case: protect users from "blank answer" regressions when the backend returns
 * a successful stream completion with an empty text payload.
 */
test("single chat renders explicit empty-response placeholder for empty successful payloads", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    await routeOnce(page, chatStreamUrl, async route => {
        const payload = toNdjson([
            { type: "start", mode: "single", web_source_items: [] },
            {
                type: "response_done",
                response: makeChatDoneResponse({
                    provider: "openai",
                    model: "gpt-5.1",
                    text: "",
                }),
            },
            { type: "done", session_id: null },
        ]);
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: payload,
        });
    });

    const nextIndex = await page.locator("[id^='response-text-']").count();
    await submitPromptDirect(page, liveApp.withPromptMarker("Empty-output guardrail probe."));

    const text = await waitForNonEmptyCardText(page, nextIndex);
    expect(text).toBe("(empty response)");
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.locator("#submitBtn")).toHaveAttribute("aria-label", "Send message");
});

/**
 * Use case: when providers return rate-limit responses, users should see an actionable
 * failure state on the affected answer with a retry action instead of a raw backend message.
 */
test("single chat 429 failure shows an actionable response error", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    await routeOnce(page, chatStreamUrl, async route => {
        await route.fulfill({
            status: 429,
            contentType: "application/json",
            body: JSON.stringify({ detail: "rate limit exceeded" }),
        });
    });

    await submitPromptDirect(page, liveApp.withPromptMarker("429 banner + retry probe."));

    const failedCard = page.locator('[data-response-error="true"]').last();
    await expect(failedCard).toBeVisible();
    await expect(failedCard).toContainText(/too many requests/i);
    await expect(failedCard).not.toContainText(/rate limit exceeded/i);
    await expect(failedCard.getByRole("button", { name: "Regenerate response" })).toBeVisible();
    await expect(page.locator("#submitBtn")).toHaveAttribute("aria-label", "Send message");
});

/**
 * Use case: upstream 5xx outages must produce clear recovery guidance without
 * exposing infrastructure wording returned by the backend.
 */
test("single chat 503 failure maps to safe recovery copy", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);

    await routeOnce(page, chatStreamUrl, async route => {
        await route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: "service unavailable" }),
        });
    });

    await submitPromptDirect(page, liveApp.withPromptMarker("503 service copy probe."));

    const failedCard = page.locator('[data-response-error="true"]').last();
    await expect(failedCard).toContainText(/couldn't complete this request/i);
    await expect(failedCard).toContainText(/try again or choose another model/i);
    await expect(failedCard).not.toContainText(/service unavailable/i);
});

/**
 * Use case: gateway timeout conditions should be classified as timeout in the UI,
 * with copy that tells users the request took too long (instead of generic errors).
 */
test("single chat 504 failure maps to timeout UI copy", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);

    await routeOnce(page, chatStreamUrl, async route => {
        await route.fulfill({
            status: 504,
            contentType: "application/json",
            body: JSON.stringify({ detail: "gateway timeout" }),
        });
    });

    await submitPromptDirect(page, liveApp.withPromptMarker("504 timeout copy probe."));

    const failedCard = page.locator('[data-response-error="true"]').last();
    await expect(failedCard).toContainText(/took too long/i);
    await expect(failedCard).not.toContainText(/gateway timeout/i);
    await expect(failedCard.getByRole("button", { name: "Regenerate response" })).toBeVisible();
});

/**
 * Use case: transient outages should recover in-place via Retry without forcing users
 * to retype prompts; second attempt success should replace the failed response.
 */
test("retry action recovers after transient 503 and returns a successful response", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;
    let attempts = 0;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    await page.route(chatStreamUrl, async route => {
        attempts += 1;
        if (attempts === 1) {
            await route.fulfill({
                status: 503,
                contentType: "application/json",
                body: JSON.stringify({ detail: "temporary upstream outage" }),
            });
            return;
        }

        const successText = "Recovered answer after retry.";
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", web_source_items: [] },
                { type: "line", text: "Recovered answer " },
                { type: "line", text: "after retry." },
                {
                    type: "response_done",
                    response: makeChatDoneResponse({
                        provider: "openai",
                        model: "gpt-5.1",
                        text: successText,
                    }),
                },
                { type: "done", session_id: null },
            ]),
        });
    });

    await submitPromptDirect(page, liveApp.withPromptMarker("Retry path probe for transient failure."));
    const retryButton = page.getByRole("button", { name: "Regenerate response" }).last();
    await expect(page.locator('[data-response-error="true"]').last()).toBeVisible();
    await expect(retryButton).toBeVisible();

    await retryButton.evaluate(node => {
        if (node && typeof node.click === "function") {
            node.click();
        }
    });
    await expect(page.locator("[id^='response-text-']", { hasText: "Recovered answer after retry." }).first()).toBeVisible();
    expect(attempts).toBeGreaterThanOrEqual(2);

    await page.unroute(chatStreamUrl);
});

/**
 * Use case: compare-mode should remain useful when one model fails and another succeeds,
 * rendering both cards plus an accurate success ratio in the compare summary.
 */
test("compare stream with one failed model still renders mixed-result cards and summary", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const compareStreamUrl = `${config.apiBaseUrl}/v1/compare/stream`;

    await ensureMode(page, "compare");
    await setToggle(page, "#routeResearchBtn", false);
    await chooseDistinctCompareModels(page, 2);

    const selectedOne = parseModelKey(await page.locator("#compareModel1").inputValue());
    const selectedTwo = parseModelKey(await page.locator("#compareModel2").inputValue());
    const cardsBefore = await page.locator("[id^='response-text-']").count();
    const firstIndex = cardsBefore;
    const secondIndex = cardsBefore + 1;

    const failedResponse = makeChatDoneResponse({
        provider: selectedOne.provider,
        model: selectedOne.model,
        text: "",
        error: {
            code: "provider_error",
            message: "temporary upstream failure",
            provider: selectedOne.provider,
            retryable: true,
            details: {},
        },
    });
    const successfulResponse = makeChatDoneResponse({
        provider: selectedTwo.provider,
        model: selectedTwo.model,
        text: "Recovery answer paragraph one.\nSecond supporting line.",
    });

    await routeOnce(page, compareStreamUrl, async route => {
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "compare", target_count: 2, web_source_items: [] },
                { type: "line", index: 0, text: "Error: temporary upstream failure" },
                { type: "response_done", index: 0, response: failedResponse },
                { type: "line", index: 1, text: "Recovery answer paragraph one.\n" },
                { type: "line", index: 1, text: "Second supporting line." },
                { type: "response_done", index: 1, response: successfulResponse },
                {
                    type: "done",
                    compare: {
                        responses: [failedResponse, successfulResponse],
                        total_tokens: 260,
                        total_cost: 0.0001,
                        success_count: 1,
                        error_count: 1,
                    },
                },
            ]),
        });
    });

    await submitPromptDirect(page, liveApp.withPromptMarker("Compare mixed failure/success resilience."));

    const failedCard = page.locator(`#response-text-${firstIndex}`).locator("xpath=..");
    await expect(failedCard).toHaveAttribute("data-response-error", "true");
    await expect(page.locator(`#response-text-${firstIndex}`)).toContainText("The model couldn't complete this request");
    await expect(page.locator(`#response-text-${firstIndex}`)).not.toContainText("temporary upstream failure");
    await expect(page.locator(`#response-text-${secondIndex}`)).toContainText("Recovery answer paragraph one.");
    await expect(page.locator(".compare-summary-card")).toContainText("1 succeeded");
    await expect(page.locator(".compare-summary-card")).toContainText("1 errors");
});

/**
 * Use case: analysts switch between Ask and Compare repeatedly; session continuity must
 * preserve one backend thread so context and audit trails stay coherent.
 */
test("ask then compare then ask again keeps one backend session id across all turns", async ({ liveApp }) => {
    const { page, config } = liveApp;

    await ensureMode(page, "single");
    await setToggle(page, "#routeSmartBtn", true);
    await setToggle(page, "#routeResearchBtn", false);
    await setToggle(page, "#routeOptimizeBtn", false);

    await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.smartAsk), config);
    const afterAsk = await waitForSnapshot(liveApp, { minRequests: 1 });
    const firstRequest = latestRequest(afterAsk);

    await ensureMode(page, "compare");
    await setToggle(page, "#routeResearchBtn", false);
    await chooseDistinctCompareModels(page, 2);
    await submitComparePrompt(page, liveApp.withPromptMarker(promptLibrary.compare), config, 2);

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);
    await submitAskPrompt(page, liveApp.withPromptMarker(promptLibrary.tokensOne), config);

    const finalSnapshot = await waitForSnapshot(liveApp, {
        sessionId: null,
        predicate: current => current.requests.length >= 4,
        timeoutMs: config.timeouts.compareMs,
    });

    expect(finalSnapshot.requests).toHaveLength(4);
    expect(finalSnapshot.requests.every(row => row.session_id === firstRequest.session_id)).toBe(true);
});

/**
 * Use case: enterprise users ask for strict, structured outputs; the UI must correctly
 * render headings, ordered lists, and tables from streamed markdown.
 */
test("single chat renders structured markdown (headings list table) for exec-style outputs", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);

    const markdownText = [
        "## Executive Summary",
        "",
        "1. Revenue risk is moderate.",
        "Revenue controls remain active.",
        "",
        "2. Security posture is improving.",
        "Hardening work is underway.",
        "",
        "3. Rollout readiness is high.",
        "",
        "| Area | Risk | Owner |",
        "| --- | --- | --- |",
        "| API | Medium | Platform |",
        "| UI | Low | Product |",
    ].join("\n");

    await routeOnce(page, chatStreamUrl, async route => {
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", web_source_items: [] },
                { type: "line", text: "## Executive Summary\n\n1. Revenue risk is moderate.\n" },
                { type: "line", text: "2. Security posture is improving.\n3. Rollout readiness is high.\n" },
                {
                    type: "response_done",
                    response: makeChatDoneResponse({
                        provider: "openai",
                        model: "gpt-5.1",
                        text: markdownText,
                    }),
                },
                { type: "done", session_id: null },
            ]),
        });
    });

    const nextIndex = await page.locator("[id^='response-text-']").count();
    await submitPromptDirect(page, liveApp.withPromptMarker("Exec summary strict-format rendering probe."));

    const responseRoot = page.locator(`#response-text-${nextIndex}`);
    await expect(responseRoot.locator("h2")).toContainText("Executive Summary");
    await expect(responseRoot.locator("ol li")).toHaveCount(3);
    await expect(responseRoot.locator("ol")).toHaveCount(1);
    await expect(responseRoot.locator("table")).toBeVisible();
});

/**
 * Use case: if a model returns an error in chat mode, source chips should not be shown
 * even when source metadata exists, to avoid fake confidence in failed outputs.
 */
test("error chat cards suppress web-source chips even when source metadata is present", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const chatStreamUrl = `${config.apiBaseUrl}/v1/chat/stream`;

    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", true);

    await routeOnce(page, chatStreamUrl, async route => {
        const sources = [{ title: "Example Source", url: "https://example.com/report" }];
        await route.fulfill({
            status: 200,
            headers: { "content-type": "application/x-ndjson" },
            body: toNdjson([
                { type: "start", mode: "single", web_source_items: sources },
                {
                    type: "response_done",
                    response: makeChatDoneResponse({
                        provider: "openai",
                        model: "gpt-5.1",
                        text: "",
                        error: {
                            code: "provider_error",
                            message: "provider unavailable",
                            provider: "openai",
                            retryable: true,
                            details: {},
                        },
                        webSourceItems: sources,
                    }),
                },
                { type: "done", session_id: null },
            ]),
        });
    });

    const nextIndex = await page.locator("[id^='response-text-']").count();
    await submitPromptDirect(page, liveApp.withPromptMarker("Error card should hide sources."));

    const errorCard = page.locator(`#response-text-${nextIndex}`).locator("xpath=..");
    await expect(errorCard).toHaveAttribute("data-response-error", "true");
    await expect(page.locator(`#response-text-${nextIndex}`)).toContainText("provider unavailable");
    await expect(errorCard.getByRole("button", { name: /^Sources:/ })).toHaveCount(0);
});

/**
 * Use case: compare threads are often revisited later; reopening a prior compare thread
 * must restore that backend session so follow-up requests do not fork into a new thread.
 */
test("reopening a compare history thread keeps follow-up requests on the original compare session", async ({ liveApp }) => {
    const { page, config } = liveApp;
    const compareAnchorPrompt = "Compare thread anchor: fail-fast versus retries in orchestration.";

    await ensureMode(page, "compare");
    await setToggle(page, "#routeResearchBtn", false);
    await chooseDistinctCompareModels(page, 2);
    await submitComparePrompt(page, liveApp.withPromptMarker(compareAnchorPrompt), config, 2);

    const afterCompare = await waitForSnapshot(liveApp, {
        sessionId: null,
        predicate: current => current.requests.length >= 2,
    });
    const compareSessionId = afterCompare.requests[0].session_id;
    expect(afterCompare.requests.every(row => row.session_id === compareSessionId)).toBe(true);

    await startNewChat(page);
    await ensureMode(page, "single");
    await setToggle(page, "#routeResearchBtn", false);
    await submitAskPrompt(page, liveApp.withPromptMarker("Secondary thread for history split."), config);

    const splitSnapshot = await waitForSnapshot(liveApp, {
        sessionId: null,
        predicate: current => current.requests.length >= 3,
    });
    const secondaryRequest = splitSnapshot.requests.find(row =>
        String(row.prompt_text || "").includes("Secondary thread for history split."),
    );
    expect(secondaryRequest).toBeTruthy();

    const compareHistoryEntry = page.locator(
        `button[data-history-thread][data-session-id="${compareSessionId}"]`,
    ).first();
    await expect(compareHistoryEntry).toBeVisible({ timeout: config.timeouts.historyRestoreMs });
    await compareHistoryEntry.click();

    await submitAskPrompt(page, liveApp.withPromptMarker("Follow-up after reopening compare thread."), config);
    const finalSnapshot = await waitForSnapshot(liveApp, {
        sessionId: null,
        predicate: current => current.requests.length >= 4,
    });
    const followUpRequest = finalSnapshot.requests.find(row =>
        String(row.prompt_text || "").includes("Follow-up after reopening compare thread."),
    );
    expect(followUpRequest).toBeTruthy();
    expect(followUpRequest?.session_id).toBe(compareSessionId);
});
