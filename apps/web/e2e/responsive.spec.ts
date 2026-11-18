import { expect, test } from "@playwright/test";

test("core screens work at phone width without horizontal scrolling", async ({ page }) => {
  for (const path of ["/", "/gallery", "/pricing", "/gallery/the-last-train-home"]) {
    await page.goto(path);
    await expect(page.locator("main h1")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${path} overflows`).toBeLessThanOrEqual(1);
  }
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(`m-${Date.now()}@example.com`);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
  for (const path of ["/dashboard", "/new", "/billing", "/settings"]) {
    await page.goto(path);
    await expect(page.locator("main h1")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, `${path} overflows`).toBeLessThanOrEqual(1);
  }
  // keyboard: the skip link is the first tab stop
  await page.goto("/new");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
});
