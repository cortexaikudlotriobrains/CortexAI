import type {
  GenerationRequest,
  ModelCatalogItem,
  ReasoningLevel,
} from "../types";

export const MANUAL_REASONING_LEVELS: Exclude<ReasoningLevel, "auto">[] = [
  "low",
  "medium",
  "high",
  "max",
];

export function modelReasoningLevels(
  model: ModelCatalogItem | undefined,
): Exclude<ReasoningLevel, "auto">[] {
  if (!model?.reasoning_controllable) return [];
  const configured = new Set(model.reasoning_levels ?? []);
  return MANUAL_REASONING_LEVELS.filter((level) => configured.has(level));
}

export function modelForKey(
  key: string,
  models: ModelCatalogItem[],
): ModelCatalogItem | undefined {
  const separator = key.indexOf(":");
  if (separator < 1) return undefined;
  const provider = key.slice(0, separator);
  const model = key.slice(separator + 1);
  return models.find((item) => item.provider === provider && item.model === model);
}

export function smartReasoningLevels(
  models: ModelCatalogItem[],
): Exclude<ReasoningLevel, "auto">[] {
  const supported = new Set(
    models.flatMap((model) => modelReasoningLevels(model)),
  );
  return MANUAL_REASONING_LEVELS.filter((level) => supported.has(level));
}

export function compareReasoningLevels(
  keys: string[],
  models: ModelCatalogItem[],
): Exclude<ReasoningLevel, "auto">[] {
  const selected = keys.filter(Boolean).map((key) => modelForKey(key, models));
  if (selected.length === 0 || selected.some((model) => !model)) return [];

  return MANUAL_REASONING_LEVELS.filter((level) =>
    selected.every((model) => modelReasoningLevels(model).includes(level)),
  );
}

export function defaultModelReasoningLevel(
  model: ModelCatalogItem | undefined,
): ReasoningLevel {
  const supported = modelReasoningLevels(model);
  if (model?.default_reasoning_level && supported.includes(model.default_reasoning_level)) {
    return model.default_reasoning_level;
  }
  return supported[0] ?? "auto";
}

export function generationForReasoningLevel(
  level: ReasoningLevel,
  profile: GenerationRequest["profile"] = "auto",
): GenerationRequest {
  return {
    profile,
    reasoning:
      level === "auto"
        ? { mode: "auto", effort: "auto" }
        : { mode: "on", effort: level },
  };
}

export function reasoningLevelFromResponse(
  requestedEffort: string | undefined,
): ReasoningLevel {
  const normalized = String(requestedEffort ?? "auto").toLowerCase();
  if (normalized === "minimal" || normalized === "low") return "low";
  if (normalized === "medium") return "medium";
  if (normalized === "high") return "high";
  if (normalized === "xhigh" || normalized === "max") return "max";
  return "auto";
}
