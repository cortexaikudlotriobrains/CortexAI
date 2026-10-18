import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError } from "../api/client";
import { LoginPage } from "../pages/LoginPage";

const mocks = vi.hoisted(() => ({
  authState: {
    cognitoConfig: null as Record<string, unknown> | null,
    whoAmI: null,
    loading: false,
    loggedIn: false,
  },
  logout: vi.fn(),
  signUp: vi.fn(),
  confirmSignUp: vi.fn(),
  resendSignUpCode: vi.fn(),
  loginWithPassword: vi.fn(),
  requestPasswordReset: vi.fn(),
  resetPassword: vi.fn(),
  markFreshLoginPending: vi.fn(),
}));

vi.mock("../hooks/useAuth", () => ({
  useAuth: () => ({
    ...mocks.authState,
    login: vi.fn(),
    logout: mocks.logout,
    refresh: vi.fn(),
  }),
}));

vi.mock("../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../api/auth")>("../api/auth");
  return {
    ...actual,
    signUp: mocks.signUp,
    confirmSignUp: mocks.confirmSignUp,
    resendSignUpCode: mocks.resendSignUpCode,
    loginWithPassword: mocks.loginWithPassword,
    requestPasswordReset: mocks.requestPasswordReset,
    resetPassword: mocks.resetPassword,
  };
});

vi.mock("../session/activeSession", () => ({
  markFreshLoginPending: mocks.markFreshLoginPending,
}));

const GOOD_PASSWORD = "correct horse battery";

function apiError(status: number, code: string, extra: Record<string, unknown> = {}) {
  return new ApiClientError(status, "server text that must never be shown", {
    detail: { code, message: "server text that must never be shown", ...extra },
  });
}

function Landing() {
  const location = useLocation();
  return <div data-testid="landing">{`${location.pathname}${location.search}`}</div>;
}

