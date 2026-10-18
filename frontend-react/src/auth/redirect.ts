const BLOCKED_PREFIXES = ["/login", "/auth", "/v1/"];

/** Same-origin absolute path or "/". Mirrors the backend's `sanitize_next_path`. */
export function safeNextPath(value: string | null | undefined): string {
  const candidate = (value ?? "").trim();
  if (!candidate || candidate.length > 512) return "/";
  if (!candidate.startsWith("/") || candidate.startsWith("//") || candidate.startsWith("/\\")) {
    return "/";
  }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f\\]/.test(candidate)) return "/";
  const path = candidate.split(/[?#]/, 1)[0] ?? "";
  if (BLOCKED_PREFIXES.some((prefix) => path.startsWith(prefix))) return "/";
  return candidate;
}

/** Where to send the user back to after sign-in, without transient auth params. */
export function currentNextPath(location: Pick<Location, "pathname" | "search">): string {
  const params = new URLSearchParams(location.search);
  params.delete("fresh_login");
  const query = params.toString();
  return safeNextPath(`${location.pathname}${query ? `?${query}` : ""}`);
}

export function loginPageUrl(nextPath: string): string {
  const next = safeNextPath(nextPath);
  return next === "/" ? "/login" : `/login?next=${encodeURIComponent(next)}`;
}
