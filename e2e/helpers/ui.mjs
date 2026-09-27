/**
 * Browser interaction helpers for the live Playwright suite.
 *
 * These functions intentionally encode product semantics, not generic page-object
 * abstractions. They know how Cortex exposes streaming, mode switches, history,
 * and provider summaries so specs can stay focused on assertions.
 */
import { expect } from "@playwright/test";

/** Count rendered assistant cards so the suite can predict the next response index. */
async function countResponseCards(page) {
    return page.locator("[id^='response-text-']").count();
}

/** Locate the React response-card article for one transcript-wide response index. */
function responseCard(page, index) {
    return page.locator(`#response-text-${index}`).locator("xpath=..");
}

/** Playwright can report pointer interception when the history rail overlaps controls. */
function isPointerInterceptionError(error) {
    return /intercepts pointer events/i.test(String(error?.message || ""));
}

/** Use a normal click first, then force-click only for known overlay interception cases. */
async function clickWithInterceptionFallback(locator) {
    try {
        await locator.click({ timeout: 5_000 });
    } catch (error) {
        if (!isPointerInterceptionError(error)) {
            throw error;
        }
        await locator.evaluate(node => {
            if (node && typeof node.click === "function") {
                node.click();
            }
        });
    }
}

/**
 * Install a lightweight in-page stream probe.
 *
 * Mutation observers let the suite verify that visible response text changes more
 * than once during streaming, which is stronger than only checking spinner state.
 */
async function ensureStreamProbe(page, indices) {
    await page.evaluate(({ indices }) => {
        const probeKey = "__cortexE2EStreamProbe";
        const probe = window[probeKey] ||= {
            sequences: {},
            observer: null,
        };
        const sequenceKey = indices.map(index => Number(index)).join(",");

        const readSnapshot = sequence => {
            return sequence.indices
                .map(index => {
                    const textEl = document.getElementById(`response-text-${index}`);
                    return String(textEl?.textContent || "");
                })
                .join("\n")
                .trim();
        };

        const recordSnapshot = sequence => {
            const snapshot = readSnapshot(sequence);
            if (!snapshot || snapshot === sequence.lastSnapshot) {
                return;
            }
            sequence.lastSnapshot = snapshot;
            sequence.snapshots.push({
                text: snapshot,
                length: snapshot.length,
                recordedAt: Date.now(),
            });
        };

        if (!probe.sequences[sequenceKey]) {
            probe.sequences[sequenceKey] = {
                indices: indices.map(index => Number(index)),
                lastSnapshot: "",
                snapshots: [],
            };
        }

        if (!probe.observer) {
            probe.observer = new MutationObserver(() => {
                for (const sequence of Object.values(probe.sequences)) {
                    recordSnapshot(sequence);
                }
            });
            probe.observer.observe(document.body, {
                childList: true,
                characterData: true,
                subtree: true,
            });
        }

        recordSnapshot(probe.sequences[sequenceKey]);
    }, { indices });
}

/** Read the recorded visible-text snapshots for one response sequence. */
async function readStreamProbe(page, indices) {
    return page.evaluate(({ indices }) => {
        const probe = window.__cortexE2EStreamProbe;
        if (!probe) {
            return { distinctSnapshots: 0, snapshots: [] };
        }
        const sequenceKey = indices.map(index => Number(index)).join(",");
        const sequence = probe.sequences?.[sequenceKey];
        if (!sequence) {
            return { distinctSnapshots: 0, snapshots: [] };
        }
        return {
            distinctSnapshots: Array.isArray(sequence.snapshots) ? sequence.snapshots.length : 0,
            snapshots: Array.isArray(sequence.snapshots) ? sequence.snapshots : [],
        };
    }, { indices });
}

/**
 * Wait for a streamed response to both show up and finish cleanly.
 *
 * The helper verifies three things together:
 * 1. text becomes visible within the first-content budget
 * 2. the visible text changes at least twice while streaming
 * 3. the React response streaming state eventually clears for all tracked cards
 */
