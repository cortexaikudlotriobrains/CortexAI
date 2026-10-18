import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  confirmSignUp,
  googleLoginUrl,
  loginWithPassword,
  requestPasswordReset,
  resendSignUpCode,
  resetPassword,
  signUp,
} from "../api/auth";
import { ApiClientError } from "../api/client";
import { loginFailure, oauthRedirectMessage } from "../auth/loginErrors";
import { checkPassword, isValidEmail, passwordMeetsPolicy } from "../auth/passwordPolicy";
import { safeNextPath } from "../auth/redirect";
import { CortexIcon } from "../components/shared/CortexIcon";
import { useAuth } from "../hooks/useAuth";
import { useTheme } from "../hooks/useTheme";
import { markFreshLoginPending } from "../session/activeSession";
import styles from "./LoginPage.module.css";

type View = "signin" | "signup" | "verify" | "forgot" | "reset";
type Field = "email" | "password" | "code";

const RESEND_COOLDOWN_SECONDS = 30;

const VIEW_COPY: Record<View, { title: string; lede: string; submit: string; busy: string }> = {
  signin: {
    title: "Sign in to CortexAI",
    lede: "Welcome back. Pick up your chats, comparisons, and files.",
    submit: "Sign in",
    busy: "Signing in…",
  },
  signup: {
    title: "Create your account",
    lede: "One account for Ask, Compare, and Work.",
    submit: "Create account",
    busy: "Creating account…",
  },
  verify: {
    title: "Check your email",
    lede: "Enter the 6-digit code to verify your email address.",
    submit: "Verify email",
    busy: "Verifying…",
  },
  forgot: {
    title: "Reset your password",
    lede: "Enter your email and we'll send you a code to set a new password.",
    submit: "Send reset code",
    busy: "Sending code…",
  },
  reset: {
    title: "Choose a new password",
    lede: "Enter the 6-digit code from your email and a new password.",
    submit: "Update password",
    busy: "Updating…",
  },
};

