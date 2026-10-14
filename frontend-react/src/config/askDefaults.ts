import type {
  ModelBillingClass,
  ModelCatalogItem,
  SubscriptionPlanCode,
} from "../types";

// Plan defaults live in config/model_registry.yaml (ui_defaults.ask_by_plan) and are
// generated into ./generated/registryDefaults.ts by `python scripts/models.py sync-frontend`.
import { DEFAULT_ASK_MODEL_KEYS_BY_PLAN } from "./generated/registryDefaults";

export { DEFAULT_ASK_MODEL_KEYS_BY_PLAN };

export function resolveAskModelKey(
  models: ModelCatalogItem[],
  currentKey: string,
  planCode: SubscriptionPlanCode | null,
  allowedBillingClasses: readonly ModelBillingClass[] | null,
): string {
  const availableKeys = new Set(models.map(modelKey));
  if (currentKey && availableKeys.has(currentKey)) return currentKey;

  const eligibleModels = allowedBillingClasses
    ? models.filter((model) => allowedBillingClasses.includes(model.billing_class))
    : models;
  const preferredKey = planCode ? DEFAULT_ASK_MODEL_KEYS_BY_PLAN[planCode] : null;
  if (preferredKey && eligibleModels.some((model) => modelKey(model) === preferredKey)) {
    return preferredKey;
  }
  return eligibleModels[0] ? modelKey(eligibleModels[0]) : "";
}

function modelKey(model: ModelCatalogItem): string {
  return `${model.provider}:${model.model}`;
}
