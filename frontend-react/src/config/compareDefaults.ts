import type { ModelCatalogItem } from "../types";

export const DEFAULT_COMPARE_MODEL_KEYS = [
  "openai:gpt-5.6-luna",
  "claude:claude-sonnet-5",
] as const;

export const DEFAULT_ADDED_COMPARE_MODEL_KEY = "deepseek:deepseek-v4-flash";

export function newCompareModelKeys(
  models: ModelCatalogItem[],
  smartOffModel: string,
  eligibleModels: ModelCatalogItem[] = models,
): [string, string, string] {
  const second =
    smartOffModel === DEFAULT_COMPARE_MODEL_KEYS[1]
      ? DEFAULT_COMPARE_MODEL_KEYS[0]
      : DEFAULT_COMPARE_MODEL_KEYS[1];
  const eligibleKeys = eligibleModels.map(modelKey);
  return resolveCompareModelKeys(
    models,
    [smartOffModel, eligibleKeys.includes(second) ? second : "", ""],
    eligibleModels,
  );
}

export function resolveCompareModelKeys(
  models: ModelCatalogItem[],
  currentKeys: [string, string, string],
  defaultModels: ModelCatalogItem[] = models,
): [string, string, string] {
  const availableKeys = models.map(modelKey);
  const defaultKeys = defaultModels.map(modelKey).filter((key) => availableKeys.includes(key));
  const selected = new Set<string>();
  const resolved = currentKeys.map((currentKey, index) => {
    if (currentKey && availableKeys.includes(currentKey) && !selected.has(currentKey)) {
      selected.add(currentKey);
      return currentKey;
    }

    if (index === 2) return "";

    const preferredKey = DEFAULT_COMPARE_MODEL_KEYS[index];
    const nextKey =
      defaultKeys.find((key) => key === preferredKey && !selected.has(key)) ??
      defaultKeys.find((key) => !selected.has(key)) ??
      "";
    if (nextKey) selected.add(nextKey);
    return nextKey;
  });

  return resolved as [string, string, string];
}

export function resolveAddedCompareModelKey(
  models: ModelCatalogItem[],
  currentKeys: [string, string, string],
): string {
  const availableKeys = models.map(modelKey);
  if (
    availableKeys.includes(DEFAULT_ADDED_COMPARE_MODEL_KEY) &&
    !currentKeys.includes(DEFAULT_ADDED_COMPARE_MODEL_KEY)
  ) {
    return DEFAULT_ADDED_COMPARE_MODEL_KEY;
  }
  return availableKeys.find((key) => !currentKeys.includes(key)) ?? "";
}

export function removeCompareModelKey(
  currentKeys: [string, string, string],
  removeIndex: 0 | 1 | 2,
): [string, string, string] {
  const remaining = currentKeys.filter((key, index) => index !== removeIndex && Boolean(key));
  return [remaining[0] ?? "", remaining[1] ?? "", remaining[2] ?? ""];
}

function modelKey(model: ModelCatalogItem): string {
  return `${model.provider}:${model.model}`;
}
