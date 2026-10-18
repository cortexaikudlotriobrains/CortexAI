import { useCallback, useEffect, useState } from "react";
import { listSessions, revokeOtherSessions, revokeSession, type AuthSession } from "../api/auth";
import { AUTH_METHOD_LABEL, deviceLabel } from "../auth/deviceLabel";
import { SubscriptionPageShell } from "../components/subscription/SubscriptionPageShell";
import { useAuth } from "../hooks/useAuth";
import styles from "./SecurityPage.module.css";

type LoadState = "loading" | "ready" | "error";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}

export function SecurityPage() {
  const { cognitoConfig, loading: authLoading, loggedIn, login, logout } = useAuth();
  const authEnabled = cognitoConfig?.enabled ?? false;

  return (
    <SubscriptionPageShell
      title="Signed-in devices"
      subtitle="Where your account is signed in right now"
      authLoading={authLoading}
      authEnabled={authEnabled}
      loggedIn={loggedIn}
      onLogin={login}
      onLogout={logout}
    >
      {authLoading ? (
        <p className={styles.muted} role="status">
          Loading…
        </p>
      ) : loggedIn ? (
        <SessionList />
      ) : (
        <section className={styles.panel}>
          <p>Sign in to see where your account is signed in.</p>
          {authEnabled ? (
            <button type="button" className={styles.primary} onClick={login}>
              Sign in
            </button>
          ) : null}
        </section>
      )}
    </SubscriptionPageShell>
  );
}

export function SessionList() {
  const [sessions, setSessions] = useState<AuthSession[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    try {
      const { sessions: rows } = await listSessions();
      setSessions(rows);
      setState("ready");
    } catch {
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const others = sessions.filter((session) => !session.current);

  const signOut = async (session: AuthSession) => {
    setBusy(session.id);
    setError(null);
    setMessage(null);
    try {
      await revokeSession(session.id);
      setMessage(`Signed out ${deviceLabel(session.user_agent)}.`);
      await load();
    } catch {
      setError("That device couldn't be signed out. Try again.");
    } finally {
      setBusy(null);
    }
  };

  const signOutOthers = async () => {
    setBusy("others");
    setError(null);
    setMessage(null);
    try {
      const { revoked } = await revokeOtherSessions();
      setMessage(
        revoked === 1 ? "Signed out 1 other device." : `Signed out ${revoked} other devices.`,
      );
      await load();
    } catch {
      setError("Other devices couldn't be signed out. Try again.");
    } finally {
      setBusy(null);
    }
  };

  if (state === "loading" && sessions.length === 0) {
    return (
      <p className={styles.muted} role="status">
        Loading devices…
      </p>
    );
  }

  if (state === "error") {
    return (
      <section className={styles.panel}>
        <p role="alert">Your devices couldn't be loaded.</p>
        <button type="button" className={styles.secondary} onClick={() => void load()}>
          Try again
        </button>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-labelledby="devices-title">
      <div className={styles.header}>
        <h2 id="devices-title">Active sessions</h2>
        <button
          type="button"
          className={styles.secondary}
          disabled={others.length === 0 || busy !== null}
          onClick={() => void signOutOthers()}
        >
          {busy === "others" ? "Signing out…" : "Sign out of all other devices"}
        </button>
      </div>
      {message ? (
        <p className={styles.notice} role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}
      <ul className={styles.list}>
        {sessions.map((session) => (
          <li key={session.id} className={styles.row}>
            <div className={styles.rowCopy}>
              <p className={styles.device}>
                {deviceLabel(session.user_agent)}
                {session.current ? <span className={styles.badge}>This device</span> : null}
              </p>
              <p className={styles.meta}>
                {AUTH_METHOD_LABEL[session.auth_method] ?? "Signed in"} · Last active{" "}
                {formatDate(session.last_seen_at)} · Signed in {formatDate(session.created_at)}
              </p>
            </div>
            {session.current ? null : (
              <button
                type="button"
                className={styles.secondary}
                disabled={busy !== null}
                onClick={() => void signOut(session)}
                aria-label={`Sign out ${deviceLabel(session.user_agent)}, last active ${formatDate(session.last_seen_at)}`}
              >
                {busy === session.id ? "Signing out…" : "Sign out"}
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className={styles.muted}>
        Sessions also end on their own after a while, and sooner on devices you stop using.
        Resetting your password signs out every device.
      </p>
    </section>
  );
}
