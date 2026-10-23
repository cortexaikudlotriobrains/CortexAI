import { ApiClientError } from "../api/client";

export type LoginErrorCode =
  | "invalid_credentials"
  | "email_not_verified"
  | "email_unverified"
  | "password_reset_required"
  | "challenge_unsupported"
  | "weak_password"
  | "breached_password"
  | "invalid_email"
  | "invalid_code"
  | "expired_code"
  | "rate_limited"
  | "account_exists_other_method"
  | "account_disabled"
  | "auth_method_disabled"
  | "signup_unavailable"
  | "origin_not_allowed"
  | "auth_unavailable";

const COPY: Record<LoginErrorCode, string> = {
  invalid_credentials: "Incorrect email or password.",
  email_not_verified: "Verify your email address to continue.",
  email_unverified:
    "This account's email address isn't verified yet. Contact support, or continue with Google if you use it.",
  password_reset_required: "For your security, reset your password to continue.",
  challenge_unsupported: "This account needs extra steps. Reset your password to continue.",
  weak_password: "Choose a stronger password that meets the requirements below.",
  breached_password:
    "This password has appeared in a known data breach, so it isn't safe to use. Choose a different one.",
  invalid_email: "Enter a valid email address.",
  invalid_code: "That code isn't correct. Check the 6-digit code from your email.",
  expired_code: "That code has expired. Request a new one.",
  rate_limited: "Too many attempts. Please wait a few minutes and try again.",
  account_exists_other_method:
    "An account with this email already exists. Sign in the way you did before, for example with Google.",
  account_disabled: "This account isn't available. Contact support if you think this is a mistake.",
  auth_method_disabled: "This sign-in method isn't available right now.",
  signup_unavailable:
    "Creating an account isn't available right now. Try Continue with Google, or contact support.",
  origin_not_allowed: "This request couldn't be completed. Reload the page and try again.",
  auth_unavailable: "Sign-in is temporarily unavailable. Please try again shortly.",
};

const GENERIC = "Something went wrong. Please try again.";

/** Server `?error=` reasons from the Google callback redirect. */
const OAUTH_REDIRECT_COPY: Record<string, string> = {
  google_cancelled: "Google sign-in was cancelled.",
  google_failed: "We couldn't sign you in with Google. Please try again.",
  session_expired: "That sign-in attempt expired. Please try again.",
  email_not_verified: "Your Google account's email address isn't verified.",
  account_exists: COPY.account_exists_other_method,
  account_disabled: COPY.account_disabled,
  logout_failed: "We couldn't sign you out. Check your connection and try again.",
};

function isLoginErrorCode(code: string | null): code is LoginErrorCode {
  return code !== null && Object.prototype.hasOwnProperty.call(COPY, code);
}

export interface LoginFailure {
  code: LoginErrorCode | "unknown";
  /** Static copy only: never server-provided text. */
  message: string;
}

export function loginFailure(error: unknown): LoginFailure {
  if (error instanceof ApiClientError) {
    if (isLoginErrorCode(error.code)) return { code: error.code, message: COPY[error.code] };
    if (error.status === 429) return { code: "rate_limited", message: COPY.rate_limited };
    if (error.status >= 500) return { code: "auth_unavailable", message: COPY.auth_unavailable };
  } else if (error instanceof TypeError) {
    return { code: "auth_unavailable", message: COPY.auth_unavailable };
  }
  return { code: "unknown", message: GENERIC };
}

export function oauthRedirectMessage(reason: string | null): string | null {
  if (!reason) return null;
  return Object.prototype.hasOwnProperty.call(OAUTH_REDIRECT_COPY, reason)
    ? OAUTH_REDIRECT_COPY[reason]!
    : GENERIC;
}
