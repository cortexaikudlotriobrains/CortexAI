import { expect, test as base } from "@playwright/test";

const LONG_RESPONSE = Array.from(
    { length: 35 },
    (_, index) =>
        `Paragraph ${index + 1}: detailed comparison content that must remain readable inside the responsive response layout.`,
).join("\n\n");

const TABLE_RESPONSE = [
    "## Deployment comparison",
    "",
    "| Option | Delivery speed | Operational risk | Recommendation |",
    "| :--- | :---: | :---: | ---: |",
    "| Managed service | Fast | Low | Preferred |",
    "| Self-hosted | Moderate | Medium | Use for strict control |",
    "| Custom platform | Slow | High | Defer |",
].join("\n");

const MODELS = [
    model("openai", "gpt-5.1", true, "standard"),
    model("claude", "claude-sonnet-4-5", true, "advanced"),
    model("deepseek", "deepseek-chat", false, "economical"),
    model("gemini", "gemini-2.5-flash", true, "standard"),
    model("grok", "grok-4", true, "premium"),
];

export const test = base.extend({
    responsiveApp: async ({ page }, use) => {
        const state = {
            history: responsiveHistoryEntries(),
            analysisRuns: responsiveAnalysisRuns(),
            uploadedFiles: new Map(),
            models: [...MODELS],
            subscriptionPlan: "free",
            subscriptionSource: null,
            billingEnabled: true,
            billingPlans: [],
            maxFilesPerRequest: 1,
            directUploadIntentRequests: [],
            s3UploadRequests: [],
            completeUploadRequests: [],
            s3FailuresByFilename: new Map(),
            s3DelayMs: 0,
            directUploadSequence: 0,
            workSessions: [],
            workRun: null,
            workRuns: [],
            workRunSequence: 0,
            workStartDelayMs: 0,
            workStartPayload: null,
            workEvents: [],
            workEventsByRun: new Map(),
            workArtifacts: [],
            workArtifactsByRun: new Map(),
            workApproval: null,
            workConnections: [],
        };
        const pageErrors = [];
        page.on("pageerror", error => pageErrors.push(error));

        await installResponsiveRoutes(page, state);
        await page.goto("/");
        await expect(page.locator("#promptInput")).toBeVisible();

        await use({
            page,
            state,
            async reload() {
                await page.reload();
                await expect(page.locator("#promptInput")).toBeVisible();
            },
        });

        expect(
            pageErrors.map(error => error.stack || error.message),
            "uncaught browser errors",
        ).toEqual([]);
    },
});

export { expect };

export async function openMobilePanel(page, name) {
    await page
        .getByRole("navigation", { name: "Mobile navigation" })
        .getByRole("button", { name })
        .click();
}

export async function restoreHistoryThread(page, title) {
    const desktopHistory = page.locator("aside[aria-label='Primary navigation']");
    if (await desktopHistory.isVisible()) {
        await page.getByRole("button", { name: new RegExp(title, "i") }).first().click();
    } else {
        await openMobilePanel(page, "History");
        await page.getByRole("button", { name: new RegExp(title, "i") }).first().click();
    }
    await expect(page.locator('section[aria-label="Chat transcript"]')).toBeVisible();
}

export async function expectNoHorizontalOverflow(page) {
    const metrics = await page.evaluate(() => ({
        viewportWidth: window.innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
    }));
    expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewportWidth);
    expect(metrics.bodyWidth).toBeLessThanOrEqual(metrics.viewportWidth);
}

