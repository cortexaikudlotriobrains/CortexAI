import { expect, expectNoHorizontalOverflow, test } from "../fixtures/responsive-e2e.mjs";

const VIEWPORTS = [
    { name: "desktop", size: { width: 1440, height: 900 } },
    { name: "phone", size: { width: 390, height: 844 } },
];

for (const viewport of VIEWPORTS) {
    test(`login page renders and validates on ${viewport.name}`, async ({ page }) => {
        await page.setViewportSize(viewport.size);
        const requests = await installLoginRoutes(page);

        await page.goto("/login");

        await expect(page.getByRole("heading", { name: "Sign in to CortexAI" })).toBeVisible();
        const google = page.getByRole("link", { name: /Continue with Google/ });
        await expect(google).toHaveAttribute("href", "/v1/auth/google/start?next=%2F");
        await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
        await expect(page.getByLabel("Password", { exact: true })).toHaveAttribute("type", "password");
        await expectNoHorizontalOverflow(page);

        await page.getByRole("button", { name: "Sign in" }).click();
        await expect(page.getByRole("alert")).toHaveText("Enter a valid email address.");
        expect(requests.passwordLogins).toEqual([]);

        await page.getByRole("button", { name: "Create an account" }).click();
        await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
        await page.getByLabel("Password", { exact: true }).fill("short");
        await expect(page.getByRole("list", { name: "Password requirements" })).toContainText(
            "At least 12 characters",
        );
        await expectNoHorizontalOverflow(page);
    });
}

test("password sign-in sends the credentials to the API only and returns to next", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS[0].size);
    const requests = await installLoginRoutes(page);

    await page.goto("/login?next=%2Fusage");
    await page.getByLabel("Email", { exact: true }).fill("Ada@Example.com");
    await page.getByLabel("Password", { exact: true }).fill("correct horse battery");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect.poll(() => requests.passwordLogins.length).toBe(1);
    expect(requests.passwordLogins[0]).toEqual({
        email: "ada@example.com",
        password: "correct horse battery",
    });
    await expect(page).toHaveURL(/\/usage$/);
    expect(page.url()).not.toContain("correct%20horse");
});

test("failed sign-in shows generic static copy and stays on the page", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS[1].size);
    await installLoginRoutes(page, { loginStatus: 401, loginCode: "invalid_credentials" });

    await page.goto("/login");
    await page.getByLabel("Email", { exact: true }).fill("ada@example.com");
    await page.getByLabel("Password", { exact: true }).fill("not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("alert")).toHaveText("Incorrect email or password.");
    await expect(page).toHaveURL(/\/login$/);
    await expectNoHorizontalOverflow(page);
});

test("external next targets are ignored", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS[0].size);
    await installLoginRoutes(page);

    await page.goto("/login?next=https%3A%2F%2Fevil.example%2Fphish");
    await expect(page.getByRole("link", { name: /Continue with Google/ })).toHaveAttribute(
        "href",
        "/v1/auth/google/start?next=%2F",
    );
});

test("the signed-out workspace gate opens the login page and remembers the page", async ({ page }) => {
    await page.setViewportSize(VIEWPORTS[0].size);
    await installLoginRoutes(page);

    await page.goto("/");
    await page
        .getByRole("region", { name: "Sign in to use CortexAI" })
        .getByRole("button", { name: "Sign in" })
        .click();

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "Sign in to CortexAI" })).toBeVisible();
});

async function installLoginRoutes(page, { loginStatus = 200, loginCode = "invalid_credentials" } = {}) {
    const requests = { passwordLogins: [] };

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
                passwordLoginEnabled: true,
                signupEnabled: true,
                googleLoginEnabled: true,
            });
        }
        if (url.pathname === "/v1/auth/login/password" && method === "POST") {
            requests.passwordLogins.push(request.postDataJSON());
            if (loginStatus !== 200) {
                return json(
                    route,
                    { detail: { code: loginCode, message: "server text that is never shown" } },
                    loginStatus,
                );
            }
            return json(route, { user_id: "user-1", email: "ada@example.com" });
        }
        if (url.pathname === "/v1/whoami" && method === "GET") {
            return json(route, { detail: "Not authenticated" }, 401);
        }
        return json(route, { detail: `Unhandled login route: ${method} ${url.pathname}` }, 404);
    });

    return requests;
}

function json(route, body, status = 200) {
    return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
    });
}
