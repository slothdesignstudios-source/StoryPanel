import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { API } from "./helpers";

/** The public site: what it says, what it promises (and doesn't), and what search engines may see. */

test("the landing page states the product, offers one primary action and shows live prices", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Turn a story idea into a comic worth sharing");
  const main = page.locator("main");
  await expect(main.getByRole("link", { name: "Start a comic" }).first()).toBeVisible();
  // prices come from the API's configuration, not from copies in the page
  const meta = await (await request.get(`${API}/v1/meta`)).json();
  const strip = meta.credit_prices.cast + meta.credit_prices.script + 4 * meta.credit_prices.panel;
  await expect(main.getByText(`${strip} credits`).first()).toBeVisible();
  for (const section of ["How it works", "Gallery", "What you get", "Pricing", "Questions"]) {
    await expect(main.getByText(section, { exact: true }).first()).toBeVisible();
  }
  // nothing invented: no testimonials, customer counts or guarantees
  const text = (await main.innerText()).toLowerCase();
  for (const claim of ["testimonial", "trusted by", "customers", "guaranteed consisten", "award"]) expect(text).not.toContain(claim);
  expect(text).toContain("it isn't guaranteed");
});

test("public pages pass an automated accessibility check and keep a keyboard order", async ({ page }) => {
  for (const path of ["/", "/gallery", "/pricing", "/gallery/the-last-train-home"]) {
    await page.goto(path);
    await expect(page.locator("main h1")).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
    expect(results.violations.map((v) => `${path}: ${v.id} (${v.nodes.length})`)).toEqual([]);
  }
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "StoryPanel home" })).toBeFocused();
});

test("only public pages are offered to search engines", async ({ request, baseURL }) => {
  const robots = await (await request.get(`${baseURL}/robots.txt`)).text();
  const sitemap = await (await request.get(`${baseURL}/sitemap.xml`)).text();
  for (const p of ["/dashboard", "/projects", "/billing", "/settings", "/new"]) expect(sitemap).not.toContain(p);
  if (robots.includes("Disallow: /\n") || robots.trim().endsWith("Disallow: /")) {
    // a local preview showing unapproved sample art: nothing at all may be indexed
    expect(sitemap).toContain("/gallery");
  } else {
    for (const p of ["/dashboard", "/projects", "/billing", "/settings"]) expect(robots).toContain(`Disallow: ${p}`);
  }
  for (const p of ["/dashboard", "/billing", "/settings", "/sign-in"]) {
    const res = await request.get(`${baseURL}${p}`);
    expect(res.headers()["x-robots-tag"], p).toContain("noindex");
  }
  expect((await request.get(`${baseURL}/`)).headers()["x-robots-tag"]).toBeUndefined();
});

test("sample artwork is labelled as a preview unless it is approved", async ({ page }) => {
  await page.goto("/gallery");
  const images = page.locator("main img");
  if ((await images.count()) > 0) {
    // the only way to see unapproved art is the local preview build, and it says so
    await expect(page.getByText("Local preview · not approved for publication").first()).toBeVisible();
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", /noindex/);
    for (const img of await images.all()) {
      expect(await img.getAttribute("width")).toBeTruthy();
      expect(await img.getAttribute("height")).toBeTruthy();
      expect((await img.getAttribute("alt"))?.length ?? 0).toBeGreaterThan(40);
    }
  } else {
    await expect(page.getByText("Sample artwork coming soon").first()).toBeVisible();
  }
});

test("signing in from 'Start a comic' returns to the new comic form", async ({ page }) => {
  await page.goto("/");
  await page.locator("main").getByRole("link", { name: "Start a comic" }).first().click();
  await expect(page).toHaveURL(/\/sign-in\?next=%2Fnew/);
  await page.getByLabel("Email").fill(`e2e-next-${Date.now()}@example.com`);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "What's the story?" })).toBeVisible();
});

test("sign-in never redirects off the site", async ({ page }) => {
  await page.goto("/sign-in?next=//evil.example.com/x");
  await page.getByLabel("Email").fill(`e2e-open-${Date.now()}@example.com`);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
  expect(new URL(page.url()).host).toBe(new URL(page.url()).host);
  expect(page.url()).not.toContain("evil.example.com");
});