async function installResponsiveRoutes(page, state) {
    await page.route("**/runtime-config.js", route =>
        route.fulfill({
            status: 200,
            contentType: "application/javascript",
            body: "window.CORTEX_RUNTIME_CONFIG = { enableDevSessionLogin: false, directAttachmentUploads: true, legacyAttachmentUploads: true, workEnabled: true };",
        }),
    );

    await page.route("https://*.amazonaws.com/**", async route => {
        const request = route.request();
        const url = new URL(request.url());
        const fileId = decodeURIComponent(url.pathname.split("/").filter(Boolean).at(-1) || "");
        const uploaded = state.uploadedFiles.get(fileId);
        const body = request.postDataBuffer();
        state.s3UploadRequests.push({
            fileId,
            headers: request.headers(),
            body: body?.toString("latin1") ?? "",
        });
        if (!uploaded) {
            return route.fulfill({
                status: 404,
                headers: { "Access-Control-Allow-Origin": "*" },
                body: "missing upload intent",
            });
        }
        const failuresRemaining = state.s3FailuresByFilename.get(uploaded.original_filename) ?? 0;
        if (failuresRemaining > 0) {
            state.s3FailuresByFilename.set(uploaded.original_filename, failuresRemaining - 1);
            return route.fulfill({
                status: 503,
                headers: { "Access-Control-Allow-Origin": "*" },
                body: "temporary storage failure",
            });
        }
        if (state.s3DelayMs > 0) {
            await new Promise(resolve => setTimeout(resolve, state.s3DelayMs));
        }
        uploaded.s3Uploaded = true;
        return route.fulfill({
            status: 204,
            headers: { "Access-Control-Allow-Origin": "*" },
            body: "",
        });
    });

    await page.route("**/v1/**", async route => {
        const request = route.request();
        const url = new URL(request.url());
        const method = request.method();

        if (url.pathname === "/v1/auth/cognito-config") {
            return json(route, { enabled: false });
        }
        if (url.pathname === "/v1/whoami") {
            return json(route, whoAmI());
        }
        if (url.pathname === "/v1/models") {
            return json(route, {
                enabled_only: true,
                models: state.models,
                total: state.models.length,
                timestamp: "2026-06-12T12:00:00Z",
            });
        }
        if (url.pathname === "/v1/billing/plans" && method === "GET") {
            return json(route, {
                currency: "USD",
                billing_period: "monthly",
                billing_enabled: state.billingEnabled,
                plans: state.billingPlans,
            });
        }
        if (url.pathname === "/v1/billing/subscription" && method === "GET") {
            const paid = state.subscriptionPlan !== "free";
            return json(route, {
                plan_code: state.subscriptionPlan,
                status: paid ? "active" : "free",
                provider: paid && state.subscriptionSource !== "cortex_grant" ? "stripe" : null,
                current_period_start: "2026-07-01T00:00:00Z",
                current_period_end: "2026-08-01T00:00:00Z",
                cancel_at_period_end: false,
                can_manage: paid && state.subscriptionSource !== "cortex_grant",
            });
        }
        if (url.pathname === "/v1/entitlements" && method === "GET") {
            const payload = entitlements(state.subscriptionPlan, state.maxFilesPerRequest);
            if (state.subscriptionSource) payload.plan.source = state.subscriptionSource;
            return json(route, payload);
        }
        if (url.pathname === "/v1/tools/catalog" && method === "GET") {
            return json(route, [
                { connector_key: "cortex_files", display_name: "Cortex Files", description: "Use uploaded files.", icon: "files", connection_state: "available", plan_requirement: "plus", capabilities: ["read_files"], risk_classes: ["READ"], configuration_required: false },
                { connector_key: "github", display_name: "GitHub", description: "Repository tools.", icon: "github", connection_state: "configuration_required", plan_requirement: "plus", capabilities: ["repositories"], risk_classes: ["READ", "WRITE"], configuration_required: true },
                { connector_key: "custom_mcp", display_name: "Custom Remote MCP", description: "Reviewed remote tools.", icon: "plug", connection_state: "available", plan_requirement: "pro", capabilities: ["remote_mcp"], risk_classes: ["READ", "WRITE"], configuration_required: false },
            ]);
        }
        if (url.pathname === "/v1/tools/connections" && method === "GET") {
            return json(route, state.workConnections);
        }
        if (url.pathname === "/v1/tools/connections" && method === "POST") {
            const payload = request.postDataJSON();
            const connection = {
                id: `connection-${state.workConnections.length + 1}`,
                connector_key: "custom_mcp",
                connection_type: "mcp_remote",
                display_name: payload.display_name,
                server_url: payload.server_url,
                auth_type: payload.auth_type,
                status: "pending",
                granted_scopes: [],
                metadata: {},
                created_at: "2026-08-20T12:00:00Z",
                updated_at: "2026-08-20T12:00:00Z",
                last_verified_at: null,
            };
            state.workConnections.push(connection);
            return json(route, connection, 201);
        }
        const connectionTest = url.pathname.match(/^\/v1\/tools\/connections\/([^/]+)\/test$/);
        if (connectionTest && method === "POST") {
            const connection = state.workConnections.find(item => item.id === connectionTest[1]);
            if (connection) connection.status = "connected";
            return json(route, { ok: true, status: "connected", message: "Connected" });
        }
        if (url.pathname === "/v1/work/sessions" && method === "GET") {
            return json(route, state.workSessions);
        }
        if (url.pathname === "/v1/work/sessions" && method === "POST") {
            const payload = request.postDataJSON();
            const session = makeWorkSession(payload.title || "New work");
            state.workSessions = [session, ...state.workSessions];
            return json(route, session, 201);
        }
        const workSessionLatest = url.pathname.match(/^\/v1\/work\/sessions\/([^/]+)\/runs\/latest$/);
        if (workSessionLatest && method === "GET") {
            const latest = state.workRuns.at(-1) || state.workRun;
            return latest
                ? json(route, latest)
                : json(route, { detail: { code: "work_run_not_found", message: "No run" } }, 404);
        }
        const workSessionRuns = url.pathname.match(/^\/v1\/work\/sessions\/([^/]+)\/runs$/);
        if (workSessionRuns && method === "GET") {
            return json(route, state.workRuns.length > 0 ? state.workRuns : state.workRun ? [state.workRun] : []);
        }
        const workSession = url.pathname.match(/^\/v1\/work\/sessions\/([^/]+)$/);
        if (workSession && method === "GET") {
            const session = state.workSessions.find(item => item.id === workSession[1]);
            return session ? json(route, session) : json(route, { detail: "Not found" }, 404);
        }
        const workStart = url.pathname.match(/^\/v1\/work\/sessions\/([^/]+)\/(runs|instructions)$/);
        if (workStart && method === "POST") {
            const payload = request.postDataJSON();
            state.workStartPayload = payload;
            const session = state.workSessions.find(item => item.id === workStart[1]) || makeWorkSession(payload.instruction.slice(0, 120));
            if (!state.workSessions.some(item => item.id === session.id)) state.workSessions.unshift(session);
            if (state.workStartDelayMs > 0) {
                await new Promise(resolve => setTimeout(resolve, state.workStartDelayMs));
            }
            state.workRunSequence += 1;
            state.workRun = {
                ...makeWorkRun(session.id, payload.instruction, "running", payload.web_mode),
                id: `work-run-${state.workRunSequence}`,
                request_id: `responsive-work-request-${state.workRunSequence}`,
            };
            state.workEvents = [
                makeWorkEvent(1, "run_created", "Work run created"),
                makeWorkEvent(2, "planning", "Creating a plan"),
                makeWorkEvent(3, "run_completed", "Work completed"),
            ];
            state.workArtifacts = [{
                id: "artifact-1", file_id: "artifact-file-1", role: "artifact", source: "agent",
                filename: "work-report.pdf", mime_type: "application/pdf", size_bytes: 4096,
                artifact_type: "report", metadata: {}, created_at: "2026-08-20T12:04:00Z",
            }];
            state.workRuns = [
                ...state.workRuns.filter(run => run.id !== state.workRun.id),
                state.workRun,
            ];
            state.workEventsByRun.set(state.workRun.id, state.workEvents);
            state.workArtifactsByRun.set(state.workRun.id, state.workArtifacts);
            return json(route, state.workRun, 202);
        }
        const workRunEvents = url.pathname.match(/^\/v1\/work\/runs\/([^/]+)\/events$/);
        if (workRunEvents && method === "GET") {
            const after = Number(url.searchParams.get("after_sequence") || 0);
            const events = state.workEventsByRun.get(workRunEvents[1])
                || (state.workRun?.id === workRunEvents[1] ? state.workEvents : []);
            const items = events.filter(event => event.sequence > after);
            return json(route, { items, latest_sequence: items.at(-1)?.sequence ?? after });
        }
        const workRunStream = url.pathname.match(/^\/v1\/work\/runs\/([^/]+)\/stream$/);
        if (workRunStream && method === "GET") {
            const runId = workRunStream[1];
            const streamedRun = state.workRuns.find(run => run.id === runId)
                || (state.workRun?.id === runId ? state.workRun : null);
            if (streamedRun?.status === "running") {
                const completedRun = { ...streamedRun, status: "completed", actual_credits: 18400, completed_at: "2026-08-20T12:04:00Z", updated_at: "2026-08-20T12:04:00Z" };
                state.workRuns = state.workRuns.map(run => run.id === runId ? completedRun : run);
                if (state.workRun?.id === runId) state.workRun = completedRun;
                const events = state.workEventsByRun.get(runId) || state.workEvents;
                const terminal = events.at(-1);
                return route.fulfill({ status: 200, contentType: "text/event-stream", body: `id: ${terminal.sequence}\nevent: ${terminal.type}\ndata: ${JSON.stringify(terminal)}\n\n` });
            }
            return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": heartbeat\n\n" });
        }
        const workArtifacts = url.pathname.match(/^\/v1\/work\/runs\/([^/]+)\/artifacts$/);
        if (workArtifacts && method === "GET") {
            return json(
                route,
                state.workArtifactsByRun.get(workArtifacts[1])
                    || (state.workRun?.id === workArtifacts[1] ? state.workArtifacts : []),
            );
        }
        const workRunCancel = url.pathname.match(/^\/v1\/work\/runs\/([^/]+)\/cancel$/);
        if (workRunCancel && method === "POST") {
            const existing = state.workRuns.find(run => run.id === workRunCancel[1]) || state.workRun;
            const cancelledRun = { ...existing, status: "cancelled", completed_at: "2026-08-20T12:03:00Z" };
            state.workRuns = state.workRuns.map(run => run.id === workRunCancel[1] ? cancelledRun : run);
            if (state.workRun?.id === workRunCancel[1]) state.workRun = cancelledRun;
            return json(route, cancelledRun);
        }
        const workRun = url.pathname.match(/^\/v1\/work\/runs\/([^/]+)$/);
        if (workRun && method === "GET") {
            const found = state.workRuns.find(run => run.id === workRun[1])
                || (state.workRun?.id === workRun[1] ? state.workRun : null);
            return found ? json(route, found) : json(route, { detail: "Not found" }, 404);
        }
        const workApproval = url.pathname.match(/^\/v1\/work\/approvals\/([^/]+)$/);
        if (workApproval && method === "GET") {
            return state.workApproval ? json(route, state.workApproval) : json(route, { detail: "Not found" }, 404);
        }
        const workDecision = url.pathname.match(/^\/v1\/work\/approvals\/([^/]+)\/(approve|deny)$/);
        if (workDecision && method === "POST") {
            state.workApproval = { ...state.workApproval, status: workDecision[2] === "approve" ? "approved" : "denied", decided_at: "2026-08-20T12:03:00Z" };
            state.workRun = { ...state.workRun, status: "running" };
            return json(route, state.workApproval);
        }
        if (url.pathname === "/v1/credits/transactions" && method === "GET") {
            return json(route, creditTransactions());
        }
        if (url.pathname === "/v1/usage/summary" && method === "GET") {
            return json(route, usageSummary());
        }
        if (url.pathname === "/v1/usage/export" && method === "GET") {
            return route.fulfill({
                status: 200,
                contentType: "text/csv",
                headers: {
                    "Content-Disposition": "attachment; filename=usage_report.csv",
                },
                body: "date,requests,tokens,cost\n2026-07-01,42,189540,1.72\n",
            });
        }
        if (url.pathname === "/v1/history" && method === "GET") {
            const sessionId = url.searchParams.get("session_id");
            return json(
                route,
                sessionId
                    ? state.history.filter(entry => entry.session_id === sessionId)
                    : state.history,
            );
        }
        if (url.pathname === "/v1/history" && method === "DELETE") {
            state.history = [];
            state.analysisRuns = [];
            return route.fulfill({ status: 204, body: "" });
        }
        if (url.pathname === "/v1/compare/analysis-runs" && method === "GET") {
            const sessionId = url.searchParams.get("session_id");
            const requestGroupId = url.searchParams.get("request_group_id");
            return json(
                route,
                state.analysisRuns.filter(run =>
                    (!sessionId || run.sessionId === sessionId) &&
                    (!requestGroupId || run.requestGroupId === requestGroupId),
                ),
            );
        }
        const analysisMatch = url.pathname.match(/^\/v1\/compare\/([^/]+)\/analysis$/);
        if (analysisMatch && method === "POST") {
            const requestGroupId = decodeURIComponent(analysisMatch[1]);
            const sourceRows = state.history.filter(
                entry => entry.request_group_id === requestGroupId && !entry.error_message,
            );
            if (sourceRows.length < 2) {
                return json(route, { detail: "Not enough successful responses" }, 409);
            }
            const run = makeResponsiveAnalysisRun({
                analysisId: `analysis-${state.analysisRuns.length + 1}`,
                requestGroupId,
                sessionId: sourceRows[0].session_id,
                createdAt: new Date().toISOString(),
                sourceRows,
            });
            state.analysisRuns = [run, ...state.analysisRuns];
            return json(route, run, 201);
        }
        if (url.pathname === "/v1/files/upload-batch" && method === "POST") {
            const requestBody = request.postDataBuffer();
            const multipartText = requestBody?.toString("latin1") ?? "";
            const fileNames = [...multipartText.matchAll(/filename="([^"]+)"/g)].map(
                match => match[1],
            );
            const uploadedFiles = fileNames.map((fileName, index) => {
                const uploaded = {
                    file_id: `file-${state.uploadedFiles.size + index + 1}`,
                    original_filename: fileName,
                    mime_type: "text/plain",
                    size_bytes: requestBody?.byteLength ?? 0,
                    status: "ready",
                    ingestion_meta: {},
                    created_at: "2026-06-12T12:00:00Z",
                    deduplicated: false,
                };
                state.uploadedFiles.set(uploaded.file_id, uploaded);
                return uploaded;
            });
            return json(route, { files: uploadedFiles });
        }
        if (url.pathname === "/v1/files/upload-intents" && method === "POST") {
            const payload = request.postDataJSON();
            state.directUploadIntentRequests.push(payload);
            const files = (payload.files ?? []).map(metadata => {
                state.directUploadSequence += 1;
                const fileId = `direct-file-${state.directUploadSequence}`;
                const uploaded = {
                    file_id: fileId,
                    original_filename: metadata.filename,
                    mime_type: metadata.mime_type,
                    size_bytes: metadata.size_bytes,
                    status: "uploading",
                    ingestion_meta: {},
                    created_at: "2026-08-11T12:00:00Z",
                    deduplicated: false,
                    s3Uploaded: false,
                };
                state.uploadedFiles.set(fileId, uploaded);
                return {
                    ...uploaded,
                    upload: {
                        url: `https://cortex-e2e-bucket.s3.us-east-1.amazonaws.com/${fileId}`,
                        fields: {
                            key: `attachments/users/e2e/${fileId}`,
                            "Content-Type": metadata.mime_type,
                            "x-amz-meta-cortex-file-id": fileId,
                            policy: `policy-${fileId}`,
                            "x-amz-signature": `signature-${fileId}`,
                        },
                        expires_at: "2026-08-11T12:05:00Z",
                    },
                };
            });
            return json(route, { files });
        }
        const completeMatch = url.pathname.match(/^\/v1\/files\/([^/]+)\/complete$/);
        if (completeMatch && method === "POST") {
            const fileId = decodeURIComponent(completeMatch[1]);
            const uploaded = state.uploadedFiles.get(fileId);
            state.completeUploadRequests.push(fileId);
            if (!uploaded?.s3Uploaded) {
                return json(route, {
                    detail: {
                        code: "attachment_upload_not_complete",
                        message: "The object has not reached storage.",
                    },
                }, 409);
            }
            uploaded.status = "ready";
            return json(route, uploaded);
        }
        if (url.pathname.startsWith("/v1/files/") && method === "DELETE") {
            const fileId = decodeURIComponent(url.pathname.split("/").at(-1));
            const uploaded = state.uploadedFiles.get(fileId);
            if (!uploaded) return json(route, { detail: "File not found" }, 404);
            uploaded.status = "deleting";
            return json(route, uploaded);
        }
        if (url.pathname === "/v1/files/upload" && method === "POST") {
            const fileName = request.headers()["x-file-name"] || "attachment.txt";
            const uploaded = {
                file_id: `file-${state.uploadedFiles.size + 1}`,
                original_filename: fileName,
                mime_type: request.headers()["x-file-content-type"] || "text/plain",
                size_bytes: request.postDataBuffer()?.byteLength ?? 0,
                status: "ready",
                ingestion_meta: {},
                created_at: "2026-06-12T12:00:00Z",
                deduplicated: false,
            };
            state.uploadedFiles.set(uploaded.file_id, uploaded);
            return json(route, uploaded);
        }
        if (url.pathname.startsWith("/v1/files/") && method === "GET") {
            const fileId = url.pathname.split("/").at(-1);
            const uploaded = state.uploadedFiles.get(fileId);
            return uploaded
                ? json(route, uploaded)
                : json(route, { detail: "File not found" }, 404);
        }

        return json(route, { detail: `Unhandled responsive test route: ${method} ${url.pathname}` }, 404);
    });
}

