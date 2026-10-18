import { afterEach, describe, expect, it, vi } from "vitest";
import {
  confirmSignUp,
  googleLoginUrl,
  loginWithPassword,
  logoutSession,
  requestPasswordReset,
  resendSignUpCode,
  resetPassword,
  signUp,
} from "../api/auth";
import { currentNextPath, loginPageUrl, safeNextPath } from "../auth/redirect";

function mockFetchOnce(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("auth API client", () => {
  const cases: Array<[string, () => Promise<unknown>, string, unknown]> = [
    ["signUp", () => signUp("a@b.co", "pw"), "/v1/auth/signup", { email: "a@b.co", password: "pw" }],
    [
      "confirmSignUp",
      () => confirmSignUp("a@b.co", "123456"),
      "/v1/auth/signup/confirm",
      { email: "a@b.co", code: "123456" },
    ],
    [
      "resendSignUpCode",
      () => resendSignUpCode("a@b.co"),
      "/v1/auth/signup/resend",
      { email: "a@b.co" },
    ],
    [
      "loginWithPassword",
      () => loginWithPassword("a@b.co", "pw"),
      "/v1/auth/login/password",
      { email: "a@b.co", password: "pw" },
    ],
    [
      "requestPasswordReset",
      () => requestPasswordReset("a@b.co"),
      "/v1/auth/password/forgot",
      { email: "a@b.co" },
    ],
    [
      "resetPassword",
      () => resetPassword("a@b.co", "123456", "new-pw"),
      "/v1/auth/password/reset",
      { email: "a@b.co", code: "123456", new_password: "new-pw" },
    ],
    ["logoutSession", () => logoutSession(), "/v1/auth/logout", {}],
  ];

  it.each(cases)("%s posts JSON with credentials to the backend", async (_n, call, path, body) => {
    const fetchMock = mockFetchOnce({ status: "ok" });
    await call();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe(path);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it("never puts credentials in a URL", () => {
    expect(googleLoginUrl("/work?x=1")).toBe("/v1/auth/google/start?next=%2Fwork%3Fx%3D1");
  });
});

describe("post-login redirect safety", () => {
  it.each([
    "https://evil.example",
    "//evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
    "relative",
    "/login",
    "/login?next=/",
    "/auth?code=1",
    "/v1/auth/me",
    "/ok\nSet-Cookie: x",
    "",
    null,
    undefined,
  ])("falls back to / for %j", (value) => {
    expect(safeNextPath(value as string | null | undefined)).toBe("/");
  });

  it("keeps in-app paths", () => {
    expect(safeNextPath("/work/abc?x=1")).toBe("/work/abc?x=1");
    expect(safeNextPath("/account/billing")).toBe("/account/billing");
  });

  it("builds login URLs and strips the transient fresh_login param", () => {
    expect(loginPageUrl("/")).toBe("/login");
    expect(loginPageUrl("/usage")).toBe("/login?next=%2Fusage");
    expect(loginPageUrl("https://evil.example")).toBe("/login");
    expect(currentNextPath({ pathname: "/usage", search: "?fresh_login=1&tab=a" })).toBe(
      "/usage?tab=a",
    );
  });
});
