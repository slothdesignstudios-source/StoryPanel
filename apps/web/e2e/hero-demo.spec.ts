import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

/** The landing-page hero demo: illustrative only, never touches the API, never downloads anything. */

const step = (page: Page) => page.getByText(/^Step \d of 9$/);
const next = (page: Page) => page.getByRole("button", { name: "Next", exact: true });
async function settle(page: Page) {
  const skip = page.getByRole("button", { name: "Skip ahead" });
  if (await skip.count()) await skip.click();
}

test("Watch plays, pauses and keeps its place; the CTA is always available", async ({ page, baseURL }) => {
  // the demo requests nothing but the page's own assets: no API, no provider, no third party
  const calls: string[] = [];
  page.on("request", (r) => { const u = new URL(r.url()); if (u.pathname.startsWith("/v1/") || u.origin !== new URL(baseURL!).origin) calls.push(r.url()); });
  let downloads = 0;
  page.on("download", () => downloads++);
  await page.goto("/");
  const main = page.locator("main");
  await expect(main.getByRole("link", { name: "Start a comic" }).first()).toHaveAttribute("href", "/new");
  await expect(page.getByText("Illustrative demo with sample artwork", { exact: false })).toBeVisible();
  const pause = page.getByRole("button", { name: "Pause" });
  await expect(pause).toBeVisible({ timeout: 5000 });
  await page.waitForTimeout(1500);
  await pause.click();
  const scene = await page.locator('[aria-label="Demo scenes"] [aria-current="step"]').innerText();
  await page.waitForTimeout(1500);
  await expect(page.locator('[aria-label="Demo scenes"] [aria-current="step"]')).toHaveText(scene);
  await expect(page.getByRole("button", { name: "Play" })).toBeVisible();
  // the stage is decorative while it plays: hidden from assistive tech and out of the tab order
  await expect(page.locator("[inert]")).toHaveCount(1);
  expect(downloads).toBe(0);
  expect(calls.filter((u) => !u.includes("/v1/meta"))).toEqual([]);
});

test("Explore steps through the workflow with gates, a keyboard balloon edit and a real CTA", async ({ page }) => {
  const calls: string[] = [];
  page.on("request", (r) => { if (new URL(r.url()).pathname.startsWith("/v1/")) calls.push(r.url()); });
  let downloads = 0;
  page.on("download", () => downloads++);
  await page.goto("/");
  await page.getByRole("button", { name: "Try it step by step" }).click();
  await expect(step(page)).toHaveText("Step 1 of 9");
  await expect(next(page)).toBeDisabled();
  await page.getByRole("button", { name: "Create my cast" }).click(); await settle(page);
  await expect(step(page)).toHaveText("Step 2 of 9");
  await expect(next(page)).toBeDisabled();
  await page.getByRole("button", { name: "Approve cast" }).click(); await settle(page);
  await expect(step(page)).toHaveText("Step 3 of 9");
  await next(page).click(); await settle(page);
  await page.getByRole("button", { name: "Use the edited line" }).click(); await settle(page);
  await expect(page.getByText("“A room with a sea view.”").first()).toBeVisible();
  await page.getByRole("button", { name: "Approve and draw" }).click(); await settle(page);
  await expect(step(page)).toHaveText("Step 6 of 9");
  await next(page).click(); await settle(page);
  await expect(step(page)).toHaveText("Step 7 of 9");
  await expect(next(page)).toBeDisabled();
  // keyboard alternative to dragging
  const balloon = page.getByRole("button", { name: /Speech balloon/ });
  await balloon.focus();
  for (let i = 0; i < 8; i++) await page.keyboard.press("ArrowUp");
  for (let i = 0; i < 4; i++) await page.keyboard.press("ArrowLeft");
  await expect(page.getByText("Clear of the face. Saved.")).toBeVisible();
  await expect(next(page)).toBeEnabled();
  // Back keeps progress
  await page.getByRole("button", { name: "Back" }).click();
  await expect(step(page)).toHaveText("Step 6 of 9");
  await next(page).click(); await settle(page);
  await next(page).click(); await settle(page);
  await expect(page.getByText("PDF", { exact: true })).toBeVisible();
  await next(page).click(); await settle(page);
  await expect(step(page)).toHaveText("Step 9 of 9");
  await expect(page.getByRole("link", { name: "Create your comic" })).toHaveAttribute("href", "/new");
  await expect(page.getByRole("button", { name: "Replay demo" })).toBeVisible();
  expect(downloads).toBe(0);
  expect(calls.filter((u) => !u.includes("/v1/meta"))).toEqual([]);
  const results = await new AxeBuilder({ page }).include("figure").withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.map((v) => `${v.id} (${v.nodes.length})`)).toEqual([]);
});

test("reduced motion starts on the finished comic, still and step-through", async ({ browser }) => {
  const page = await browser.newPage({ reducedMotion: "reduce" });
  await page.goto("/");
  await expect(step(page)).toHaveText("Step 9 of 9");
  await expect(page.getByRole("button", { name: "Pause" })).toHaveCount(0);
  await expect(page.getByText("Your idea. Your characters. Your comic.").first()).toBeVisible();
  await page.getByRole("button", { name: "Back" }).click();
  await expect(step(page)).toHaveText("Step 8 of 9");
  await page.close();
});

test("on a phone the headline and CTA come first and the demo is recomposed", async ({ browser }) => {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await page.goto("/");
  const h1 = await page.locator("main h1").boundingBox();
  const cta = await page.locator("main").getByRole("link", { name: "Start a comic" }).first().boundingBox();
  const demo = await page.locator("figure").boundingBox();
  expect(h1!.y).toBeLessThan(demo!.y);
  expect(cta!.y + cta!.height).toBeLessThan(844);
  expect(demo!.height).toBeGreaterThan(demo!.width); // portrait composition
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.close();
});

test("the capture route is never indexed", async ({ request }) => {
  const res = await request.get("/demo/capture?layout=portrait&t=10");
  expect(res.status()).toBe(200);
  expect(res.headers()["x-robots-tag"]).toContain("noindex");
  expect(await (await request.get("/robots.txt")).text()).toContain("/demo");
});
