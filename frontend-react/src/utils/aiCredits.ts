/**
 * Cortex APIs and browser state keep credits as integer metering units.
 * Customer-facing React surfaces present 1 AI credit per 1,000 metering units.
 */
export const CREDIT_UNITS_PER_DISPLAY_CREDIT = 1_000;

const DISPLAY_CREDIT_FORMATTER = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 3,
});

export function toDisplayAiCredits(creditUnits: number): number {
  return creditUnits / CREDIT_UNITS_PER_DISPLAY_CREDIT;
}

export function formatAiCredits(creditUnits: number): string {
  return DISPLAY_CREDIT_FORMATTER.format(toDisplayAiCredits(creditUnits));
}