function responsiveHistoryEntries() {
    return [
        historyEntry({
            id: 1,
            sessionId: "ask-session",
            timestamp: "2026-06-12T09:00:00Z",
            prompt: "Help debug a FastAPI stream",
            response: "Check disconnect handling, response iteration, and request correlation.",
            provider: "openai",
            modelName: "gpt-5.1",
        }),
        historyEntry({
            id: 2,
            sessionId: "ask-session",
            timestamp: "2026-06-12T09:05:00Z",
            prompt: "Add a retry strategy",
            response: "Retry only transient failures and preserve the request identifier.",
            provider: "openai",
            modelName: "gpt-5.1",
        }),
        historyEntry({
            id: 3,
            sessionId: "dense-history-session",
            timestamp: "2026-06-12T09:30:00Z",
            prompt: "Plan a multi-region platform migration with strict recovery objectives",
            response: "Use phased regional cutovers with tested rollback and recovery procedures.",
            provider: "openai",
            modelName: "gpt-5.4-mini-enterprise-preview-with-extended-context",
        }),
        ...compareHistoryEntries(),
        ...compareTableHistoryEntries(),
        ...threeModelCompareHistoryEntries(),
    ];
}

function responsiveAnalysisRuns() {
    const history = responsiveHistoryEntries();
    const sourceRows = history.filter(
        entry => entry.request_group_id === "compare-group-1",
    );
    return [
        makeResponsiveAnalysisRun({
            analysisId: "analysis-saved-1",
            requestGroupId: "compare-group-1",
            sessionId: "compare-session",
            createdAt: "2026-06-12T10:02:00Z",
            sourceRows,
        }),
    ];
}