function renderLogin(url = "/login") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Landing />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    mocks.authState.cognitoConfig = {
      enabled: true,
      passwordLoginEnabled: true,
      signupEnabled: true,
      googleLoginEnabled: true,
    };
    mocks.authState.loading = false;
    mocks.authState.loggedIn = false;
    mocks.loginWithPassword.mockResolvedValue({ user_id: "u1", email: "ada@example.com" });
    mocks.signUp.mockResolvedValue({ status: "verification_required" });
    mocks.confirmSignUp.mockResolvedValue({ status: "verified" });
    mocks.resendSignUpCode.mockResolvedValue({ status: "code_sent" });
    mocks.requestPasswordReset.mockResolvedValue({ status: "code_sent" });
    mocks.resetPassword.mockResolvedValue({ status: "password_reset" });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("offers Google and email sign-in with the right autofill hints", () => {
    renderLogin();

    expect(screen.getByRole("heading", { name: "Sign in to CortexAI" })).toBeInTheDocument();
    const google = screen.getByRole("link", { name: /Continue with Google/ });
    expect(google).toHaveAttribute("href", "/v1/auth/google/start?next=%2F");

    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("type", "email");
    expect(email).toHaveAttribute("autocomplete", "username");
    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
  });

  it("carries a safe next target into the Google link and ignores external ones", () => {
    renderLogin("/login?next=%2Fusage");
    expect(screen.getByRole("link", { name: /Continue with Google/ })).toHaveAttribute(
      "href",
      "/v1/auth/google/start?next=%2Fusage",
    );
    cleanup();
    renderLogin("/login?next=https%3A%2F%2Fevil.example");
    expect(screen.getByRole("link", { name: /Continue with Google/ })).toHaveAttribute(
      "href",
      "/v1/auth/google/start?next=%2F",
    );
  });

  it("signs in, starts a fresh session and returns to the requested page", async () => {
    const user = userEvent.setup();
    renderLogin("/login?next=%2Fusage");

    await user.type(screen.getByLabelText("Email"), " Ada@Example.com ");
    await user.type(screen.getByLabelText("Password"), GOOD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByTestId("landing")).toHaveTextContent("/usage"));
    expect(mocks.loginWithPassword).toHaveBeenCalledWith("ada@example.com", GOOD_PASSWORD);
    expect(mocks.markFreshLoginPending).toHaveBeenCalled();
  });

  it("shows only static copy for failures and keeps the user on the form", async () => {
    const user = userEvent.setup();
    mocks.loginWithPassword.mockRejectedValue(apiError(401, "invalid_credentials"));
    renderLogin();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password-1");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Incorrect email or password.");
    expect(document.body).not.toHaveTextContent("server text that must never be shown");
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByTestId("landing")).not.toBeInTheDocument();
  });

  it("validates before calling the API", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a valid email address.");
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter your password.");
    expect(mocks.loginWithPassword).not.toHaveBeenCalled();
  });

  it("toggles password visibility accessibly", async () => {
    const user = userEvent.setup();
    renderLogin();

    const toggle = screen.getByRole("button", { name: "Show password" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await user.click(toggle);
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("blocks double submission while a request is pending", async () => {
    const user = userEvent.setup();
    let resolve: (value: unknown) => void = () => undefined;
    mocks.loginWithPassword.mockReturnValue(new Promise((r) => (resolve = r)));
    renderLogin();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), GOOD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const busy = await screen.findByRole("button", { name: "Signing in…" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
    await user.click(busy);
    expect(mocks.loginWithPassword).toHaveBeenCalledTimes(1);
    resolve({ user_id: "u1", email: "ada@example.com" });
  });

  it("creates an account, verifies the email and signs in without retyping", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "Create an account" }));
    expect(screen.getByRole("heading", { name: "Create your account" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("autocomplete", "new-password");

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    const rules = screen.getByRole("list", { name: "Password requirements" });
    expect(within(rules).getByText(/At least 12 characters/).closest("li")).toHaveTextContent(
      "not met",
    );
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/stronger password/);
    expect(mocks.signUp).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), GOOD_PASSWORD);
    expect(within(rules).getByText(/At least 12 characters/).closest("li")).toHaveTextContent(
      "(met)",
    );
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("heading", { name: "Check your email" })).toBeInTheDocument();
    expect(mocks.signUp).toHaveBeenCalledWith("ada@example.com", GOOD_PASSWORD);
    expect(screen.getByRole("status")).toHaveTextContent("ada@example.com");
    expect(screen.getByRole("button", { name: /Resend code in \d+s/ })).toBeDisabled();

    const code = screen.getByLabelText("6-digit code");
    expect(code).toHaveAttribute("autocomplete", "one-time-code");
    await user.type(code, "12a3456789");
    expect(code).toHaveValue("123456");
    await user.click(screen.getByRole("button", { name: "Verify email" }));

    await waitFor(() => expect(screen.getByTestId("landing")).toHaveTextContent("/"));
    expect(mocks.confirmSignUp).toHaveBeenCalledWith("ada@example.com", "123456");
    expect(mocks.loginWithPassword).toHaveBeenCalledWith("ada@example.com", GOOD_PASSWORD);
  });

  it("sends unverified accounts to the verification step with a fresh code", async () => {
    const user = userEvent.setup();
    mocks.loginWithPassword.mockRejectedValue(apiError(403, "email_not_verified"));
    renderLogin();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), GOOD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Check your email" })).toBeInTheDocument();
    expect(mocks.resendSignUpCode).toHaveBeenCalledWith("ada@example.com");
  });

  it("walks through forgot and reset password", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "Forgot password?" }));
    expect(screen.getByRole("heading", { name: "Reset your password" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.click(screen.getByRole("button", { name: "Send reset code" }));
    expect(await screen.findByRole("heading", { name: "Choose a new password" })).toBeInTheDocument();
    // Identical wording whether or not the account exists.
    expect(screen.getByRole("status")).toHaveTextContent(
      "If an account exists for ada@example.com, we've sent a 6-digit code.",
    );

    await user.type(screen.getByLabelText("6-digit code"), "654321");
    await user.type(screen.getByLabelText("New password"), GOOD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Update password" }));

    expect(await screen.findByRole("heading", { name: "Sign in to CortexAI" })).toBeInTheDocument();
    expect(mocks.resetPassword).toHaveBeenCalledWith("ada@example.com", "654321", GOOD_PASSWORD);
    expect(screen.getByRole("status")).toHaveTextContent("Password updated");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("explains Google redirect failures without echoing the query", () => {
    renderLogin("/login?error=account_exists");
    expect(screen.getByRole("alert")).toHaveTextContent(/already exists/);
    cleanup();
    renderLogin("/login?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E");
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong. Please try again.");
    expect(document.body).not.toHaveTextContent("<script>");
  });

  it("honours server-side method availability", () => {
    mocks.authState.cognitoConfig = {
      enabled: true,
      passwordLoginEnabled: true,
      signupEnabled: false,
      googleLoginEnabled: false,
    };
    renderLogin();
    expect(screen.queryByRole("link", { name: /Continue with Google/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create an account" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows a clear state when sign-in is not configured", () => {
    mocks.authState.cognitoConfig = { enabled: false };
    renderLogin();
    expect(screen.getByRole("heading", { name: "Sign-in isn't available" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to CortexAI" })).toHaveAttribute("href", "/");
  });

  it("leaves the login page for signed-in users", async () => {
    mocks.authState.loggedIn = true;
    renderLogin("/login?next=%2Fwork");
    await waitFor(() => expect(screen.getByTestId("landing")).toHaveTextContent("/work"));
  });

  it("shows breached-password feedback on the password field", async () => {
    const user = userEvent.setup();
    mocks.signUp.mockRejectedValue(apiError(422, "breached_password"));
    renderLogin();
    await user.click(screen.getByRole("button", { name: "Create an account" }));
    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), GOOD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/known data breach/);
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
  });

  it("stays put after a failed sign-out and lets the user retry", async () => {
    const user = userEvent.setup();
    mocks.authState.loggedIn = true;
    renderLogin("/login?error=logout_failed");

    expect(screen.getByRole("heading", { name: "You're still signed in" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("couldn't sign you out");
    expect(screen.queryByTestId("landing")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out again" }));
    expect(mocks.logout).toHaveBeenCalledTimes(1);
  });
});

