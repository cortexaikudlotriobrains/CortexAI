import type {
  AnalysisLinkedSource,
  AnalysisPassage,
  ChatResponse,
  CortexAnalysisRun,
} from "../types";
import { normalizeSourceUrl } from "../utils/sourceMeta";
import { sourcePassages, type SourcePassage } from "../markdown/sourcePassages";

export type SourceResponse = Omit<AnalysisPassage, "quote">;
export interface SourceReference extends SourceResponse {
  label?: string;
  available: boolean;
  passages: SourcePassage[];
}
export interface AnalysisSourceDetails extends AnalysisLinkedSource {
  references: SourceReference[];
}
const identity = (ref: { requestId: string; responseVersion: number }) =>
  `${ref.requestId}:${ref.responseVersion}`;

export function collectAnalysisSources(
  responses: ChatResponse[],
  run: CortexAnalysisRun | undefined,
  includePassages: boolean,
): AnalysisSourceDetails[] {
  const sources = new Map<string, AnalysisLinkedSource>();
  function add(source: AnalysisLinkedSource) {
    const url = normalizeSourceUrl(source.url);
    if (!url) return;
    const previous = sources.get(url);
    if (previous) {
      for (const ref of source.attributions)
        if (!previous.attributions.some((item) => identity(item) === identity(ref)))
          previous.attributions.push({ ...ref });
    } else
      sources.set(url, {
        ...source,
        url,
        domain: new URL(url).hostname,
        attributions: source.attributions.map((ref) => ({ ...ref })),
      });
  }
  for (const source of run?.structuredResult?.linkedSources ?? []) add(source);
  for (const response of responses)
    for (const source of response.web_source_items ?? [])
      add({
        ...source,
        sourceId: source.url,
        domain: "",
        relationship: "attached_to_response",
        attributions: [
          { requestId: response.request_id, responseVersion: response.response_version ?? 1 },
        ],
      });
  // Snapshots retain the original provider source ordering. Never resolve an old
  // [n] using today's response/source list, or the global deduplicated list.
  const contexts = new Map<
    string,
    {
      ref: SourceResponse;
      label?: string;
      text?: string | null;
      sources: ChatResponse["web_source_items"];
    }
  >();
  for (const response of responses) {
    const ref = {
      requestId: response.request_id,
      responseVersion: response.response_version ?? 1,
      provider: response.provider,
      model: response.model,
    };
    contexts.set(identity(ref), {
      ref,
      text: response.text,
      sources: response.web_source_items ?? [],
    });
  }
  for (const snapshot of run?.sourceResponses ?? []) {
    const key = identity(snapshot);
    const current = contexts.get(key);
    contexts.set(key, {
      ref: {
        requestId: snapshot.requestId,
        responseVersion: snapshot.responseVersion,
        provider: snapshot.provider || current?.ref.provider || "",
        model: snapshot.model || current?.ref.model || "",
      },
      label: snapshot.responseName,
      text: snapshot.content ?? current?.text,
      sources: snapshot.webSources ?? current?.sources ?? [],
    });
    for (const source of snapshot.webSources ?? [])
      add({
        ...source,
        sourceId: source.url,
        domain: "",
        relationship: "attached_to_response",
        attributions: [
          { requestId: snapshot.requestId, responseVersion: snapshot.responseVersion },
        ],
      });
  }
  const citations = new Map<string, Map<string, SourcePassage[]>>();
  if (includePassages)
    for (const [key, context] of contexts) {
      if (!context.text) continue;
      const approved = [...sources.values()]
        .filter((source) => source.attributions.some((ref) => identity(ref) === key))
        .map((source) => source.url);
      if (!approved.length) continue;
      citations.set(key, sourcePassages(context.text, context.sources, approved));
    }
  return [...sources.values()].map((source) => ({
    ...source,
    references: source.attributions.map((ref) => {
      const key = identity(ref);
      const context = contexts.get(key);
      return {
        ...ref,
        provider: context?.ref.provider ?? "",
        model: context?.ref.model ?? "",
        label: context?.label,
        available: !!context?.text,
        passages: citations.get(key)?.get(source.url) ?? [],
      };
    }),
  }));
}