function makeResponsiveAnalysisRun({
    analysisId,
    requestGroupId,
    sessionId,
    createdAt,
    sourceRows,
}) {
    return {
        analysisId,
        requestGroupId,
        sessionId,
        model: "gpt-5.4-mini",
        recommendedAnswer:
            "Use a phased gateway rollout with explicit provider fallbacks and observable recovery checks.",
        agreements: [
            "Both responses favor incremental rollout over a one-time cutover.",
        ],
        disagreements: [
            {
                who: "Claude (Sonnet 4.5)",
                text: "Assigns a different priority to cost and operational control.",
            },
        ],
        disagreementNote: "These are different implementation priorities, not conflicting facts.",
        uniqueInsights: [
            {
                responseName: "ChatGPT",
                text: "One response highlights request correlation as a rollout prerequisite.",
            },
        ],
        confidence: {
            level: "moderate",
            reason: "The responses align on the main rollout shape but differ on priorities.",
        },
        verify: ["Confirm the recovery objectives for each rollout phase."],
        highStakesDomain: null,
        sourceFingerprint: `responsive-${analysisId}`,
        sourceResponses: sourceRows.map(entry => ({
            requestId: entry.request_id,
            responseVersion: entry.response_version,
            responseName: entry.provider === "openai" ? "ChatGPT" : "Claude",
        })),
        combinedResponseCount: sourceRows.length,
        failedResponseCount: 0,
        createdAt,
        isStale: false,
    };
}

