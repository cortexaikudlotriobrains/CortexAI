import { ApiClientError } from "../api/client";
import type { ApiError } from "../types";

export type ErrorContext =
  | "chat"
  | "history_load"
  | "history_delete"
  | "history_rename"
  | "history_clear"
  | "usage_load"
  | "usage_export"
  | "credits_load"
  | "models_load"
  | "work_load"
  | "work_stream"
  | "work_start"
  | "work_cancel"
  | "work_approval"
  | "work_connect"
  | "work_upload"
  | "subscription";

export type UserErrorAction =
  | "retry"
  | "sign_in"
  | "view_plans"
  | "switch_model"
  | "remove_file"
  | "dismiss";

export interface UserFacingError {
  code: string;
  title: string;
  message: string;
  retryable: boolean;
  action: UserErrorAction;
  actionLabel?: string;
  requestId?: string;
  context?: ErrorContext;
}

interface ErrorFacts {
  code: string;
  status: number | null;
  retryable: boolean | null;
  requestId?: string;
  details: Record<string, unknown>;
}

const CONTEXT_DEFAULTS: Record<ErrorContext, Pick<UserFacingError, "title" | "message">> = {
  chat: {
    title: "Your request couldn't be completed",
    message: "Try again. If the problem continues, switch models.",
  },
  history_load: {
    title: "This chat couldn't load",
    message: "Your other chats are still available. Try opening this chat again.",
  },
  history_delete: {
    title: "This chat wasn't deleted",
    message: "Nothing was removed. Please try again.",
  },
  history_rename: {
    title: "This chat wasn't renamed",
    message: "Your previous title was kept. Please try again.",
  },
  history_clear: {
    title: "History wasn't cleared",
    message: "Your chats are still available. Please try again.",
  },
  usage_load: {
    title: "Usage data couldn't load",
    message: "Your usage data is safe. Please try again.",
  },
  usage_export: {
    title: "Usage couldn't be exported",
    message: "No file was downloaded. Please try again.",
  },
  credits_load: {
    title: "Credit activity couldn't load",
    message: "Your balance and charges are unchanged. Please try again.",
  },
  models_load: {
    title: "Model options couldn't refresh",
    message: "The default model list is available while you try again.",
  },
  work_load: {
    title: "This Work session couldn't load",
    message: "Your saved work is still available. Please try again.",
  },
  work_stream: {
    title: "The live Work connection was interrupted",
    message: "The run may still be continuing. Reopen this Work session to reconnect.",
  },
  work_start: {
    title: "Work couldn't start",
    message: "Your instructions were kept. Review the settings and try again.",
  },
  work_cancel: {
    title: "Work couldn't be stopped",
    message: "The run may still be active. Please try stopping it again.",
  },
  work_approval: {
    title: "That approval wasn't recorded",
    message: "No action was taken. Refresh the approval and try again.",
  },
  work_connect: {
    title: "The tool couldn't connect",
    message: "Check the connection settings and try again.",
  },
  work_upload: {
    title: "The file couldn't be added",
    message: "Check the file and try the upload again.",
  },
  subscription: {
    title: "Plan information couldn't load",
    message: "Your current access has not changed. Please try again.",
  },
};

export function presentError(error: unknown, context: ErrorContext): UserFacingError {
  if (isUserFacingError(error)) return error;
  if (isAbortError(error)) {
    return userFacingMessage("Request cancelled", "Nothing was changed.", {
      code: "request_cancelled",
      action: "dismiss",
      context,
    });
  }
  if (error instanceof TypeError) {
    return userFacingMessage(
      "CortexAI couldn't be reached",
      "Check your connection and try again.",
      {
        code: "network_error",
        retryable: true,
        action: "retry",
        actionLabel: "Try again",
        context,
      },
    );
  }

  const facts = errorFacts(error);
  const mapped = mapKnownError(facts, context);
  if (mapped) return { ...mapped, context };

  const fallback = CONTEXT_DEFAULTS[context];
  const retryable = facts.retryable ?? retryableStatus(facts.status);
  return userFacingMessage(fallback.title, fallback.message, {
    code: facts.code || "request_failed",
    retryable,
    action: retryable ? "retry" : "dismiss",
    actionLabel: retryable ? "Try again" : undefined,
    requestId: facts.requestId,
    context,
  });
}

