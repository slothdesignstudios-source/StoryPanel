import { expect, test, type Request } from "@playwright/test";

/**
 * LIVE Clerk integration (development instance). Skipped unless E2E_CLERK=1.
 * Needs the web app built with NEXT_PUBLIC_AUTH_MODE=clerk and NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY,
 * the API running with SP_AUTH_MODE=clerk + SP_CLERK_* (incl. SP_CLERK_SECRET_KEY), and, for this
 * test process only, E2E_CLERK_SECRET_KEY (the same development-instance secret) to create and
 * inspect test users through Clerk's Backend API. Never put that secret in a NEXT_PUBLIC_ variable.
 *
 *   E2E_CLERK=1 E2E_CLERK_SECRET_KEY=... pnpm exec playwright test e2e/live/clerk.spec.ts --project=desktop
 */
const LIVE = process.env.E2E_CLERK === "1";
const CLERK_API = "https://api.clerk.com/v1";
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
test.skip(!LIVE, "set E2E_CLERK=1 to run the live Clerk tests");
test.describe.configure({ mode: "serial", timeout: 600_000 });

async function clerk(method: string, path: string, body?: unknown) {
  const r = await fetch(`${CLERK_API}${path}`, { method, headers: { Authorization: `Bearer ${process.env.E2E_CLERK_SECRET_KEY}`, "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  return { status: r.status, body: r.status === 204 ? null : await r.json().catch(() => null) };
}

const stamp = Date.now();
const users = { A: { email: `sp-a-${stamp}+clerk_test@example.com`, password: `Sp-${stamp}-a!Q9`, id: "" }, B: { email: `sp-b-${stamp}+clerk_test@example.com`, password: `Sp-${stamp}-b!Q9`, id: "" } };

test.beforeAll(async () => {
  for (const u of Object.values(users)) {
    const r = await clerk("POST", "/users", { email_address: [u.email], password: u.password, skip_password_checks: true });
    expect(r.status, JSON.stringify(r.body)).toBe(200);
    u.id = r.body.id;
  }
});

test.afterAll(async () => {
  for (const u of Object.values(users)) if (u.id) await clerk("DELETE", `/users/${u.id}`); // cleanup; 404 is fine
});

async function signIn(page: import("@playwright/test").Page, u: { email: string; password: string }) {
  await page.goto("/sign-in");
  await page.getByLabel(/email address/i).fill(u.email);
  await page.getByRole("button", { name: /^continue$/i }).click();
  await page.getByLabel(/^password$/i).fill(u.password);
  await page.getByRole("button", { name: /^continue$/i }).click();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible({ timeout: 60_000 });
}

test("sign-in, token refresh past expiry, isolation, logout and deletion", async ({ page }) => {
  const unauthorized: string[] = [];
  const authAfterLogout: string[] = [];
  let loggedOut = false;
  page.on("response", (r) => { if (r.url().startsWith(API) && r.status() === 401) unauthorized.push(r.url()); });
  page.on("request", (r: Request) => { if (loggedOut && r.url().startsWith(API) && r.headers()["authorization"]) authAfterLogout.push(r.url()); });

  await signIn(page, users.A);
  const me = await page.evaluate(async (api) => { const t = await (window as any).Clerk.session.getToken(); return (await fetch(`${api}/v1/me`, { headers: { Authorization: `Bearer ${t}` } })).json(); }, API);
  expect(me.id).toBeTruthy();
  const title = `Clerk live ${stamp}`;
  await page.evaluate(async ([api, prompt]) => { const t = await (window as any).Clerk.session.getToken(); await fetch(`${api}/v1/projects`, { method: "POST", headers: { Authorization: `Bearer ${t}`, "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ prompt }) }); }, [API, `${title}: a robot waters a cactus, as a 4 panel strip`]);

  // Clerk session tokens live 60 s: keep using the app past that and expect no 401
  await page.waitForTimeout(75_000);
  await page.getByRole("link", { name: "Comics" }).first().click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
  expect(unauthorized, "requests rejected after the first token expired").toEqual([]);

  // logout, then B in the same tab: nothing of A, no request sent with a token while signed out
  await page.getByRole("button", { name: "Sign out" }).click();
  loggedOut = true;
  await page.waitForTimeout(3000);
  expect(authAfterLogout, "authenticated requests after logout").toEqual([]);
  loggedOut = false;
  await signIn(page, users.B);
  await page.waitForTimeout(2000);
  expect(await page.content()).not.toContain(String(stamp) + ": a robot");
  const bMe = await page.evaluate(async (api) => { const t = await (window as any).Clerk.session.getToken(); return (await fetch(`${api}/v1/me`, { headers: { Authorization: `Bearer ${t}` } })).json(); }, API);
  expect(bMe.id).not.toBe(me.id);

  // deletion from settings: our API deletes the Clerk user (deletion job; maintenance loop finishes it)
  await page.goto("/settings");
  await page.getByRole("button", { name: "Delete my account" }).click();
  await page.getByLabel("Confirmation").fill("DELETE");
  await page.getByRole("dialog").getByRole("button", { name: /delete/i }).click();
  await expect.poll(async () => (await clerk("GET", `/users/${users.B.id}`)).status, { timeout: 300_000, intervals: [5000] }).toBe(404);
  users.B.id = "";
});
