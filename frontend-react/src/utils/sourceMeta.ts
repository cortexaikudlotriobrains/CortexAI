import type { WebSourceItem } from "../types";

export function normalizeSourceUrl(value: string): string | null {
  if ([...value].some((character) => character.charCodeAt(0) <= 0x20 || character === "\\"))
    return null;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    url.hash = "";
    return url.href;
  } catch {
    return null;
  }
}

const COMMON_PUBLISHERS: Record<string, string> = {
  "aljazeera.com": "Al Jazeera",
  "bbc.co.uk": "BBC",
  "bbc.com": "BBC",
  "cnn.com": "CNN",
  "greenwichtime.com": "Greenwich Time",
  "npr.org": "NPR",
  "nytimes.com": "The New York Times",
  "reuters.com": "Reuters",
  "theguardian.com": "The Guardian",
  "wikipedia.org": "Wikipedia",
};

export function publisherName(source: WebSourceItem): string {
  const suffix = publisherFromTitleSuffix(source.title);
  if (suffix) return suffix;

  const host = sourceHost(source);
  if (!host) return source.url.trim() || "Source";

  const mapped = COMMON_PUBLISHERS[host] ?? COMMON_PUBLISHERS[stripSubdomains(host)];
  if (mapped) return mapped;

  return titleCaseHost(stripTld(stripSubdomains(host))) || "Source";
}

export function faviconUrl(source: WebSourceItem): string {
  const host = sourceHost(source);
  const fallbackHost = host || "source.invalid";
  return `https://icons.duckduckgo.com/ip3/${fallbackHost}.ico`;
}

function publisherFromTitleSuffix(title: string): string | null {
  const segments = title
    .split(/\s(?:\||-|\u2013|\u2014)\s/g)
    .map((segment) => segment.trim())
    .filter(Boolean);

  if (segments.length < 2) return null;

  const suffix = segments[segments.length - 1];
  return suffix.length >= 2 && suffix.length <= 80 ? suffix : null;
}

function sourceHost(source: WebSourceItem): string | null {
  const rawUrl = source.url.trim();
  if (!rawUrl) return null;

  try {
    const parsed = new URL(rawUrl);
    return normalizeHost(parsed.hostname);
  } catch {
    try {
      const parsed = new URL(`https://${rawUrl}`);
      return normalizeHost(parsed.hostname);
    } catch {
      return null;
    }
  }
}

function normalizeHost(hostname: string): string {
  return hostname.toLowerCase().replace(/^www\./, "");
}

function stripSubdomains(hostname: string): string {
  const parts = hostname.split(".").filter(Boolean);
  if (parts.length <= 2) return hostname;

  const lastTwo = parts.slice(-2).join(".");
  if (lastTwo === "co.uk" && parts.length >= 3) {
    return parts.slice(-3).join(".");
  }

  return parts.slice(-2).join(".");
}

function stripTld(hostname: string): string {
  const parts = hostname.split(".").filter(Boolean);
  if (parts.length === 0) return hostname;
  if (parts.length >= 3 && parts.slice(-2).join(".") === "co.uk") {
    return parts.slice(0, -2).join(" ");
  }
  return parts.slice(0, -1).join(" ");
}

function titleCaseHost(value: string): string {
  return value
    .replace(/[-_]+/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}
