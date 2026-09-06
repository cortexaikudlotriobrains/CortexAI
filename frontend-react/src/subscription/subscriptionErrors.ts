import { ApiClientError } from "../api/client";
import { formatAiCredits } from "../utils/aiCredits";

export type SubscriptionErrorKind =
  | "authentication"
  | "access"
  | "allowance"
  | "payment"
  | "selection"
  | "configuration"
  | "provider"
  | "network"
  | "unknown";

export interface SubscriptionErrorOptions {
  code: string;
  message: string;
  status: number | null;
  kind: SubscriptionErrorKind;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export class SubscriptionError extends Error {
  readonly code: string;
  readonly status: number | null;
  readonly kind: SubscriptionErrorKind;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;

  constructor(options: SubscriptionErrorOptions) {
    super(options.message);
    this.name = "SubscriptionError";
    this.code = options.code;
    this.status = options.status;
    this.kind = options.kind;
    this.retryable = options.retryable;
    this.details = options.details ?? {};
  }
}

export function toSubscriptionError(
  error: unknown,
  fallbackMessage = "Subscription information could not be loaded.",
): SubscriptionError {
  if (error instanceof SubscriptionError) return error;

  if (error instanceof ApiClientError) {
    const detail = structuredDetail(error.body);
    const code = detail.code ?? fallbackCode(error.status);
    const fallback = detail.message ?? error.message ?? fallbackMessage;
    return new SubscriptionError({
      code,
      message: displaySubscriptionMessage(code, detail.fields, fallback),
      status: error.status,
      kind: errorKind(code, error.status),
      retryable: isRetryable(code, error.status),
      details: detail.fields,
    });
  }

  if (error instanceof TypeError) {
    return new SubscriptionError({
      code: "billing_network_error",
      message: "The billing service could not be reached. Please try again.",
      status: null,
      kind: "network",
      retryable: true,
    });
  }

  return new SubscriptionError({
    code: "subscription_request_failed",
    message: error instanceof Error && error.message ? error.message : fallbackMessage,
    status: null,
    kind: "unknown",
    retryable: false,
  });
}

export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

export function isSubscriptionDenial(error: unknown): error is SubscriptionError {
  const resolved = error instanceof SubscriptionError ? error : toSubscriptionError(error);
  return (
    resolved.kind === "access" ||
    resolved.kind === "allowance" ||
    resolved.kind === "payment"
  );
}

export function localSubscriptionDenial(options: {
  code:
    | "feature_not_in_plan"
    | "model_not_in_plan"
    | "monthly_allowance_exhausted"
    | "insufficient_credits";
  message: string;
  details?: Record<string, unknown>;
}): SubscriptionError {
  const status =
    options.code === "monthly_allowance_exhausted"
      ? 429
      : options.code === "insufficient_credits"
        ? 402
        : 403;
  return new SubscriptionError({
    code: options.code,
    message: options.message,
    status,
    kind:
      options.code === "monthly_allowance_exhausted" ||
      options.code === "insufficient_credits"
        ? "allowance"
        : "access",
    retryable: false,
    details: options.details,
  });
}

export function detailString(
  error: SubscriptionError,
  key: string,
): string | null {
  const value = error.details[key];
  return typeof value === "string" && value.trim() ? value : null;
}

interface StructuredDetail {
  code?: string;
  message?: string;
  fields: Record<string, unknown>;
}

function structuredDetail(body: unknown): StructuredDetail {
  if (!isRecord(body)) return { fields: {} };
  const candidate = isRecord(body.detail) ? body.detail : body;
  const fields = { ...candidate };
  const code = typeof candidate.code === "string" ? candidate.code : undefined;
  const message = typeof candidate.message === "string" ? candidate.message : undefined;
  delete fields.code;
  delete fields.message;
  return { code, message, fields };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function displaySubscriptionMessage(
  code: string,
  fields: Record<string, unknown>,
  fallback: string,
): string {
  if (code !== "insufficient_credits") return fallback;
  const required = finiteNumber(fields.required);
  const remaining = finiteNumber(fields.remaining);
  if (required === null || remaining === null) return fallback;
  return `This request is estimated to require ${formatAiCredits(required)} AI credits. You have ${formatAiCredits(remaining)} remaining.`;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fallbackCode(status: number): string {
  if (status === 401 || status === 403) return "billing_authentication_required";
  if (status === 429) return "monthly_allowance_exhausted";
  if (status >= 500) return "billing_service_unavailable";
  return "subscription_request_failed";
}

function errorKind(code: string, status: number): SubscriptionErrorKind {
  if (
    code === "billing_identity_not_found" ||
    code === "billing_authentication_required" ||
    code === "session_auth_required"
  ) {
    return "authentication";
  }
  if (code === "monthly_allowance_exhausted" || code === "insufficient_credits") {
    return "allowance";
  }
  if (code === "subscription_payment_required") return "payment";
  if (code === "feature_not_in_plan" || code === "model_not_in_plan" || status === 403) {
    return "access";
  }
  if (
    code === "invalid_subscription_plan" ||
    code === "paid_subscription_plan_required" ||
    status === 422
  ) {
    return "selection";
  }
  if (code === "billing_provider_unavailable") return "provider";
  if (
    code === "billing_not_configured" ||
    code === "billing_database_required" ||
    code === "subscription_configuration_error" ||
    code === "invalid_billing_redirect"
  ) {
    return "configuration";
  }
  if (status >= 500) return "provider";
  return "unknown";
}

function isRetryable(code: string, status: number): boolean {
  if (
    code === "billing_not_configured" ||
    code === "billing_database_required" ||
    code === "subscription_configuration_error"
  ) {
    return false;
  }
  return code === "billing_provider_unavailable" || status === 429 || status >= 500;
}
