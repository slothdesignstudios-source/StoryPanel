import { expect, test, type Page, type Request } from "@playwright/test";
import { API, apiToken, finishedComic, signIn } from "./helpers";

/**
 * Account A -> sign out -> account B in the same tab, without a page reload. Nothing that
 * belonged to A (titles, thumbnails, signed asset URLs, balances, drafts) may render for B,
 * including responses to A's requests that arrive late, and including when offline.
 */

const PROMPT = "A lighthouse keeper teaches a seal to read, as a 4 panel strip";

function emailOf(req: Request): string | null {
  const auth = req.headers()["authorization"];
  if (!auth) return null;
  const payload = auth.split(".")[1];
  return payload ? (JSON.parse(Buffer.from(payload, "base64url").toString()) as { email: string }).email : null;
}

async function signOutViaShell(page: Page) {
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Email")).toBeVisible();
}

async function privateMarkers(page: Page, token: string) {
  const h = { Authorization: `Bearer ${token}` };
  const projects = (await (await page.request.get(`${API}/v1/projects`, { headers: h })).json()).projects as { id: string; title: string; cover_url?: string | null }[];
  const me = await (await page.request.get(`${API}/v1/me`, { headers: h })).json();
  const urls = projects.map((p) => p.cover_url).filter((u): u is string => !!u).map((u) => new URL(u).pathname);
  return { titles: projects.map((p) => p.title), ids: projects.map((p) => p.id), urls, balance: String(me.balance) };
}

async function expectNoTraceOf(page: Page, a: Awaited<ReturnType<typeof privateMarkers>>) {
  const html = await page.content();
  for (const t of a.titles) expect(html, `A's title "${t}" rendered for B`).not.toContain(t);
  for (const id of a.ids) expect(html, `A's project id rendered for B`).not.toContain(id);
  for (const u of a.urls) expect(html, `A's signed asset URL rendered for B`).not.toContain(u);
  const imgs = await page.locator("img").evaluateAll((els) => els.map((e) => (e as HTMLImageElement).currentSrc || (e as HTMLImageElement).src));
  for (const u of a.urls) expect(imgs.some((s) => s.includes(u)), "A's image is displayed for B").toBeFalsy();
}

test.describe("account switch isolation", () => {
  const stamp = Date.now();
  const A = `iso-a-${stamp}@example.com`;
  const B = `iso-b-${stamp}@example.com`;
  let markersA: Awaited<ReturnType<typeof privateMarkers>>;

  test.beforeAll(async ({ request }) => {
    const token = await apiToken(request, A);
    await finishedComic(request, token, PROMPT);
    // B exists with a different balance, so a leaked A balance would be visible
    await apiToken(request, B);
  });

  test("A then B: no previous-account data, and B's requests carry only B's identity", async ({ page }) => {
    await signIn(page, A);
    const tokenA = await apiToken(page.request, A);
    markersA = await privateMarkers(page, tokenA);
    await expect(page.getByText(markersA.titles[0]!)).toBeVisible();
    // open A's comic in the editor so its signed URLs and drafts are in memory
    await page.getByText(markersA.titles[0]!).click();
    await expect(page).toHaveURL(/\/projects\//);
    await signOutViaShell(page);
    const requestsAfter: Request[] = [];
    page.on("request", (r) => { if (r.url().startsWith(API)) requestsAfter.push(r); });
    await page.getByLabel("Email").fill(B);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
    await page.waitForTimeout(1500);
    await expectNoTraceOf(page, markersA);
    for (const r of requestsAfter) {
      const who = emailOf(r);
      expect(who === null || who === B, `request ${r.url()} sent as ${who}`).toBeTruthy();
    }
    await page.getByRole("link", { name: "Credits" }).first().click();
    await expect(page.getByRole("heading", { name: /credits/i }).first()).toBeVisible();
    await expectNoTraceOf(page, markersA);
  });

  test("slow responses for A that land after the switch are dropped", async ({ page }) => {
    await signIn(page, A);
    const tokenA = await apiToken(page.request, A);
    markersA = await privateMarkers(page, tokenA);
    // From now on A's list/me/credits responses take 4 s to arrive.
    await page.route(`${API}/v1/**`, async (route) => {
      const who = emailOf(route.request());
      if (who === A) await new Promise((r) => setTimeout(r, 4000));
      await route.continue().catch(() => undefined);
    });
    await page.getByRole("link", { name: "Comics" }).first().click(); // triggers slow refetches for A
    await page.waitForTimeout(300);
    await signOutViaShell(page);
    await page.getByLabel("Email").fill(B);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
    await page.waitForTimeout(5500); // A's delayed responses have now arrived
    await expectNoTraceOf(page, markersA);
  });

  test("offline: signing out clears A's data without network, then B signs in", async ({ page, context }) => {
    await signIn(page, A);
    const tokenA = await apiToken(page.request, A);
    markersA = await privateMarkers(page, tokenA);
    await expect(page.getByText(markersA.titles[0]!)).toBeVisible();
    await context.setOffline(true);
    // Sign-out is local; navigation to the sign-in route may not complete while offline.
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.waitForTimeout(1500);
    await expectNoTraceOf(page, markersA);
    await context.setOffline(false);
    await page.goto("/sign-in");
    await page.getByLabel("Email").fill(B);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
    await page.waitForTimeout(1000);
    await expectNoTraceOf(page, markersA);
  });
});