function compareHistoryEntries() {
    const entries = [];
    let id = 10;
    for (let turn = 1; turn <= 3; turn += 1) {
        const prompt =
            turn === 1
                ? "Architecture decision for an LLM gateway"
                : `Architecture follow-up ${turn}`;
        for (const [provider, modelName] of [
            ["openai", "gpt-5.1"],
            ["claude", "claude-sonnet-4-5"],
        ]) {
            entries.push(
                historyEntry({
                    id,
                    sessionId: "compare-session",
                    requestGroupId: `compare-group-${turn}`,
                    timestamp: `2026-06-12T10:0${turn}:${provider === "openai" ? "00" : "01"}Z`,
                    mode: "compare",
                    prompt,
                    response: LONG_RESPONSE,
                    provider,
                    modelName,
                    tokens: 1800 + id,
                }),
            );
            id += 1;
        }
    }
    return entries;
}

function compareTableHistoryEntries() {
    return [
        historyEntry({
            id: 30,
            sessionId: "compare-table-session",
            requestGroupId: "compare-table-group",
            timestamp: "2026-06-12T11:00:00Z",
            mode: "compare",
            prompt: "Compare deployment options table",
            response: TABLE_RESPONSE,
            provider: "openai",
            modelName: "gpt-5.1",
            tokens: 640,
        }),
        historyEntry({
            id: 31,
            sessionId: "compare-table-session",
            requestGroupId: "compare-table-group",
            timestamp: "2026-06-12T11:00:01Z",
            mode: "compare",
            prompt: "Compare deployment options table",
            response: TABLE_RESPONSE,
            provider: "claude",
            modelName: "claude-sonnet-4-5",
            tokens: 670,
        }),
    ];
}

