import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

const VIEWPORTS = [
    { name: "desktop", size: { width: 1440, height: 900 } },
    { name: "phone", size: { width: 390, height: 844 } },
];

for (const viewport of VIEWPORTS) {
    test(`signed-out Cognito gate renders on ${viewport.name}`, async ({ page }) => {
        const sessionScopedStartupPaths = [];
        await page.setViewportSize(viewport.size);
        await installSignedOutRoutes(page, sessionScopedStartupPaths);

        await page.goto("/");

        const signInGate = page.getByRole("region", { name: "Sign in to use CortexAI" });
        await expect(signInGate).toBeVisible();
        await expect(signInGate).toContainText(
            "Access your AI workspace, saved chats, model comparison, and file analysis.",
        );
        await expect(signInGate.getByRole("button", { name: "Sign in" })).toBeVisible();
        await expect(page.locator("#promptInput")).toHaveCount(0);
        await expect(page.getByText(/Backend not connected/i)).toHaveCount(0);
        await expect(page.getByText(/port 8000/i)).toHaveCount(0);
        await expect(page.getByRole("navigation", { name: "Mobile navigation" })).toHaveCount(0);

        if (viewport.name === "desktop") {
            const sidebar = page.getByRole("complementary", { name: "Primary navigation" });
            await expect(sidebar).toContainText("Sign in to view history.");
            await expect(sidebar.getByRole("button", { name: "New chat" })).toBeDisabled();
        }

        await page.waitForTimeout(100);
        expect(sessionScopedStartupPaths).toEqual([]);
        await expectNoHorizontalOverflow(page);
    });
}

async function installSignedOutRoutes(page, sessionScopedStartupPaths) {
    await page.route("**/runtime-config.js", route =>
        route.fulfill({
            status: 200,
            contentType: "application/javascript",
            body: "window.CORTEX_RUNTIME_CONFIG = { enableDevSessionLogin: false };",
        }),
    );

    await page.route("**/v1/**", async route => {
        const request = route.request();
        const url = new URL(request.url());
        const method = request.method();

        if (url.pathname === "/v1/auth/cognito-config" && method === "GET") {
            return json(route, {
                enabled: true,
                domain: "https://auth.example.com",
                clientId: "client-123",
                logoutUrl: "https://auth.example.com/logout?client_id=client-123",
            });
        }

        if (url.pathname === "/v1/whoami" && method === "GET") {
            return json(route, { detail: "Not authenticated" }, 401);
        }

        if (["/v1/providers", "/v1/model-options", "/v1/models", "/v1/history"].includes(url.pathname)) {
            sessionScopedStartupPaths.push(url.pathname);
            return json(route, { detail: `Unexpected signed-out request: ${url.pathname}` }, 500);
        }

        return json(route, { detail: `Unhandled signed-out route: ${method} ${url.pathname}` }, 404);
    });
}

function json(route, body, status = 200) {
    return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
    });
}
