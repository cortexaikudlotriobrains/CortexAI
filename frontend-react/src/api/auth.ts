import { del, get, post } from "./client";
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

export interface PasswordLoginResponse {
  user_id: string;
  email: string | null;
}

export interface LogoutResponse {
  ok: boolean;
  cognito_logout_url: string | null;
}

export function signUp(email: string, password: string): Promise<{ status: string }> {
  return post("/v1/auth/signup", { email, password });
}

export function confirmSignUp(email: string, code: string): Promise<{ status: string }> {
  return post("/v1/auth/signup/confirm", { email, code });
}

export function resendSignUpCode(email: string): Promise<{ status: string }> {
  return post("/v1/auth/signup/resend", { email });
}

export function loginWithPassword(email: string, password: string): Promise<PasswordLoginResponse> {
  return post("/v1/auth/login/password", { email, password });
}

export function requestPasswordReset(email: string): Promise<{ status: string }> {
  return post("/v1/auth/password/forgot", { email });
}

export function resetPassword(
  email: string,
  code: string,
  newPassword: string,
): Promise<{ status: string }> {
  return post("/v1/auth/password/reset", { email, code, new_password: newPassword });
}

export function logoutSession(): Promise<LogoutResponse> {
  return post("/v1/auth/logout", {});
}

/** Same-origin URL that starts Google sign-in (the backend redirects to Google via Cognito). */
export function googleLoginUrl(nextPath: string): string {
  return `/v1/auth/google/start?next=${encodeURIComponent(nextPath)}`;
}

export interface AuthSession {
  id: string;
  auth_method: "password" | "google" | "cognito_token" | "dev";
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  user_agent: string | null;
  current: boolean;
}

export function listSessions(): Promise<{ sessions: AuthSession[] }> {
  return get("/v1/auth/sessions");
}

export function revokeSession(sessionId: string): Promise<{ ok: boolean }> {
  return del(`/v1/auth/sessions/${encodeURIComponent(sessionId)}`);
}

export function revokeOtherSessions(): Promise<{ ok: boolean; revoked: number }> {
  return post("/v1/auth/sessions/revoke-others", {});
}
