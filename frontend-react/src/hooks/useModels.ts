import { useState, useEffect } from "react";
import { fetchModels } from "../api/catalog";
import type { ModelCatalogItem } from "../types";
import { DEFAULT_MODELS } from "../config/defaultModels";

interface UseModelsResult {
  models: ModelCatalogItem[];
  loading: boolean;
  error: string | null;
}

export function useModels(enabled = true): UseModelsResult {
  const [models, setModels] = useState<ModelCatalogItem[]>(DEFAULT_MODELS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled) {
      setModels(DEFAULT_MODELS);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    fetchModels(true)
      .then((data) => {
        if (!cancelled) setModels(data.models.length > 0 ? data.models : DEFAULT_MODELS);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setModels(DEFAULT_MODELS);
          setError(err instanceof Error ? err.message : "Failed to load models");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return { models, loading, error };
}

/** Returns a display label for a model key "provider:model". */
export function modelKeyLabel(key: string, models: ModelCatalogItem[]): string {
  const [provider, model] = key.split(":");
  const found = models.find((m) => m.provider === provider && m.model === model);
  if (found) return `${found.model} (${found.provider})`;
  return key;
}
