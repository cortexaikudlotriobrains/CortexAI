import type { ModelCatalogItem } from "../types";
import { FALLBACK_MODELS } from "./generated/registryDefaults";

// Complete offline selectable catalog, including every provider, generated from
// config/model_registry.yaml by `python scripts/models.py sync-frontend`.
export const DEFAULT_MODELS: ModelCatalogItem[] = FALLBACK_MODELS;
