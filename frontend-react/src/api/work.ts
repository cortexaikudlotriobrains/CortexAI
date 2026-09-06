import { ApiClientError, buildHeaders, del, get, post, postWithHeaders } from "./client";
import type {
  ToolCatalogItem,
  ToolConnection,
  WorkApproval,
  WorkArtifact,
  WorkEvent,
  WorkEventsResponse,
  WorkRun,
  WorkSession,
  WorkWebMode,
} from "../types";

export interface StartWorkRunInput {
  instruction: string;
  input_file_ids: string[];
  enabled_connection_ids: string[];
  web_mode: WorkWebMode;
  max_credit_budget: number;
}

export interface CreateToolConnectionInput {
  display_name: string;
  server_url: string;
  auth_type: "none" | "bearer" | "oauth2";
  credential_reference?: string;
  provider_vault_id?: string;
}

export function listWorkSessions(signal?: AbortSignal): Promise<WorkSession[]> {
  return get("/v1/work/sessions", signal);
}

export function createWorkSession(title: string, signal?: AbortSignal): Promise<WorkSession> {
  return post("/v1/work/sessions", { title }, signal);
}

export function getWorkSession(id: string, signal?: AbortSignal): Promise<WorkSession> {
  return get(`/v1/work/sessions/${encodeURIComponent(id)}`, signal);
}

export function getLatestWorkRun(sessionId: string, signal?: AbortSignal): Promise<WorkRun> {
  return get(`/v1/work/sessions/${encodeURIComponent(sessionId)}/runs/latest`, signal);
}

export function listWorkRuns(sessionId: string, signal?: AbortSignal): Promise<WorkRun[]> {
  return get(`/v1/work/sessions/${encodeURIComponent(sessionId)}/runs`, signal);
}

export function startWorkRun(
  sessionId: string,
  input: StartWorkRunInput,
  requestId: string,
  signal?: AbortSignal,
): Promise<WorkRun> {
  return postWithHeaders(
    `/v1/work/sessions/${encodeURIComponent(sessionId)}/runs`,
    input,
    { "Idempotency-Key": requestId },
    signal,
  );
}

export function sendWorkInstruction(
  sessionId: string,
  input: StartWorkRunInput,
  requestId: string,
  signal?: AbortSignal,
): Promise<WorkRun> {
  return postWithHeaders(
    `/v1/work/sessions/${encodeURIComponent(sessionId)}/instructions`,
    input,
    { "Idempotency-Key": requestId },
    signal,
  );
}

export function getWorkRun(id: string, signal?: AbortSignal): Promise<WorkRun> {
  return get(`/v1/work/runs/${encodeURIComponent(id)}`, signal);
}

export function getWorkEvents(
  runId: string,
  afterSequence = 0,
  signal?: AbortSignal,
): Promise<WorkEventsResponse> {
  return get(
    `/v1/work/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}`,
    signal,
  );
}

export function cancelWorkRun(id: string, signal?: AbortSignal): Promise<WorkRun> {
  return post(`/v1/work/runs/${encodeURIComponent(id)}/cancel`, {}, signal);
}

export function listWorkArtifacts(id: string, signal?: AbortSignal): Promise<WorkArtifact[]> {
  return get(`/v1/work/runs/${encodeURIComponent(id)}/artifacts`, signal);
}

export function workArtifactDownloadUrl(runId: string, fileId: string): string {
  return `/v1/work/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(fileId)}/download`;
}

export function getWorkApproval(id: string, signal?: AbortSignal): Promise<WorkApproval> {
  return get(`/v1/work/approvals/${encodeURIComponent(id)}`, signal);
}

export function decideWorkApproval(
  id: string,
  decision: "approve" | "deny",
  reason?: string,
  remember = false,
  signal?: AbortSignal,
): Promise<WorkApproval> {
  return post(
    `/v1/work/approvals/${encodeURIComponent(id)}/${decision}`,
    { reason: reason || null, remember },
    signal,
  );
}

export function listToolCatalog(signal?: AbortSignal): Promise<ToolCatalogItem[]> {
  return get("/v1/tools/catalog", signal);
}

export function listToolConnections(signal?: AbortSignal): Promise<ToolConnection[]> {
  return get("/v1/tools/connections", signal);
}

export function createToolConnection(
  input: CreateToolConnectionInput,
  signal?: AbortSignal,
): Promise<ToolConnection> {
  return post("/v1/tools/connections", input, signal);
}

export function testToolConnection(id: string, signal?: AbortSignal): Promise<unknown> {
  return post(`/v1/tools/connections/${encodeURIComponent(id)}/test`, {}, signal);
}

export function deleteToolConnection(id: string, signal?: AbortSignal): Promise<void> {
  return del(`/v1/tools/connections/${encodeURIComponent(id)}`, signal);
}

export async function beginToolOAuth(
  connectorKey: string,
  returnTo: string,
  signal?: AbortSignal,
): Promise<{ authorization_url: string; expires_at: string }> {
  return post(
    `/v1/tools/${encodeURIComponent(connectorKey)}/oauth/start`,
    { return_to: returnTo },
    signal,
  );
}

export async function streamWorkEvents(
  runId: string,
  afterSequence: number,
  onEvent: (event: WorkEvent) => boolean | void | Promise<boolean | void>,
  signal: AbortSignal,
): Promise<void> {
  let cursor = afterSequence;
  while (!signal.aborted) {
    const response = await fetch(
      `/v1/work/runs/${encodeURIComponent(runId)}/stream?after_sequence=${cursor}`,
      {
        credentials: "include",
        headers: buildHeaders(cursor > 0 ? { "Last-Event-ID": String(cursor) } : undefined),
        signal,
      },
    );
    if (!response.ok || !response.body) {
      throw new ApiClientError(response.status, response.statusText || "Work stream failed");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventName = "message";
    let data = "";
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line) {
          if (data) {
            const parsed = JSON.parse(data) as WorkEvent;
            cursor = Math.max(cursor, parsed.sequence);
            if (await onEvent(parsed)) return;
          }
          eventName = "message";
          data = "";
        } else if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data += line.slice(5).trim();
        }
      }
      if (done) break;
    }
    if (signal.aborted) return;
    if (eventName && data) {
      const parsed = JSON.parse(data) as WorkEvent;
      cursor = Math.max(cursor, parsed.sequence);
      if (await onEvent(parsed)) return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 750));
  }
}
