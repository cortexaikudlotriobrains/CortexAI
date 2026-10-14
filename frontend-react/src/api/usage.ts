import { ApiClientError, buildHeaders, get } from "./client";
import type { UsageSummary } from "../types";

export interface UsageSummaryParams {
  from?: string;
  to?: string;
}

export interface UsageExportParams extends UsageSummaryParams {
  groupBy?: "day" | "provider" | "model";
}

export interface UsageByokStatus {
  providers: Array<{ provider: string; configured: boolean }>;
}

export function fetchUsageByokStatus(signal?: AbortSignal): Promise<UsageByokStatus> {
  return get<UsageByokStatus>("/v1/byok/status", signal);
}

export async function fetchUsageSummary(
  params: UsageSummaryParams = {},
  signal?: AbortSignal,
): Promise<UsageSummary> {
  const query = new URLSearchParams();
  if (params.from) query.set("from", params.from);
  if (params.to) query.set("to", params.to);

  const suffix = query.toString();
  return get<UsageSummary>(`/v1/usage/summary${suffix ? `?${suffix}` : ""}`, signal);
}

export async function exportUsageCsv(
  params: UsageExportParams = {},
  signal?: AbortSignal,
): Promise<Blob> {
  const query = new URLSearchParams({ format: "csv", group_by: params.groupBy ?? "day" });
  if (params.from) query.set("from", params.from);
  if (params.to) query.set("to", params.to);

  const res = await fetch(`/v1/usage/export?${query.toString()}`, {
    method: "GET",
    credentials: "include",
    headers: buildHeaders({ Accept: "text/csv" }),
    signal,
  });

  if (!res.ok) {
    const { body, message } = await parseCsvExportError(res);
    throw new ApiClientError(res.status, message, body);
  }

  return res.blob();
}

async function parseCsvExportError(res: Response): Promise<{ body: unknown; message: string }> {
  const text = await res.text().catch(() => "");
  if (!text) return { body: null, message: res.statusText || "Usage export failed" };

  try {
    const body = JSON.parse(text) as unknown;
    return { body, message: usageExportErrorMessage(body, res.statusText) };
  } catch {
    return { body: text, message: text || res.statusText || "Usage export failed" };
  }
}

function usageExportErrorMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (typeof body !== "object" || body === null) return fallback || "Usage export failed";

  const record = body as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (typeof detail === "object" && detail !== null) {
    const detailRecord = detail as Record<string, unknown>;
    if (typeof detailRecord.message === "string") return detailRecord.message;
    if (typeof detailRecord.code === "string") return detailRecord.code;
  }
  if (typeof record.message === "string") return record.message;
  return fallback || "Usage export failed";
}