function threeModelCompareHistoryEntries() {
    return [
        ["openai", "gpt-5.1"],
        ["claude", "claude-sonnet-4-5"],
        ["deepseek", "deepseek-chat"],
    ].map(([provider, modelName], index) =>
        historyEntry({
            id: 40 + index,
            sessionId: "compare-three-model-session",
            requestGroupId: "compare-three-model-group",
            timestamp: `2026-06-12T11:30:0${index}Z`,
            mode: "compare",
            prompt: "Three model platform comparison",
            response: LONG_RESPONSE,
            provider,
            modelName,
            tokens: 900 + index * 100,
        }),
    );
}

function historyEntry({
    id,
    sessionId,
    requestGroupId,
    timestamp,
    mode = "single",
    prompt,
    response,
    provider,
    modelName,
    tokens = 320,
}) {
    return {
        id,
        request_id: String(id),
        response_version: 1,
        session_id: sessionId,
        request_group_id: requestGroupId,
        timestamp,
        mode,
        prompt,
        provider,
        model: modelName,
        response,
        latency_ms: 900,
        tokens,
        cost: 0.001,
        web_source_items: [],
    };
}

function model(provider, modelName, supportsImageInput, billingClass) {
    return {
        provider,
        model: modelName,
        tier: "frontier",
        billing_class: billingClass,
        access_category: billingClass,
        input_credit_multiplier: 1,
        output_credit_multiplier: 4,
        credit_usage_label: "Standard",
        credit_pricing_version: "responsive-test",
        input_cost_per_1m: 0,
        output_cost_per_1m: 0,
        context_limit: 128000,
        tags: [],
        enabled: true,
        supports_image_input: supportsImageInput,
        supported_attachment_mime_types: [],
    };
}

