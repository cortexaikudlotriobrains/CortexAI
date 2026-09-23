import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { useModels } from "../hooks/useModels";

const mocks = vi.hoisted(() => ({
  fetchModelOptions: vi.fn(),
  fetchModels: vi.fn(),
}));

vi.mock("../api/catalog", () => ({
  fetchModelOptions: mocks.fetchModelOptions,
  fetchModels: mocks.fetchModels,
}));

describe("useModels", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("uses the bundled selector fallback without calling an API while disabled", async () => {
    const { result } = renderHook(() => useModels(false));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.models).toEqual(DEFAULT_MODELS);
    expect(result.current.error).toBeNull();
    expect(mocks.fetchModelOptions).not.toHaveBeenCalled();
    expect(mocks.fetchModels).not.toHaveBeenCalled();
  });

  it("loads Ask and Compare options independently of the rich catalogue", async () => {
    const models = [
      ...DEFAULT_MODELS,
      { ...DEFAULT_MODELS[0]!, provider: "gemini", model: "gemini-3.6-flash" },
      { ...DEFAULT_MODELS[0]!, provider: "grok", model: "grok-4.3" },
    ];
    mocks.fetchModelOptions.mockResolvedValue({
      enabled_only: true,
      models,
      total: models.length,
      timestamp: "2026-09-22T00:00:00Z",
    });

    const { result } = renderHook(() => useModels(true));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(new Set(result.current.models.map((model) => model.provider))).toEqual(
      new Set(["openai", "claude", "deepseek", "gemini", "grok"]),
    );
    expect(mocks.fetchModelOptions).toHaveBeenCalledTimes(1);
    expect(mocks.fetchModels).not.toHaveBeenCalled();
  });

  it("uses the gated catalogue endpoint only for the Models screen", async () => {
    mocks.fetchModels.mockResolvedValue({
      enabled_only: true,
      models: DEFAULT_MODELS,
      total: DEFAULT_MODELS.length,
      timestamp: "2026-09-22T00:00:00Z",
    });

    const { result } = renderHook(() => useModels(true, "catalog"));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.models).toEqual(DEFAULT_MODELS);
    expect(mocks.fetchModels).toHaveBeenCalledWith(true);
    expect(mocks.fetchModelOptions).not.toHaveBeenCalled();
  });
});
