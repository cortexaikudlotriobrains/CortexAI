import { get } from "./client";
import type { ModelsCatalogResponse, ProvidersCatalogResponse } from "../types";

export async function fetchModels(enabledOnly = true): Promise<ModelsCatalogResponse> {
  return get<ModelsCatalogResponse>(`/v1/models?enabled_only=${enabledOnly}`);
}

export async function fetchModelOptions(): Promise<ModelsCatalogResponse> {
  return get<ModelsCatalogResponse>("/v1/model-options");
}

export async function fetchProviders(): Promise<ProvidersCatalogResponse> {
  return get<ProvidersCatalogResponse>("/v1/providers");
}
