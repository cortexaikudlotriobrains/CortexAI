const getApiKey = (): string | null => {
  const w = window as unknown as { __CORTEX_API_KEY?: string };
  return w.__CORTEX_API_KEY ?? null;
};

export function makeRequestId(prefix = "react-ui"): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

export function buildHeaders(extra?: Record<string, string | undefined>): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  for (const [key, value] of Object.entries(extra ?? {})) {
    if (value !== undefined && value !== "") headers[key] = value;
  }

  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  if (!headers["X-Request-ID"]) headers["X-Request-ID"] = makeRequestId();
  return headers;
}

export class ApiClientError extends Error {
  readonly code: string | null;
  readonly retryable: boolean | null;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
    metadata: {
      code?: string | null;
      retryable?: boolean | null;
      details?: Record<string, unknown>;
      requestId?: string | null;
    } = {},
  ) {
    super(message);
    this.name = "ApiClientError";
    const detail = structuredErrorDetail(body);
    this.code = metadata.code ?? detail.code;
    this.retryable = metadata.retryable ?? detail.retryable;
    this.details = metadata.details ?? detail.fields;
    this.requestId = metadata.requestId ?? detail.requestId;
  }
}

interface StructuredErrorDetail {
  code: string | null;
  message: string | null;
  retryable: boolean | null;
  requestId: string | null;
  fields: Record<string, unknown>;
}

export function structuredErrorDetail(body: unknown): StructuredErrorDetail {
  if (!isRecord(body)) return emptyStructuredErrorDetail();
  const candidate = isRecord(body.detail) ? body.detail : body;
  const fields = { ...candidate };
  const code = stringValue(candidate.code);
  const message = stringValue(candidate.message);
  const retryable = typeof candidate.retryable === "boolean" ? candidate.retryable : null;
  const requestId = stringValue(candidate.request_id) ?? stringValue(candidate.requestId);
  delete fields.code;
  delete fields.message;
  delete fields.retryable;
  delete fields.request_id;
  delete fields.requestId;
  return { code, message, retryable, requestId, fields };
}

function detailMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (typeof body !== "object" || body === null) return fallback;

  const record = body as Record<string, unknown>;
  if (typeof record.detail === "string") return record.detail;
  const detail = structuredErrorDetail(body);
  if (detail.message) return detail.message;
  if (detail.code) return detail.code;
  if (typeof record.message === "string") return record.message;
  return fallback;
}

async function parseErrorBody(res: Response): Promise<unknown> {
  const text = await res.text().catch(() => "");
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await parseErrorBody(res);
    throw apiClientError(res, body);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "GET",
    credentials: "include",
    headers: buildHeaders(),
    signal,
  });
  return handleResponse<T>(res);
}

export async function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders(),
    body: JSON.stringify(body),
    signal,
  });
  return handleResponse<T>(res);
}

export async function postWithHeaders<T>(
  path: string,
  body: unknown,
  headers: Record<string, string | undefined>,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders(headers),
    body: JSON.stringify(body),
    signal,
  });
  return handleResponse<T>(res);
}

export async function patch<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "PATCH",
    credentials: "include",
    headers: buildHeaders(),
    body: JSON.stringify(body),
    signal,
  });
  return handleResponse<T>(res);
}

export async function del<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: "DELETE",
    credentials: "include",
    headers: buildHeaders(),
    signal,
  });
  return handleResponse<T>(res);
}

export async function* streamPost(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<string> {
  const res = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: buildHeaders(),
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    const errBody = await parseErrorBody(res);
    throw apiClientError(res, errBody);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      yield line;
    }
  }

  const finalText = decoder.decode();
  if (finalText) buffer += finalText;
  if (buffer) yield buffer;
}

export async function apiClientErrorFromResponse(
  response: Response,
  fallbackMessage?: string,
): Promise<ApiClientError> {
  const body = await parseErrorBody(response);
  return apiClientError(response, body, fallbackMessage);
}

export function apiClientErrorFromStreamEvent(
  event: Record<string, unknown>,
  fallbackMessage: string,
): ApiClientError {
  const body = { detail: event };
  const detail = structuredErrorDetail(body);
  return new ApiClientError(0, detail.message ?? fallbackMessage, body, {
    code: detail.code ?? "stream_error",
    retryable: detail.retryable ?? true,
    details: detail.fields,
    requestId: detail.requestId,
  });
}

function apiClientError(
  response: Response,
  body: unknown,
  fallbackMessage?: string,
): ApiClientError {
  return new ApiClientError(
    response.status,
    detailMessage(body, fallbackMessage || response.statusText || "Request failed"),
    body,
    { requestId: response.headers.get("X-Request-ID") },
  );
}

function emptyStructuredErrorDetail(): StructuredErrorDetail {
  return { code: null, message: null, retryable: null, requestId: null, fields: {} };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}
