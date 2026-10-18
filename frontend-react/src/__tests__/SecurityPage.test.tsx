import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { deviceLabel } from "../auth/deviceLabel";
import { SessionList } from "../pages/SecurityPage";

const mocks = vi.hoisted(() => ({
  listSessions: vi.fn(),
  revokeSession: vi.fn(),
  revokeOtherSessions: vi.fn(),
}));

vi.mock("../api/auth", () => ({
  listSessions: mocks.listSessions,
  revokeSession: mocks.revokeSession,
  revokeOtherSessions: mocks.revokeOtherSessions,
}));

const MAC_CHROME =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const IPHONE_SAFARI =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";

function session(id: string, current: boolean, userAgent: string, method = "password") {
  return {
    id,
    auth_method: method,
    created_at: "2026-10-17T09:00:00Z",
    last_seen_at: "2026-10-18T07:00:00Z",
    expires_at: "2026-10-24T09:00:00Z",
    user_agent: userAgent,
    current,
  };
}

describe("SessionList", () => {
  beforeEach(() => {
    mocks.listSessions.mockResolvedValue({
      sessions: [session("s1", true, MAC_CHROME), session("s2", false, IPHONE_SAFARI, "google")],
    });
    mocks.revokeSession.mockResolvedValue({ ok: true });
    mocks.revokeOtherSessions.mockResolvedValue({ ok: true, revoked: 1 });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("lists devices and marks the current one without a sign-out button", async () => {
    render(<SessionList />);
    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Chrome on macOS");
    expect(items[0]).toHaveTextContent("This device");
    expect(within(items[0]!).queryByRole("button")).not.toBeInTheDocument();
    expect(items[1]).toHaveTextContent("Safari on iOS");
    expect(items[1]).toHaveTextContent("Google");
  });

  it("signs out one other device and refreshes the list", async () => {
    const user = userEvent.setup();
    render(<SessionList />);
    const button = await screen.findByRole("button", { name: /Sign out Safari on iOS/ });
    mocks.listSessions.mockResolvedValue({ sessions: [session("s1", true, MAC_CHROME)] });
    await user.click(button);

    expect(mocks.revokeSession).toHaveBeenCalledWith("s2");
    expect(await screen.findByRole("status")).toHaveTextContent("Signed out Safari on iOS.");
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(1));
    expect(screen.getByRole("button", { name: "Sign out of all other devices" })).toBeDisabled();
  });

  it("signs out all other devices", async () => {
    const user = userEvent.setup();
    render(<SessionList />);
    await user.click(await screen.findByRole("button", { name: "Sign out of all other devices" }));
    expect(mocks.revokeOtherSessions).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("status")).toHaveTextContent("Signed out 1 other device.");
  });

  it("shows a retry state when devices cannot load", async () => {
    const user = userEvent.setup();
    mocks.listSessions.mockRejectedValueOnce(new Error("boom"));
    render(<SessionList />);
    expect(await screen.findByRole("alert")).toHaveTextContent("couldn't be loaded");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findAllByRole("listitem")).toHaveLength(2);
  });
});

describe("deviceLabel", () => {
  it.each([
    [MAC_CHROME, "Chrome on macOS"],
    [IPHONE_SAFARI, "Safari on iOS"],
    ["Mozilla/5.0 (Windows NT 10.0) AppleWebKit Chrome/130 Safari/537 Edg/130", "Edge on Windows"],
    ["Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Firefox on Linux"],
    ["curl/8.0", "Unknown device"],
    [null, "Unknown device"],
  ])("%s -> %s", (ua, label) => {
    expect(deviceLabel(ua)).toBe(label);
  });
});
