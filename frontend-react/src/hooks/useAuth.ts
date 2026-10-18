import { useState, useEffect, useCallback } from "react";
import { fetchCognitoConfig, fetchWhoAmI, logoutSession } from "../api/auth";
import { currentNextPath, loginPageUrl } from "../auth/redirect";
import { markFreshLoginPending } from "../session/activeSession";
import { getRuntimeConfig } from "../config/runtimeConfig";
import type { CognitoConfig, WhoAmIResponse } from "../types";

interface AuthState {
  cognitoConfig: CognitoConfig | null;
  whoAmI: WhoAmIResponse | null;
  loading: boolean;
  loggedIn: boolean;
}

const isLocalhost = () =>
  ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

async function ensureDevSession(cognitoEnabled: boolean): Promise<void> {
  const cfg = getRuntimeConfig();
  if (cognitoEnabled || !cfg.enableDevSessionLogin || !isLocalhost()) return;

  // Already have a valid session — nothing to do.
  const me = await fetch("/v1/whoami", { credentials: "include" }).catch(() => null);
  if (me?.ok) return;
  if (me && me.status !== 401 && me.status !== 403) return;

  const headers: Record<string, string> = {};
  if (cfg.devSessionLoginToken) headers["X-Dev-Login-Token"] = cfg.devSessionLoginToken;
  await fetch("/v1/auth/dev-login", { method: "POST", credentials: "include", headers }).catch(
    () => null,
  );
}

export function useAuth() {
  const [state, setState] = useState<AuthState>({
    cognitoConfig: null,
    whoAmI: null,
    loading: true,
    loggedIn: false,
  });

  const refresh = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true }));
    try {
      const config = await fetchCognitoConfig().catch(() => null);
      await ensureDevSession(!!config?.enabled);
      const whoAmI = await fetchWhoAmI().catch(() => null);
      setState({
        cognitoConfig: config,
        whoAmI,
        loading: false,
        loggedIn: !!whoAmI?.user_id,
      });
    } catch {
      setState((prev) => ({ ...prev, loading: false }));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(() => {
    if (!state.cognitoConfig?.enabled) return;
    markFreshLoginPending();
    window.location.assign(loginPageUrl(currentNextPath(window.location)));
  }, [state.cognitoConfig]);

  const logout = useCallback(async () => {
    const { cognitoConfig } = state;
    let cognitoLogoutUrl: string | null = null;
    try {
      cognitoLogoutUrl = (await logoutSession()).cognito_logout_url;
    } catch {
      // The session cookie may already be gone; continue clearing local state.
    } finally {
      markFreshLoginPending();
      setState((prev) => ({ ...prev, whoAmI: null, loggedIn: false }));
    }
    if (!cognitoConfig?.enabled) return;
    // A full navigation also drops all in-memory chat state from the page.
    window.location.assign(cognitoLogoutUrl ?? "/login");
  }, [state]);

  return {
    ...state,
    login,
    logout,
    refresh,
  };
}
