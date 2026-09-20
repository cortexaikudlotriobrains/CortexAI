import { get } from "./client";
import type { AuthenticatedUser, CognitoConfig, WhoAmIResponse } from "../types";

export async function fetchCognitoConfig(): Promise<CognitoConfig> {
  return get<CognitoConfig>("/v1/auth/cognito-config");
}

export async function fetchWhoAmI(): Promise<WhoAmIResponse> {
  return get<WhoAmIResponse>("/v1/whoami");
}

export async function fetchAuthenticatedUser(): Promise<AuthenticatedUser> {
  return get<AuthenticatedUser>("/v1/auth/me");
}

export async function devLogin(token?: string): Promise<{ session_id: string }> {
  const headers: Record<string, string> = {};
  if (token) headers["X-Dev-Login-Token"] = token;
  const res = await fetch("/v1/auth/dev-login", {
    method: "POST",
    credentials: "include",
    headers,
  });
  if (!res.ok) throw new Error(res.statusText || "Dev login failed");
  return res.json() as Promise<{ session_id: string }>;
}

export function cognitoClientId(config: CognitoConfig): string {
  return (config.client_id ?? config.clientId ?? "").trim();
}

export function cognitoRedirectUri(config: CognitoConfig): string {
  return (config.redirect_uri ?? config.redirectUri ?? "").trim();
}

function defaultCognitoRedirectUri(): string {
  return `${window.location.origin}/auth`;
}

export function cognitoLogoutUrl(config: CognitoConfig): string {
  return (config.logout_url ?? config.logoutUrl ?? "").trim();
}

export function buildCognitoLoginUrl(config: CognitoConfig): string {
  const clientId = cognitoClientId(config);
  const redirectUri = cognitoRedirectUri(config) || defaultCognitoRedirectUri();
  if (!config.enabled || !config.domain || !clientId || !redirectUri) {
    throw new Error("Cognito is not fully configured");
  }
  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: "openid email profile",
  });
  return `${config.domain}/oauth2/authorize?${params.toString()}`;
}

export function buildCognitoLogoutUrl(config: CognitoConfig): string {
  const logoutUrl = cognitoLogoutUrl(config);
  if (!config.enabled || !logoutUrl) {
    throw new Error("Cognito logout is not configured");
  }
  const redirectUri = cognitoRedirectUri(config) || defaultCognitoRedirectUri();
  const separator = logoutUrl.includes("?") ? "&" : "?";
  return `${logoutUrl}${separator}response_type=code&scope=email+openid&redirect_uri=${encodeURIComponent(
    redirectUri,
  )}`;
}