export function presentResponseError(error: ApiError): UserFacingError {
  const details = error.details ?? {};
  const kind = stringField(details.kind);
  if (kind === "transient_capacity") {
    return userFacingMessage(
      "This model is temporarily busy",
      "Try again shortly or switch to another model.",
      { code: error.code, retryable: true, action: "retry", actionLabel: "Retry model" },
    );
  }
  if (kind === "timeout" || error.code === "timeout") {
    return userFacingMessage(
      "The model took too long to respond",
      "Try again or choose another model.",
      { code: error.code, retryable: true, action: "retry", actionLabel: "Retry model" },
    );
  }
  if (kind === "rate_limited" || ["rate_limit", "rate_limited"].includes(error.code)) {
    return userFacingMessage(
      "This model is receiving too many requests",
      "Try again shortly or switch to another model.",
      { code: error.code, retryable: true, action: "retry", actionLabel: "Retry model" },
    );
  }
  if (kind === "auth" || kind === "quota_exceeded" || error.code === "auth") {
    return userFacingMessage(
      "This model is unavailable",
      "Try another model. If this keeps happening, contact support.",
      { code: error.code, action: "switch_model", actionLabel: "Switch model" },
    );
  }
  if (/empty response/i.test(error.message)) {
    return userFacingMessage(
      "The model didn't return an answer",
      "Retry the request or choose another model.",
      { code: error.code, retryable: true, action: "retry", actionLabel: "Retry model" },
    );
  }
  if (/content (?:was )?filtered|content filter/i.test(error.message)) {
    return userFacingMessage(
      "The model couldn't answer this request",
      "Revise the request and try again.",
      { code: error.code, action: "dismiss" },
    );
  }
  return userFacingMessage(
    "The model couldn't complete this request",
    error.retryable
      ? "Try again or choose another model."
      : "Revise the request or choose another model.",
    {
      code: error.code || "model_error",
      retryable: error.retryable,
      action: error.retryable ? "retry" : "switch_model",
      actionLabel: error.retryable ? "Retry model" : "Switch model",
    },
  );
}

export function userFacingMessage(
  title: string,
  message: string,
  options: Partial<Omit<UserFacingError, "title" | "message">> = {},
): UserFacingError {
  return {
    code: options.code ?? "client_message",
    title,
    message,
    retryable: options.retryable ?? false,
    action: options.action ?? "dismiss",
    actionLabel: options.actionLabel,
    requestId: options.requestId,
    context: options.context,
  };
}

export function isUserFacingError(value: unknown): value is UserFacingError {
  if (!isRecord(value)) return false;
  return (
    typeof value.code === "string" &&
    typeof value.title === "string" &&
    typeof value.message === "string" &&
    typeof value.retryable === "boolean" &&
    typeof value.action === "string"
  );
}