async function waitForTextGrowth(page, indices, { firstContentTimeoutMs, completeTimeoutMs }) {
    const start = Date.now();
    const deadline = start + completeTimeoutMs;
    let distinctSnapshots = 0;
    let lastSnapshot = "";
    let sawFirstContent = false;

    await ensureStreamProbe(page, indices);

    while (Date.now() < deadline) {
        const texts = await Promise.all(indices.map(async index => {
            const locator = page.locator(`#response-text-${index}`);
            if (await locator.count() === 0) return "";
            return String(await locator.textContent() || "");
        }));
        const snapshot = texts.join("\n").trim();
        if (snapshot) {
            sawFirstContent = true;
            if (snapshot !== lastSnapshot) {
                distinctSnapshots += 1;
                lastSnapshot = snapshot;
            }
        }
        if (!sawFirstContent && (Date.now() - start) > firstContentTimeoutMs) {
            throw new Error(`No streamed content became visible within ${firstContentTimeoutMs}ms.`);
        }

        const streamingFlags = await Promise.all(indices.map(async index => {
            const card = responseCard(page, index);
            if (await card.count() === 0) return true;
            return (await card.getAttribute("data-response-streaming")) === "true";
        }));
        const anyStillStreaming = streamingFlags.some(Boolean);

        if (!anyStillStreaming && snapshot) {
            const probe = await readStreamProbe(page, indices);
            const observedSnapshots = Math.max(distinctSnapshots, probe.distinctSnapshots);
            return {
                distinctSnapshots: observedSnapshots,
                finalText: snapshot,
                probeSnapshots: probe.snapshots,
            };
        }

        await page.waitForTimeout(100);
    }

    throw new Error(`Streaming did not complete within ${completeTimeoutMs}ms.`);
}

/** Wait until the main composer and model selectors are ready for interaction. */
export async function waitForComposerReady(page, config) {
    await expect(page.locator("#promptInput")).toBeVisible({ timeout: config.timeouts.frontendReadyMs });
    await expect(page.getByRole("navigation", { name: "Chat mode" })).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
}

/** Open the app and block until the initial discovery-driven UI is usable. */
export async function openApp(page, config) {
    await page.goto(config.baseUrl, { waitUntil: "domcontentloaded" });
    await waitForComposerReady(page, config);
}

/** Toggle between Ask and Compare modes and assert the intended tab is active. */
export async function ensureMode(page, mode) {
    const button = mode === "compare" ? page.locator("#btnCompareMode") : page.locator("#btnSingleMode");
    const alreadySelected = (await button.getAttribute("aria-pressed")) === "true";
    if (!alreadySelected) {
        await clickWithInterceptionFallback(button);
    }
    await expect(button).toHaveAttribute("aria-pressed", "true");
}

/**
 * Set a boolean UI toggle to an exact state.
 *
 * This avoids tests accidentally double-toggling flags like Smart Routing or
 * Rewrite when a previous test or reload left the UI in a different state.
 * The removed Ask/Compare Web toggle remains a tolerated legacy selector while
 * the live suite migrates to automatic provider-native search.
 */
export async function setToggle(page, selector, desired) {
    const locator = page.locator(selector);
    if (selector === "#routeResearchBtn" && await locator.count() === 0) {
        return;
    }
    const expectedValue = desired ? "true" : "false";
    const deadline = Date.now() + 12_000;

    while (Date.now() < deadline) {
        const currentValue = await locator.getAttribute("aria-checked");
        if (currentValue === expectedValue) {
            break;
        }
        await clickWithInterceptionFallback(locator);
        await page.waitForTimeout(120);
    }

    await expect(locator).toHaveAttribute("aria-checked", expectedValue);
}

/**
 * Submit one Ask-mode prompt and wait for the full stream lifecycle.
 *
 * The returned index lets later assertions line up UI state with persisted DB rows.
 */