function whoAmI() {
    return {
        api_key_id: "responsive-test-key",
        user_id: "responsive-test-user",
        plan_tier: "test",
        storage_policy: "full",
        redact_pii: false,
        baseline: {
            provider: "openai",
            model: "gpt-5.1",
            source: "test",
        },
        rate_limits: {
            requests_per_minute: 60,
            daily_cap_scope: "user",
        },
        breakers: {
            failure_threshold: 5,
            window_seconds: 60,
            cooldown_seconds: 120,
            scope: "provider_model",
        },
    };
}

function entitlements(planCode = "free", maxFilesPerRequest = 1) {
    const plan = {
        free: {
            displayName: "Free",
            allowedBillingClasses: ["economical", "standard"],
            maxCompareModels: 2,
            allowance: 100000,
        },
        plus: {
            displayName: "Plus",
            allowedBillingClasses: ["economical", "standard", "advanced"],
            maxCompareModels: 2,
            allowance: 1000000,
        },
        pro: {
            displayName: "Pro",
            allowedBillingClasses: ["economical", "standard", "advanced", "premium"],
            maxCompareModels: 3,
            allowance: 3000000,
        },
    }[planCode] ?? {
        displayName: "Free",
        allowedBillingClasses: ["economical", "standard"],
        maxCompareModels: 2,
        allowance: 100000,
    };
    return {
        plan: {
            code: planCode,
            display_name: plan.displayName,
            status: planCode === "free" ? "free" : "active",
            source: planCode === "free" ? "default" : "stripe",
            renews_at: "2026-08-01T00:00:00Z",
            cancel_at_period_end: false,
            grace_until: null,
        },
        features: {
            compare_enabled: true,
            max_compare_models: plan.maxCompareModels,
            research_enabled: true,
            prompt_improvement_enabled: true,
            file_analysis_enabled: true,
            usage_export_enabled: false,
            saved_history_enabled: true,
            models_catalog_enabled: true,
            work_enabled: planCode !== "free",
            verified_connectors_enabled: planCode !== "free",
            custom_mcp_enabled: planCode === "pro",
            action_tools_enabled: planCode !== "free",
        },
        model_access: {
            allowed_billing_classes: plan.allowedBillingClasses,
        },
        limits: {
            max_files_per_request: maxFilesPerRequest,
            max_file_bytes: 10000000,
            max_active_work_runs: planCode === "pro" ? 3 : planCode === "plus" ? 1 : 0,
            max_tool_connections: planCode === "pro" ? 10 : planCode === "plus" ? 3 : 0,
            max_mcp_servers_per_run: planCode === "pro" ? 10 : planCode === "plus" ? 3 : 0,
            max_work_credit_budget: planCode === "pro" ? 1000000 : planCode === "plus" ? 250000 : 0,
        },
        allowances: {
            ai_credits: {
                used: 10000,
                reserved: 0,
                limit: plan.allowance,
                remaining: plan.allowance - 10000,
            },
        },
        period: {
            starts_at: "2026-07-01T00:00:00Z",
            ends_at: "2026-08-01T00:00:00Z",
        },
    };
}

function makeWorkSession(title = "Postman Work task") {
    return {
        id: "work-session-1",
        session_id: "session-work-1",
        title,
        status: "idle",
        agent_provider: "fake",
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:00:00Z",
        latest_run_status: null,
    };
}

function makeWorkRun(workSessionId, instruction, status = "running", webMode = "auto") {
    return {
        id: "work-run-1",
        work_session_id: workSessionId,
        request_id: "responsive-work-request",
        instruction,
        status,
        provider: "fake",
        max_credit_budget: 100000,
        max_output_tokens: 40000,
        actual_output_tokens: status === "completed" ? 12000 : 6400,
        reserved_credits: 100000,
        actual_credits: status === "completed" ? 18400 : 6400,
        provider_model_id: "claude-haiku-4-5",
        billing_model_id: "claude-haiku-4-5",
        billing_model_source: "fake_session_agent_snapshot",
        provider_agent_id: "fake-agent",
        provider_agent_version: 1,
        output_finalize_requested_at: null,
        output_limit_interrupt_requested_at: null,
        configuration_snapshot: {
            requested_web_mode: webMode || "auto",
            effective_web_enabled: false,
            enabled_connection_ids: [],
        },
        usage_snapshot: {},
        stop_reason: null,
        error_code: null,
        error_message: null,
        started_at: "2026-08-20T12:00:00Z",
        completed_at: status === "completed" ? "2026-08-20T12:04:00Z" : null,
        created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:00:00Z",
    };
}

function makeWorkEvent(sequence, type, displayMessage, payload = {}) {
    return {
        id: `work-event-${sequence}`,
        sequence,
        type,
        display_message: displayMessage,
        payload,
        created_at: `2026-08-20T12:0${Math.min(sequence, 9)}:00Z`,
    };
}