function mapKnownError(facts: ErrorFacts, context: ErrorContext): UserFacingError | null {
  const code = facts.code;
  const requestId = facts.requestId;
  const withRequest = (error: UserFacingError) => ({ ...error, requestId });

  if (["session_auth_required", "billing_authentication_required"].includes(code) || facts.status === 401) {
    return withRequest(
      userFacingMessage("Your session has expired", "Sign in again to continue.", {
        code: code || "authentication_required",
        action: "sign_in",
        actionLabel: "Sign in",
      }),
    );
  }
  if (code === "rate_limited" || facts.status === 429) {
    const retryAfter = finiteNumber(facts.details.retry_after_seconds);
    return withRequest(
      userFacingMessage(
        "You're sending requests too quickly",
        retryAfter ? `Try again in ${retryAfter} seconds.` : "Wait a moment, then try again.",
        { code: code || "rate_limited", retryable: true, action: "retry", actionLabel: "Try again" },
      ),
    );
  }
  if (["monthly_allowance_exhausted", "insufficient_credits"].includes(code)) {
    return withRequest(
      userFacingMessage("More AI credits are needed", "View your plan and credit balance to continue.", {
        code,
        action: "view_plans",
        actionLabel: "View plans",
      }),
    );
  }
  if (code === "attachments_require_db") {
    return withRequest(
      userFacingMessage(
        "File analysis is unavailable",
        "Remove the file to continue, or try again later.",
        { code, action: "remove_file", actionLabel: "Remove file" },
      ),
    );
  }
  if (["no_attachment_compatible_provider", "attachment_model_incompatible"].includes(code)) {
    return withRequest(
      userFacingMessage(
        "This model can't use the attached file",
        "Switch models or remove the file.",
        { code, action: "switch_model", actionLabel: "Switch model" },
      ),
    );
  }
  if (["history_session_not_found", "history_entry_not_found"].includes(code) || (context === "history_load" && facts.status === 404)) {
    return withRequest(
      userFacingMessage("This chat is no longer available", "It may have been deleted in another tab.", {
        code: code || "history_not_found",
        action: "dismiss",
      }),
    );
  }
  if (["work_session_not_found", "work_run_not_found"].includes(code)) {
    return withRequest(
      userFacingMessage("This Work session is no longer available", "Return to Work and choose another session.", {
        code,
        action: "dismiss",
      }),
    );
  }
  if (["work_not_in_plan", "custom_mcp_not_in_plan", "verified_connectors_not_in_plan"].includes(code)) {
    return withRequest(
      userFacingMessage("Your plan doesn't include this feature", "View plans to see the available options.", {
        code,
        action: "view_plans",
        actionLabel: "View plans",
      }),
    );
  }
  if (["work_provider_not_configured", "connector_configuration_required", "connector_configuration_invalid"].includes(code)) {
    return withRequest(
      userFacingMessage("This Work feature isn't available right now", "Ask and Compare remain available.", {
        code,
        action: "dismiss",
      }),
    );
  }
  if (["invalid_mcp_url", "mcp_connection_failed", "oauth_token_exchange_failed"].includes(code)) {
    return withRequest(
      userFacingMessage("The tool couldn't connect", "Check the server address and connection settings, then try again.", {
        code,
        retryable: true,
        action: "retry",
        actionLabel: "Try again",
      }),
    );
  }
  if (code === "tool_connection_limit") {
    return withRequest(
      userFacingMessage("Your tool connection limit is reached", "Remove an unused connection before adding another.", {
        code,
        action: "dismiss",
      }),
    );
  }
  if (facts.status === 408 || facts.status === 504 || code === "timeout") {
    return withRequest(
      userFacingMessage("The request took too long", "Try again. If it keeps happening, choose another model.", {
        code: code || "timeout",
        retryable: true,
        action: "retry",
        actionLabel: "Try again",
      }),
    );
  }
  if (["internal_error", "stream_error", "work_stream_unavailable"].includes(code) || (facts.status !== null && facts.status >= 500)) {
    const fallback = CONTEXT_DEFAULTS[context];
    return withRequest(
      userFacingMessage(fallback.title, fallback.message, {
        code: code || "service_unavailable",
        retryable: true,
        action: "retry",
        actionLabel: "Try again",
      }),
    );
  }
  return null;
}

function errorFacts(error: unknown): ErrorFacts {
  if (error instanceof ApiClientError) {
    return {
      code: error.code ?? "",
      status: error.status || null,
      retryable: error.retryable,
      requestId: error.requestId ?? undefined,
      details: error.details,
    };
  }
  return {
    code: "",
    status: null,
    retryable: null,
    details: {},
  };
}

function retryableStatus(status: number | null): boolean {
  return status === null || status === 408 || status === 429 || status >= 500;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim().toLowerCase() : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