export function LoginPage() {
  useTheme();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { cognitoConfig, loading, loggedIn, logout } = useAuth();
  const nextPath = safeNextPath(searchParams.get("next"));

  const [view, setView] = useState<View>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(() =>
    oauthRedirectMessage(searchParams.get("error")),
  );
  const [errorField, setErrorField] = useState<Field | null>(null);
  const [logoutFailed] = useState(() => searchParams.get("error") === "logout_failed");
  const [notice, setNotice] = useState<string | null>(null);
  const [cooldown, setCooldown] = useState(0);

  const submitting = useRef(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const fieldRefs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});
  const ids = {
    email: useId(),
    password: useId(),
    code: useId(),
    error: useId(),
    rules: useId(),
  };

  const passwordEnabled = cognitoConfig?.passwordLoginEnabled !== false;
  const signupEnabled = passwordEnabled && cognitoConfig?.signupEnabled !== false;
  const googleEnabled = cognitoConfig?.googleLoginEnabled !== false;

  // Drop one-shot error params from the address bar but keep `next`.
  useEffect(() => {
    if (!searchParams.has("error")) return;
    const params = new URLSearchParams(searchParams);
    params.delete("error");
    setSearchParams(params, { replace: true });
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    // After a failed sign-out the session may still be live; stay here to retry.
    if (loggedIn && !logoutFailed) navigate(nextPath, { replace: true });
  }, [loggedIn, logoutFailed, navigate, nextPath]);

  useEffect(() => {
    if (cooldown <= 0) return undefined;
    const timer = window.setTimeout(() => setCooldown((value) => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  useEffect(() => {
    document.title = `${VIEW_COPY[view].title} · CortexAI`;
    headingRef.current?.focus();
  }, [view]);

  useEffect(() => {
    if (errorField) fieldRefs.current[errorField]?.focus();
  }, [errorField, error]);

  const goTo = useCallback((next: View, options: { keepCredentials?: boolean } = {}) => {
    setView(next);
    setError(null);
    setErrorField(null);
    setNotice(null);
    setShowPassword(false);
    setCode("");
    if (!options.keepCredentials) setPassword("");
  }, []);

  const fail = useCallback((message: string, field: Field | null = null) => {
    setError(message);
    setErrorField(field);
  }, []);

  const completeSignIn = useCallback(() => {
    setPassword("");
    markFreshLoginPending();
    navigate(nextPath, { replace: true });
  }, [navigate, nextPath]);

  const signInWith = useCallback(
    async (address: string, secret: string) => {
      try {
        await loginWithPassword(address, secret);
        completeSignIn();
        return true;
      } catch (err) {
        const failure = loginFailure(err);
        if (failure.code === "email_not_verified") {
          // The account exists but was never verified: send a fresh code.
          void resendSignUpCode(address).catch(() => undefined);
          goTo("verify", { keepCredentials: true });
          setNotice("Your email isn't verified yet. We sent you a new code.");
          setCooldown(RESEND_COOLDOWN_SECONDS);
          return false;
        }
        if (failure.code === "password_reset_required" || failure.code === "challenge_unsupported") {
          goTo("forgot");
          setNotice(failure.message);
          return false;
        }
        fail(failure.message, failure.code === "invalid_credentials" ? "password" : null);
        return false;
      }
    },
    [completeSignIn, fail, goTo],
  );

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting.current) return;
    const address = email.trim().toLowerCase();

    if (!isValidEmail(address)) {
      fail("Enter a valid email address.", "email");
      return;
    }
    if (view !== "forgot" && view !== "verify" && !password) {
      fail(view === "signin" ? "Enter your password." : "Choose a password.", "password");
      return;
    }
    if ((view === "signup" || view === "reset") && !passwordMeetsPolicy(password, address)) {
      fail("Choose a stronger password that meets the requirements below.", "password");
      return;
    }
    if ((view === "verify" || view === "reset") && !/^\d{6}$/.test(code.trim())) {
      fail("Enter the 6-digit code from your email.", "code");
      return;
    }

    submitting.current = true;
    setPending(true);
    setError(null);
    setErrorField(null);
    setNotice(null);
    try {
      if (view === "signin") {
        await signInWith(address, password);
      } else if (view === "signup") {
        await signUp(address, password);
        goTo("verify", { keepCredentials: true });
        setNotice(`We sent a 6-digit code to ${address}.`);
        setCooldown(RESEND_COOLDOWN_SECONDS);
      } else if (view === "verify") {
        await confirmSignUp(address, code.trim());
        if (password) {
          await signInWith(address, password);
        } else {
          goTo("signin");
          setNotice("Email verified. Sign in to continue.");
        }
      } else if (view === "forgot") {
        await requestPasswordReset(address);
        goTo("reset");
        setNotice(`If an account exists for ${address}, we've sent a 6-digit code.`);
        setCooldown(RESEND_COOLDOWN_SECONDS);
      } else {
        await resetPassword(address, code.trim(), password);
        goTo("signin");
        setNotice("Password updated. Sign in with your new password.");
      }
    } catch (err) {
      const failure = loginFailure(err);
      const retry =
        err instanceof ApiClientError && typeof err.details.retry_after_seconds === "number"
          ? err.details.retry_after_seconds
          : 0;
      if (failure.code === "rate_limited" && retry > 0) setCooldown(Math.min(retry, 300));
      const field: Field | null =
        failure.code === "weak_password" || failure.code === "breached_password"
          ? "password"
          : failure.code === "invalid_code" || failure.code === "expired_code"
            ? "code"
            : failure.code === "invalid_email"
              ? "email"
              : null;
      fail(failure.message, field);
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };

  const resendCode = async () => {
    const address = email.trim().toLowerCase();
    if (cooldown > 0 || submitting.current || !isValidEmail(address)) return;
    submitting.current = true;
    setPending(true);
    setError(null);
    try {
      if (view === "verify") await resendSignUpCode(address);
      else await requestPasswordReset(address);
      setNotice("We sent you a new code.");
      setCooldown(RESEND_COOLDOWN_SECONDS);
    } catch (err) {
      fail(loginFailure(err).message);
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };

  if (loading) {
    return (
      <main className={styles.page}>
        <p className={styles.loading} role="status">
          Loading…
        </p>
      </main>
    );
  }

  if (!cognitoConfig?.enabled) {
    return (
      <main className={styles.page}>
        <section className={styles.card} aria-labelledby="login-unavailable">
          <h1 id="login-unavailable" className={styles.title}>
            Sign-in isn't available
          </h1>
          <p className={styles.lede}>
            Sign-in isn't configured for this environment. Ask your administrator to set it up.
          </p>
          <Link className={styles.secondaryLink} to="/">
            Back to CortexAI
          </Link>
        </section>
      </main>
    );
  }

  if (logoutFailed && loggedIn) {
    return (
      <main className={styles.page}>
        <section className={styles.card} aria-labelledby="logout-failed-title">
          <h1 id="logout-failed-title" className={styles.title}>
            You're still signed in
          </h1>
          {error ? (
            <p className={styles.error} role="alert">
              {error}
            </p>
          ) : null}
          <button type="button" className={styles.submit} onClick={() => void logout()}>
            Sign out again
          </button>
          <Link className={styles.secondaryLink} to="/">
            Back to CortexAI
          </Link>
        </section>
      </main>
    );
  }

  const copy = VIEW_COPY[view];
  const showGoogle = googleEnabled && (view === "signin" || view === "signup");
  const showEmail = view !== "reset";
  const showPasswordField = view === "signin" || view === "signup" || view === "reset";
  const showCode = view === "verify" || view === "reset";
  const newPassword = view === "signup" || view === "reset";
  const describedBy = (field: Field, ...extra: string[]) =>
    [errorField === field && error ? ids.error : null, ...extra].filter(Boolean).join(" ") ||
    undefined;

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="login-title">
        <div className={styles.brand}>
          <span className={styles.mark} aria-hidden="true">
            <CortexIcon name="sparkle" size={18} />
          </span>
          <span className={styles.brandName}>CortexAI</span>
        </div>

        <h1 id="login-title" className={styles.title} ref={headingRef} tabIndex={-1}>
          {copy.title}
        </h1>
        <p className={styles.lede}>{copy.lede}</p>

        {notice ? (
          <p className={styles.notice} role="status">
            {notice}
          </p>
        ) : null}
        {error ? (
          <p className={styles.error} role="alert" id={ids.error}>
            {error}
          </p>
        ) : null}

        {showGoogle ? (
          <>
            <a
              className={styles.googleButton}
              href={googleLoginUrl(nextPath)}
              onClick={() => markFreshLoginPending()}
            >
              <GoogleMark />
              Continue with Google
            </a>
            {passwordEnabled ? (
              <div className={styles.divider} role="separator" aria-label="or">
                <span>or</span>
              </div>
            ) : null}
          </>
        ) : null}

        {passwordEnabled || !showGoogle ? (
          <form className={styles.form} onSubmit={handleSubmit} noValidate>
            {showEmail ? (
              <FieldRow label="Email" htmlFor={ids.email}>
                <input
                  id={ids.email}
                  ref={(node) => {
                    fieldRefs.current.email = node;
                  }}
                  className={styles.input}
                  type="email"
                  name="email"
                  inputMode="email"
                  autoComplete="username"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  maxLength={254}
                  required
                  readOnly={view === "verify"}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  aria-invalid={errorField === "email" ? true : undefined}
                  aria-describedby={describedBy("email")}
                />
              </FieldRow>
            ) : (
              <p className={styles.identity}>
                Resetting password for <strong>{email.trim().toLowerCase()}</strong>
              </p>
            )}

            {showCode ? (
              <FieldRow label="6-digit code" htmlFor={ids.code}>
                <input
                  id={ids.code}
                  ref={(node) => {
                    fieldRefs.current.code = node;
                  }}
                  className={`${styles.input} ${styles.codeInput}`}
                  type="text"
                  name="code"
                  inputMode="numeric"
                  pattern="[0-9]*"
                  autoComplete="one-time-code"
                  maxLength={6}
                  required
                  value={code}
                  onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  aria-invalid={errorField === "code" ? true : undefined}
                  aria-describedby={describedBy("code")}
                />
              </FieldRow>
            ) : null}

            {showPasswordField ? (
              <FieldRow
                label={view === "reset" ? "New password" : "Password"}
                htmlFor={ids.password}
                aside={
                  view === "signin" && passwordEnabled ? (
                    <button
                      type="button"
                      className={styles.linkButton}
                      onClick={() => goTo("forgot")}
                    >
                      Forgot password?
                    </button>
                  ) : null
                }
              >
                <div className={styles.passwordWrap}>
                  <input
                    id={ids.password}
                    ref={(node) => {
                      fieldRefs.current.password = node;
                    }}
                    className={`${styles.input} ${styles.passwordInput}`}
                    type={showPassword ? "text" : "password"}
                    name="password"
                    autoComplete={newPassword ? "new-password" : "current-password"}
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    maxLength={128}
                    required
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    aria-invalid={errorField === "password" ? true : undefined}
                    aria-describedby={describedBy("password", newPassword ? ids.rules : "")}
                  />
                  <button
                    type="button"
                    className={styles.toggle}
                    onClick={() => setShowPassword((value) => !value)}
                    aria-pressed={showPassword}
                    aria-label={showPassword ? "Hide password" : "Show password"}
                  >
                    {showPassword ? "Hide" : "Show"}
                  </button>
                </div>
                {newPassword ? (
                  <PasswordRules id={ids.rules} password={password} email={email} />
                ) : null}
              </FieldRow>
            ) : null}

            <button
              type="submit"
              className={styles.submit}
              disabled={pending}
              aria-busy={pending || undefined}
            >
              {pending ? copy.busy : copy.submit}
            </button>

            {view === "verify" || view === "reset" ? (
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={() => void resendCode()}
                disabled={pending || cooldown > 0}
              >
                {cooldown > 0 ? `Resend code in ${cooldown}s` : "Resend code"}
              </button>
            ) : null}
          </form>
        ) : null}

        <nav className={styles.footer} aria-label="Account options">
          {view === "signin" && signupEnabled ? (
            <p>
              New to CortexAI?{" "}
              <button type="button" className={styles.linkButton} onClick={() => goTo("signup")}>
                Create an account
              </button>
            </p>
          ) : null}
          {view === "signup" ? (
            <p>
              Already have an account?{" "}
              <button type="button" className={styles.linkButton} onClick={() => goTo("signin")}>
                Sign in
              </button>
            </p>
          ) : null}
          {view === "verify" || view === "forgot" || view === "reset" ? (
            <p>
              <button type="button" className={styles.linkButton} onClick={() => goTo("signin")}>
                Back to sign in
              </button>
            </p>
          ) : null}
        </nav>
      </section>
    </main>
  );
}

function FieldRow({
  label,
  htmlFor,
  aside,
  children,
}: {
  label: string;
  htmlFor: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className={styles.field}>
      <div className={styles.labelRow}>
        <label className={styles.label} htmlFor={htmlFor}>
          {label}
        </label>
        {aside}
      </div>
      {children}
    </div>
  );
}

function PasswordRules({ id, password, email }: { id: string; password: string; email: string }) {
  const checks = checkPassword(password, email.trim().toLowerCase());
  return (
    <ul className={styles.rules} id={id} aria-label="Password requirements">
      {checks.map((check) => (
        <li key={check.id} className={check.met ? styles.ruleMet : styles.rule}>
          <span aria-hidden="true">{check.met ? "✓" : "○"}</span>
          <span>{check.label}</span>
          <span className={styles.srOnly}>{check.met ? " (met)" : " (not met yet)"}</span>
        </li>
      ))}
    </ul>
  );
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.9 6.1C12.4 13.6 17.7 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.1 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.4c-.5 2.9-2.2 5.3-4.6 6.9l7.1 5.5c4.2-3.9 7.2-9.6 7.2-16.9z"
      />
      <path
        fill="#FBBC05"
        d="M10.5 28.7a14.5 14.5 0 0 1 0-9.4l-7.9-6.1a24 24 0 0 0 0 21.6l7.9-6.1z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.1-5.5c-2 1.4-4.6 2.3-8.8 2.3-6.3 0-11.6-4.1-13.5-9.8l-7.9 6.1C6.5 42.6 14.6 48 24 48z"
      />
    </svg>
  );
}
