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
  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiClientError";
  }
}

function detailMessage(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (typeof body !== "object" || body === null) return fallback;

  const record = body as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null) {
    const detailRecord = detail as Record<string, unknown>;
    if (typeof detailRecord.message === "string") return detailRecord.message;
    if (typeof detailRecord.code === "string") return detailRecord.code;
  }
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
    throw new ApiClientError(res.status, detailMessage(body, res.statusText), body);
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
    throw new ApiClientError(res.status, detailMessage(errBody, res.statusText), errBody);
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
