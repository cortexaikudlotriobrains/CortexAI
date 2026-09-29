import type {
  ChatMode,
  ChatResponse,
  ChatTurn,
  CompareResponse,
  HistoryEntry,
  HistoryThread,
} from "../types";

export function buildHistoryThreads(entries: HistoryEntry[]): HistoryThread[] {
  const grouped = new Map<string, { sessionId?: string; entries: HistoryEntry[] }>();

  for (const entry of entries) {
    const sessionId = normalizeId(entry.session_id);
    const key = sessionId ? `session:${sessionId}` : `entry:${entry.id}`;
    const group = grouped.get(key) ?? { sessionId, entries: [] };
    group.entries.push(entry);
    grouped.set(key, group);
  }

  return [...grouped.entries()]
    .map(([key, group]) => buildThread(key, group.sessionId, group.entries))
    .sort((left, right) => right.latestTimestampMs - left.latestTimestampMs);
}

export function filterHistoryThreads(threads: HistoryThread[], query: string): HistoryThread[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return threads;
  return threads.filter((thread) => thread.searchText.includes(normalized));
}

export function buildTurnsFromHistoryEntries(entries: HistoryEntry[]): ChatTurn[] {
  const sorted = sortEntries(entries);
  const turnGroups = new Map<string, HistoryEntry[]>();

  for (const entry of sorted) {
    const mode = normalizeMode(entry.mode);
    const key = mode === "compare" ? compareTurnKey(entry) : `ask:${entry.id}`;
    const group = turnGroups.get(key) ?? [];
    group.push(entry);
    turnGroups.set(key, group);
  }

  return [...turnGroups.entries()]
    .map(([key, group]) => buildTurn(key, group))
    .sort((left, right) => Date.parse(left.createdAt) - Date.parse(right.createdAt));
}

function buildThread(
  key: string,
  sessionId: string | undefined,
  rawEntries: HistoryEntry[],
): HistoryThread {
  const entries = sortEntries(rawEntries);
  const first = entries[0];
  const latest = entries[entries.length - 1];
  const modes = new Set(entries.map((entry) => normalizeMode(entry.mode)));
  const providers = distinct(entries.map((entry) => entry.provider));
  const models = distinct(entries.map((entry) => entry.model));
  const turns = buildTurnsFromHistoryEntries(entries);
  const title = resolveThreadTitle(first);

  return {
    key,
    sessionId,
    entries,
    title,
    latestTimestamp: latest?.timestamp || "",
    latestTimestampMs: parseTimestamp(latest?.timestamp),
    mode: modes.size > 1 ? "mixed" : (modes.values().next().value ?? "single"),
    preferredMode: normalizeMode(latest?.mode),
    providerLabel: providers.length > 1 ? "mixed" : providers[0] || "unknown",
    modelLabel: models.length > 1 ? "mixed" : models[0] || "unknown",
    totalCost: entries.reduce((sum, entry) => sum + finiteNumber(entry.cost), 0),
    totalTokens: entries.reduce((sum, entry) => sum + finiteNumber(entry.tokens), 0),
    turnCount: turns.length,
    searchText: [
      title,
      ...entries.map((entry) =>
        [entry.prompt, entry.response, entry.provider, entry.model].join(" "),
      ),
    ]
      .join(" ")
      .toLowerCase(),
  };
}

function resolveThreadTitle(first: HistoryEntry | undefined): string {
  const sessionTitle = first?.session_title?.trim() ?? "";
  if (sessionTitle && !isGenericApiSessionTitle(sessionTitle)) return sessionTitle;
  return first?.prompt || "[prompt not stored]";
}

function isGenericApiSessionTitle(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return normalized === "api chat" || normalized === "api compare";
}

function buildTurn(key: string, entries: HistoryEntry[]): ChatTurn {
  const sorted = sortEntries(entries);
  const first = sorted[0]!;
  const mode = normalizeMode(first.mode);
  const responses = sorted.map(toChatResponse);
  const requestGroupId = normalizeId(first.request_group_id);
  const researchAiCredits = sorted.reduce(
    (highest, entry) => Math.max(highest, finiteNumber(entry.research_ai_credits)),
    0,
  );

  return {
    id: `history:${key}`,
    mode,
    prompt: first.prompt,
    submittedPrompt: first.prompt,
    attachments: [],
    responses,
    status: "complete",
    createdAt: first.timestamp,
    requestGroupId,
    compareSummary:
      mode === "compare"
        ? buildCompareSummary(
            requestGroupId ?? key,
            first.session_id,
            responses,
            first.timestamp,
            researchAiCredits,
          )
        : undefined,
  };
}

