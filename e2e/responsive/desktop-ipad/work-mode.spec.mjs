import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

test("desktop Work empty state starts a real mocked run and renders its deliverable", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    state.subscriptionPlan = "pro";
    state.workStartDelayMs = 900;
    await page.goto("/work");

    await expect(page.getByRole("heading", { name: "What should I work on?" })).toBeVisible();
    await expect(page.locator("aside[aria-label='Primary navigation']").getByRole("button", { name: "Work", exact: true })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Work goal" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Web access: Auto" })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.getByRole("textbox", { name: "Work goal" }).fill("Analyze these files and create a report");
    await page.getByRole("button", { name: /Start work/ }).click();

    await expect(page.getByRole("status", { name: "Starting work" })).toBeVisible();
    await expect(page.getByText("Starting", { exact: true })).toBeVisible();
    await expect(page.getByText("Work completed", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("work-report.pdf")).toBeVisible();
    await expect(page.getByRole("link", { name: "Download work-report.pdf" })).toBeVisible();
    expect(state.workStartPayload.web_mode).toBe("auto");
    await expectNoHorizontalOverflow(page);
});

test("desktop Work opens and interacts with the Tools menu", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    state.subscriptionPlan = "pro";
    await page.setViewportSize({ width: 1367, height: 675 });
    await page.goto("/work");

    const toolsButton = page.getByRole("button", { name: /Tools/ });
    await toolsButton.click();

    const toolsDialog = page.getByRole("dialog", { name: "Tools" });
    await expect(toolsButton).toHaveAttribute("aria-expanded", "true");
    await expect(toolsDialog).toBeVisible();
    await toolsDialog.getByRole("button", { name: "Add MCP server" }).click();
    await expect(toolsDialog.getByRole("textbox", { name: "HTTPS endpoint" })).toBeVisible();
    await toolsDialog.getByRole("button", { name: "Close tools" }).click();
    await expect(toolsDialog).toHaveCount(0);
    await expectNoHorizontalOverflow(page);
});

test("desktop Work binds the inline approval card and clears it after approve", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    state.subscriptionPlan = "pro";
    state.workSessions = [workSession("waiting_for_approval")];
    state.workRun = workRun("waiting_for_approval");
    state.workApproval = {
        id: "approval-1",
        work_run_id: "work-run-1",
        tool_call_id: "tool-call-1",
        connection_id: "connection-1",
        action_type: "WRITE",
        tool_name: "open_pull_request",
        description: "The report needs a pull request in the selected repository.",
        request_payload: { repository: "cortex/example", branch: "work/report" },
        status: "pending",
        requested_at: "2026-08-20T12:03:00Z",
        decided_at: null,
    };
    state.workEvents = [
        workEvent(1, "plan_created", "Plan created"),
        workEvent(2, "approval_required", "Your approval is required", { approval_ids: ["approval-1"] }),
    ];
    await page.goto("/work/work-session-1");

    await expect(page.getByText("Approval needed")).toBeVisible();
    await expect(page.getByText("cortex/example")).toBeVisible();
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText("Approval needed")).toHaveCount(0);
    expect(state.workApproval.status).toBe("approved");
});

test("desktop Work preserves the earlier result and deliverables after a follow-up", async ({ responsiveApp }) => {
    const { page, state } = responsiveApp;
    state.subscriptionPlan = "pro";
    seedWorkHistory(state);

    await page.goto("/work/work-session-1");

    const originalTurn = page.locator("[data-work-run-id='work-run-security']");
    const followupTurn = page.locator("[data-work-run-id='work-run-deliverables']");
    await expect(originalTurn.getByText("Analyze the application security concerns")).toBeVisible();
    await expect(originalTurn.getByText("Security review complete with six findings.")).toBeVisible();
    await expect(originalTurn.getByText("SECURITY_ANALYSIS_REPORT.md")).toBeVisible();
    await expect(followupTurn.getByText("Where are the deliverables? I do not see them.")).toBeVisible();
    await expect(followupTurn.locator("p").filter({ hasText: "The deliverables remain attached above." })).toBeVisible();
    await expectNoHorizontalOverflow(page);
});

function workSession(status) {
    return {
        id: "work-session-1", session_id: "session-work-1", title: "Prepare a market report",
        status, agent_provider: "fake", created_at: "2026-08-20T12:00:00Z",
        updated_at: "2026-08-20T12:03:00Z", latest_run_status: status,
    };
}

function workRun(status) {
    return {
        id: "work-run-1", work_session_id: "work-session-1", request_id: "request-1",
        instruction: "Prepare a market report", status, provider: "fake",
        max_credit_budget: 100000, reserved_credits: 100000, actual_credits: 6400,
        max_output_tokens: 40000, actual_output_tokens: 6400,
        provider_model_id: "claude-haiku-4-5", billing_model_id: "claude-haiku-4-5",
        billing_model_source: "fake_session_agent_snapshot", provider_agent_id: "fake-agent",
        provider_agent_version: 1, output_finalize_requested_at: null,
        output_limit_interrupt_requested_at: null,
        configuration_snapshot: { requested_web_mode: "auto", effective_web_enabled: false, enabled_connection_ids: [] }, usage_snapshot: {},
        stop_reason: null, error_code: null, error_message: null,
        started_at: "2026-08-20T12:00:00Z", completed_at: null,
        created_at: "2026-08-20T12:00:00Z", updated_at: "2026-08-20T12:03:00Z",
    };
}

function workEvent(sequence, type, message, payload = {}) {
    return { id: `event-${sequence}`, sequence, type, display_message: message, payload, created_at: "2026-08-20T12:03:00Z" };
}

function seedWorkHistory(state) {
    const original = {
        ...workRun("completed"), id: "work-run-security",
        instruction: "Analyze the application security concerns",
        started_at: "2026-08-20T12:00:00Z", completed_at: "2026-08-20T12:04:00Z",
        created_at: "2026-08-20T12:00:00Z", updated_at: "2026-08-20T12:04:00Z",
    };
    const followup = {
        ...workRun("completed"), id: "work-run-deliverables",
        instruction: "Where are the deliverables? I do not see them.",
        started_at: "2026-08-20T12:05:00Z", completed_at: "2026-08-20T12:06:00Z",
        created_at: "2026-08-20T12:05:00Z", updated_at: "2026-08-20T12:06:00Z",
    };
    state.workSessions = [workSession("completed")];
    state.workRun = followup;
    state.workRuns = [original, followup];
    state.workEventsByRun.set(original.id, [workEvent(1, "agent_message", "Security review complete with six findings.")]);
    state.workEventsByRun.set(followup.id, [workEvent(2, "agent_message", "The deliverables remain attached above.")]);
    state.workArtifactsByRun.set(original.id, [{
        id: "artifact-security", file_id: "artifact-security-file", role: "artifact", source: "agent",
        filename: "SECURITY_ANALYSIS_REPORT.md", mime_type: "text/markdown", size_bytes: 4096,
        artifact_type: "report", metadata: {}, created_at: "2026-08-20T12:04:00Z",
    }]);
}
