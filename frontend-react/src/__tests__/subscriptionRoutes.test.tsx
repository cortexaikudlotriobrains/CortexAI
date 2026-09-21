import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../pages/ChatPage", () => ({ ChatPage: () => <div>Chat route</div> }));
vi.mock("../pages/CreditsPage", () => ({ CreditsPage: () => <div>Credits route</div> }));
vi.mock("../pages/ModelsPage", () => ({ ModelsPage: () => <div>Models route</div> }));
vi.mock("../pages/UsageInsightsPage", () => ({
  UsageInsightsPage: () => <div>Usage route</div>,
}));
vi.mock("../pages/PricingPage", () => ({ PricingPage: () => <div>Pricing route</div> }));
vi.mock("../pages/BillingPage", () => ({ BillingPage: () => <div>Billing route</div> }));

import { App } from "../App";

describe("subscription routes", () => {
  beforeEach(() => window.history.replaceState({}, "", "/"));
  afterEach(() => {
    cleanup();
    delete (
      window as unknown as { CORTEX_RUNTIME_CONFIG?: Record<string, unknown> }
    ).CORTEX_RUNTIME_CONFIG;
  });

  it("routes /pricing to the consumer plan catalogue", () => {
    window.history.replaceState({}, "", "/pricing");
    render(<App />);
    expect(screen.getByText("Pricing route")).toBeInTheDocument();
  });

  it("routes /account/billing to account plan management", () => {
    window.history.replaceState({}, "", "/account/billing");
    render(<App />);
    expect(screen.getByText("Billing route")).toBeInTheDocument();
  });

  it("routes /credits to the AI credit balance and activity page", () => {
    window.history.replaceState({}, "", "/credits");
    render(<App />);
    expect(screen.getByText("Credits route")).toBeInTheDocument();
  });

  it("redirects /models to Ask when the catalogue is disabled", () => {
    window.history.replaceState({}, "", "/models");
    render(<App />);

    expect(screen.getByText("Chat route")).toBeInTheDocument();
    expect(screen.queryByText("Models route")).not.toBeInTheDocument();
  });

  it("keeps /models available behind the runtime flag", () => {
    (
      window as unknown as { CORTEX_RUNTIME_CONFIG?: Record<string, unknown> }
    ).CORTEX_RUNTIME_CONFIG = { modelsCatalogEnabled: true };
    window.history.replaceState({}, "", "/models");
    render(<App />);

    expect(screen.getByText("Models route")).toBeInTheDocument();
  });
});
