import { describe, expect, it } from "vitest";

// Imported by this path on purpose: Vitest aliases only "./generated/registryDefaults"
// (used by the config modules) to a frozen fixture, so this test sees the real file
// generated from config/model_registry.yaml.
import {
  DEFAULT_ADDED_COMPARE_MODEL_KEY,
  DEFAULT_ASK_MODEL_KEYS_BY_PLAN,
  DEFAULT_COMPARE_MODEL_KEYS,
  FALLBACK_MODELS,
} from "../config/generated/registryDefaults";
import type { ModelBillingClass, SubscriptionPlanCode } from "../types";

const PLAN_CLASSES: Record<SubscriptionPlanCode, ModelBillingClass[]> = {
  free: ["economical", "standard"],
  plus: ["economical", "standard", "advanced"],
  pro: ["economical", "standard", "advanced", "premium"],
};

const keyOf = (model: { provider: string; model: string }) => `${model.provider}:${model.model}`;

describe("registry-generated model defaults", () => {
  it("retains every current provider in the real offline catalog", () => {
    expect(new Set(FALLBACK_MODELS.map((model) => model.provider))).toEqual(
      new Set(["openai", "gemini", "claude", "grok", "deepseek"]),
    );
  });
  it("ships an offline row for every default model", () => {
    const rows = new Set(FALLBACK_MODELS.map(keyOf));
    const defaults = [
      ...Object.values(DEFAULT_ASK_MODEL_KEYS_BY_PLAN),
      ...DEFAULT_COMPARE_MODEL_KEYS,
      DEFAULT_ADDED_COMPARE_MODEL_KEY,
    ];
    for (const key of defaults) expect(rows.has(key)).toBe(true);
  });

  it("gives each plan a default model that plan can use", () => {
    for (const [plan, key] of Object.entries(DEFAULT_ASK_MODEL_KEYS_BY_PLAN)) {
      const row = FALLBACK_MODELS.find((model) => keyOf(model) === key);
      expect(PLAN_CLASSES[plan as SubscriptionPlanCode]).toContain(row?.billing_class);
    }
  });

  it("keeps fallback rows selectable and credit-classified", () => {
    for (const row of FALLBACK_MODELS) {
      expect(row.selectable).toBe(true);
      expect(row.billing_class).toBe(row.access_category);
      expect(row.input_credit_multiplier ?? 0).toBeGreaterThan(0);
      expect(row.output_credit_multiplier ?? 0).toBeGreaterThan(0);
      expect(row.credit_usage_label).toBeTruthy();
    }
  });

  it("uses two distinct Compare defaults", () => {
    expect(new Set(DEFAULT_COMPARE_MODEL_KEYS).size).toBe(2);
    expect(DEFAULT_COMPARE_MODEL_KEYS).not.toContain(DEFAULT_ADDED_COMPARE_MODEL_KEY);
  });
});