export async function submitAskPrompt(page, prompt, config) {
    const nextIndex = await countResponseCards(page);
    await ensureStreamProbe(page, [nextIndex]);
    await page.locator("#promptInput").fill(prompt);
    await page.locator("#submitBtn").click();
    await expect(page.locator(`#response-text-${nextIndex}`)).toBeAttached({ timeout: 10_000 });
    const stream = await waitForTextGrowth(page, [nextIndex], {
        firstContentTimeoutMs: config.timeouts.firstContentMs,
        completeTimeoutMs: config.timeouts.askMs,
    });
    await expect(page.locator("#submitBtn")).toHaveAttribute(
        "aria-label",
        "Send message",
        { timeout: 15_000 },
    );
    await expect(page.locator(`#response-text-${nextIndex}`).getByRole("status")).toBeHidden();
    const summaryText = String(
        await responseCard(page, nextIndex)
            .locator("[data-response-provider-summary]")
            .textContent() || "",
    ).trim();
    return {
        index: nextIndex,
        summaryText,
        stream,
        text: String(await page.locator(`#response-text-${nextIndex}`).textContent() || "").trim(),
    };
}

/**
 * Choose distinct models for Compare mode.
 *
 * The suite only cares that selections are unique and valid; it leaves exact model
 * ordering to the live provider catalog returned by the backend.
 */
export async function chooseDistinctCompareModels(page, count = 2) {
    const optionValues = await page.locator("#compareModel1 option").evaluateAll(options => {
        return options
            .map(option => option.value)
            .filter(value => typeof value === "string" && value.includes(":"));
    });
    const uniqueValues = [...new Set(optionValues)].slice(0, count);
    if (uniqueValues.length < count) {
        throw new Error(`Expected at least ${count} compare model options, found ${uniqueValues.length}.`);
    }

    await page.locator("#compareModel1").selectOption(uniqueValues[0]);
    await page.locator("#compareModel2").selectOption(uniqueValues[1]);

    if (count >= 3) {
        await page.locator("#compareAddModelBtn").click();
        await expect(page.locator("#compareModel3Wrap")).toBeVisible();
        await page.locator("#compareModel3").selectOption(uniqueValues[2]);
    }

    return uniqueValues;
}

/** Submit a Compare-mode prompt and wait for all requested result cards to finish streaming. */
export async function submitComparePrompt(page, prompt, config, targetCount = 2) {
    const startIndex = await countResponseCards(page);
    const indices = Array.from({ length: targetCount }, (_, offset) => startIndex + offset);
    await ensureStreamProbe(page, indices);
    await page.locator("#promptInput").fill(prompt);
    await page.locator("#submitBtn").click();
    await expect(page.locator(`#response-text-${indices[0]}`)).toBeAttached({ timeout: 10_000 });
    const stream = await waitForTextGrowth(page, indices, {
        firstContentTimeoutMs: config.timeouts.firstContentMs,
        completeTimeoutMs: config.timeouts.compareMs,
    });

    await expect(page.locator("#submitBtn")).toHaveAttribute(
        "aria-label",
        "Send message",
        { timeout: 15_000 },
    );
    await expect(page.locator(".compare-summary-card")).toBeVisible({ timeout: 15_000 });

    const summaries = [];
    for (const index of indices) {
        await expect(page.locator(`#response-text-${index}`).getByRole("status")).toBeHidden();
        summaries.push(
            String(
                await responseCard(page, index)
                    .locator("[data-response-provider-summary]")
                    .textContent() || "",
            ).trim(),
        );
    }

    return { indices, summaries, stream };
}

/** Expand the web-sources tray for a response card and return the visible source chips. */
export async function expandSourcesForCard(page, index) {
    const card = responseCard(page, index);
    const toggle = card.getByRole("button", { name: /^Sources:/ }).first();
    await expect(toggle).toBeVisible();
    await toggle.click();
    const list = page.getByLabel("Citation sources");
    await expect(list).toBeVisible();
    return list.locator("a");
}

/** Start a brand new chat thread from the sidebar and confirm the composer resets. */
export async function startNewChat(page) {
    await page.locator("#historyNewChatBtn").click();
    await expect(page.locator("#promptInput")).toHaveValue("");
}

/** Reopen a thread from history by matching a stable prompt fragment captured in the sidebar. */
export async function openHistoryThread(page, promptFragment) {
    const entry = page.locator("button[data-history-thread]", {
        has: page.locator("[data-history-title]", { hasText: promptFragment }),
    }).first();
    await expect(entry).toBeVisible();
    await entry.click();
}