function creditTransactions() {
    return {
        items: [
            {
                id: "credit-1",
                request_id: "request-1",
                activity_id: "activity-1",
                query: "How do atomic credit reservations work?",
                operation_type: "chat",
                item_type: "model",
                provider: "openai",
                model: "gpt-5.4-mini",
                input_tokens: 600,
                output_tokens: 200,
                input_credits: 1200,
                output_credits: 800,
                fixed_credits: 0,
                total_credits: 2000,
                provider_cost_usd: 0.002,
                usage_estimated: false,
                pricing_version: "2026-07-29",
                metadata: {},
                created_at: "2026-07-31T14:30:00Z",
            },
            {
                id: "credit-2",
                request_id: "request-1",
                activity_id: "activity-1",
                query: "How do atomic credit reservations work?",
                operation_type: "chat",
                item_type: "research",
                provider: "tavily",
                model: null,
                input_tokens: 0,
                output_tokens: 0,
                input_credits: 0,
                output_credits: 0,
                fixed_credits: 10000,
                total_credits: 10000,
                provider_cost_usd: 0.002,
                usage_estimated: false,
                pricing_version: "2026-07-29",
                metadata: {
                    provider_credits_used: 2,
                    cortex_credits_per_provider_credit: 5000,
                },
                created_at: "2026-07-31T14:30:00Z",
            },
            {
                id: "credit-3",
                request_id: "request-1",
                activity_id: "activity-1",
                query: "How do atomic credit reservations work?",
                operation_type: "chat",
                item_type: "adjustment",
                provider: null,
                model: null,
                input_tokens: 0,
                output_tokens: 0,
                input_credits: 0,
                output_credits: 0,
                fixed_credits: 0,
                total_credits: 0,
                provider_cost_usd: 0,
                usage_estimated: false,
                pricing_version: "2026-07-29",
                metadata: { unbilled_credits: 200 },
                created_at: "2026-07-31T14:30:00Z",
            },
            {
                id: "credit-4",
                request_id: "optimizer-request-1",
                activity_id: "activity-1",
                query: "How do atomic credit reservations work?",
                operation_type: "optimize",
                item_type: "model",
                provider: "openai",
                model: "gpt-5.4-mini",
                input_tokens: 150,
                output_tokens: 100,
                input_credits: 300,
                output_credits: 700,
                fixed_credits: 0,
                total_credits: 1000,
                provider_cost_usd: 0.001,
                usage_estimated: false,
                pricing_version: "2026-07-29",
                metadata: {},
                created_at: "2026-07-31T14:29:58Z",
            },
        ],
        limit: 20,
        offset: 0,
    };
}

function usageSummary() {
    return {
        period: {
            from: "2026-06-02",
            to: "2026-07-01",
            label: "Last 30 days",
        },
        totalTokens: 2840000,
        totalRequests: 1336,
        totalSessions: 312,
        avgLatencyMs: 4600,
        p95LatencyMs: 8100,
        minLatencyMs: 1400,
        avgCostPerRequest: 0.0091,
        totalSpend: 12.16,
        tokensDeltaPct: 18.4,
        smartRoutedTotal: 720,
        models: [
            usageModel("openai", "gpt-5.4-mini", "GPT-5.4 Mini", 512, 470),
            usageModel("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5", 318, 88),
            usageModel("deepseek", "deepseek-chat", "DeepSeek Chat", 246, 60),
            usageModel("openai", "gpt-5.1", "GPT-5.1", 142, 64),
            usageModel("google", "gemini-2.5-flash", "Gemini 2.5", 66, 38),
            usageModel("meta", "llama-3.3-70b", "Llama 3.3 70B", 34, 0),
            usageModel("mistral", "mistral-large", "Mistral Large", 18, 0),
        ],
        sessionModes: {
            askOnly: 168,
            compareOnly: 96,
            mixed: 48,
        },
        switchedMidSession: 48,
        activityDaily: [
            activityDay("2026-06-18", 148000),
            activityDay("2026-06-19", 176000),
            activityDay("2026-06-20", 121000),
            activityDay("2026-06-21", 96000),
            activityDay("2026-06-22", 189000),
            activityDay("2026-06-23", 213000),
            activityDay("2026-06-24", 198000),
            activityDay("2026-06-25", 234000),
            activityDay("2026-06-26", 268000),
            activityDay("2026-06-27", 241000),
            activityDay("2026-06-28", 172000),
            activityDay("2026-06-29", 286000),
            activityDay("2026-06-30", 304000),
            activityDay("2026-07-01", 321000),
        ],
    };
}

function usageModel(provider, modelId, displayName, replies, viaSmart) {
    return {
        provider,
        modelId,
        displayName,
        replies,
        viaSmart,
    };
}

function activityDay(date, tokens) {
    return { date, tokens };
}

function json(route, body, status = 200) {
    return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
    });
}