function buildCompareSummary(
  requestGroupId: string,
  sessionId: string | undefined,
  responses: ChatResponse[],
  timestamp: string,
  researchAiCredits: number,
): CompareResponse {
  const errorCount = responses.filter((response) => response.error).length;
  return {
    request_group_id: requestGroupId,
    session_id: sessionId,
    responses,
    success_count: responses.length - errorCount,
    error_count: errorCount,
    total_tokens: responses.reduce(
      (sum, response) => sum + finiteNumber(response.token_usage?.total_tokens),
      0,
    ),
    total_cost: responses.reduce((sum, response) => sum + response.estimated_cost, 0),
    total_ai_credits:
      responses.reduce((sum, response) => sum + finiteNumber(response.ai_credits), 0) +
      researchAiCredits,
    timestamp,
  };
}

function toChatResponse(entry: HistoryEntry): ChatResponse {
  const isError = /^\[error\]/i.test(entry.response);
  const retryProfile = nextGenerationProfile(
    entry.generation_profile,
    entry.effective_max_output_tokens,
  );
  return {
    request_id: entry.request_id || String(entry.id),
    response_version: entry.response_version ?? 1,
    session_id: entry.session_id,
    text: isError ? "" : entry.response,
    provider: entry.provider,
    model: entry.model,
    routing_mode: entry.routing_mode,
    latency_ms: finiteNumberOrNull(entry.latency_ms),
    token_usage:
      finiteNumberOrNull(entry.tokens) === null
        ? null
        : {
            prompt_tokens: finiteNumber(entry.prompt_tokens),
            completion_tokens: finiteNumber(entry.completion_tokens),
            total_tokens: Number(entry.tokens),
          },
    estimated_cost: entry.cost ?? 0,
    cost_currency: "USD",
    ai_credits: entry.ai_credits,
    credit_usage_estimated: entry.credit_usage_estimated,
    completion_status: entry.completion_status ?? (isError ? "failed" : "complete"),
    stop_cause: entry.stop_cause ?? (isError ? "error" : "unknown"),
    generation_budget:
      entry.generation_profile && entry.effective_max_output_tokens
        ? {
            profile: entry.generation_profile,
            requested_max_output_tokens: entry.effective_max_output_tokens,
            effective_max_output_tokens: entry.effective_max_output_tokens,
            requested_reasoning_mode: "auto",
            effective_reasoning_mode: entry.effective_reasoning_mode ?? "none",
            requested_reasoning_effort: "auto",
            effective_reasoning_effort: entry.effective_reasoning_effort ?? "none",
            reasoning_disable_supported: true,
            reasoning_counts_against_output: true,
            policy_version: entry.generation_policy_version ?? "legacy-unrecorded",
          }
        : undefined,
    retry_with_more_room: {
      available: entry.completion_status === "incomplete" && Boolean(retryProfile),
      recommended_profile: retryProfile,
    },
    error: isError
      ? {
          code: entry.error_code || "persisted_error",
          message: entry.response.replace(/^\[error\]\s*/, ""),
          provider: entry.provider,
          retryable: false,
          details: {},
        }
      : undefined,
    web_source_items: entry.web_source_items ?? [],
    timestamp: entry.timestamp,
    ui_status: isError
      ? "failed"
      : entry.completion_status === "incomplete"
        ? "incomplete"
        : "complete",
  };
}

function nextGenerationProfile(
  profile: HistoryEntry["generation_profile"],
  effectiveMaxOutputTokens?: number,
): HistoryEntry["generation_profile"] | undefined {
  const order: NonNullable<HistoryEntry["generation_profile"]>[] = [
    "quick",
    "balanced",
    "deep",
    "extended",
  ];
  if (!profile) return undefined;
  if (profile === "auto") {
    if ((effectiveMaxOutputTokens ?? 0) >= 32768) return undefined;
    return (effectiveMaxOutputTokens ?? 0) >= 12288 ? "extended" : "deep";
  }
  const index = order.indexOf(profile);
  return index >= 0 && index + 1 < order.length ? order[index + 1] : undefined;
}

function compareTurnKey(entry: HistoryEntry): string {
  const requestGroupId = normalizeId(entry.request_group_id);
  if (requestGroupId) return `compare:${requestGroupId}`;

  const bucket = Math.floor(parseTimestamp(entry.timestamp) / 5000);
  return `compare-fallback:${bucket}:${entry.prompt}`;
}

function normalizeMode(value: string | undefined): ChatMode {
  return String(value || "")
    .trim()
    .toLowerCase() === "compare"
    ? "compare"
    : "single";
}

function normalizeId(value: string | undefined): string | undefined {
  const normalized = String(value || "").trim();
  return normalized || undefined;
}

function sortEntries(entries: HistoryEntry[]): HistoryEntry[] {
  return [...entries].sort((left, right) => {
    const timestampDelta = parseTimestamp(left.timestamp) - parseTimestamp(right.timestamp);
    return timestampDelta || left.id - right.id;
  });
}

function parseTimestamp(value: string | undefined): number {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function finiteNumber(value: number | undefined): number {
  return Number.isFinite(value) ? Number(value) : 0;
}

function finiteNumberOrNull(value: number | undefined): number | null {
  return Number.isFinite(value) ? Number(value) : null;
}

function distinct(values: string[]): string[] {
  return [...new Set(values.map((value) => String(value || "").trim()).filter(Boolean))];
}
