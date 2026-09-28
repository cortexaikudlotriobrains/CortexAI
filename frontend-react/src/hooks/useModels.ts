import { useState, useEffect } from "react";
import { fetchModelOptions, fetchModels } from "../api/catalog";
import type { ModelCatalogItem } from "../types";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { presentError, type UserFacingError } from "../errors/userFacingError";

export type ModelCollectionSource = "options" | "catalog";

interface UseModelsResult {
  models: ModelCatalogItem[];
  loading: boolean;
  error: UserFacingError | null;
}

export function useModels(
  enabled = true,
  source: ModelCollectionSource = "options",
): UseModelsResult {
  const [models, setModels] = useState<ModelCatalogItem[]>(DEFAULT_MODELS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<UserFacingError | null>(null);

  useEffect(() => {
    if (!enabled) {
      setModels(DEFAULT_MODELS);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const request = source === "catalog" ? fetchModels(true) : fetchModelOptions();
    request
      .then((data) => {
        if (!cancelled) setModels(data.models.length > 0 ? data.models : DEFAULT_MODELS);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setModels(DEFAULT_MODELS);
          setError(presentError(err, "models_load"));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, source]);

  return { models, loading, error };
}

/** Returns a display label for a model key "provider:model". */
export function modelKeyLabel(key: string, models: ModelCatalogItem[]): string {
  const [provider, model] = key.split(":");
  const found = models.find((m) => m.provider === provider && m.model === model);
  if (found) return `${found.model} (${found.provider})`;
  return key;
}
