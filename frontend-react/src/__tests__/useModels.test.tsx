import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_MODELS } from "../config/defaultModels";
import { useModels } from "../hooks/useModels";

const mocks = vi.hoisted(() => ({
  fetchModels: vi.fn(),
}));

vi.mock("../api/catalog", () => ({
  fetchModels: mocks.fetchModels,
}));

describe("useModels", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("uses the bundled selector fallback without calling the disabled catalogue", async () => {
    const { result } = renderHook(() => useModels(false));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.models).toEqual(DEFAULT_MODELS);
    expect(result.current.error).toBeNull();
    expect(mocks.fetchModels).not.toHaveBeenCalled();
  });
});
