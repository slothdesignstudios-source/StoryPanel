import { expect, test, type Page } from "@playwright/test";

const SHOTS = process.env.E2E_SHOTS;
const shot = async (page: Page, name: string) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true }); };

async function signIn(page: Page, email: string) {
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
}

test("prompt → cast approval → script approval → generation → edit → export", async ({ page }) => {
  const email = `e2e-${Date.now()}@example.com`;
  await signIn(page, email);
  await expect(page.getByText("Your first comic is one sentence away")).toBeVisible();
  await shot(page, "01-dashboard-empty");

  // --- wizard: moderation feedback, then a real idea
  await page.getByRole("link", { name: "Start a comic" }).click();
  await page.getByLabel("Your idea").fill("Batman opens a noodle stall in the rain");
  await page.getByRole("button", { name: "Create my cast" }).click();
  await expect(page.getByText(/original hero/i)).toBeVisible();
  await page.getByLabel("Your idea").fill("A shy robot who runs a night-time noodle stall learns to make friends. 2 pages, manga style.");
  await expect(page.getByText(/Starting holds 8 credits/)).toBeVisible();
  await shot(page, "02-wizard");
  await page.getByRole("button", { name: "Create my cast" }).click();

  // --- cast gate
  await expect(page.getByRole("heading", { name: "Meet the cast" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("heading", { name: "Kiro" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Kiro, hero portrait" })).toBeVisible();
  await shot(page, "03-cast");
  // reload mid-flow: the same gate comes back
  await page.reload();
  await expect(page.getByRole("heading", { name: "Meet the cast" })).toBeVisible();
  await page.getByLabel("Change request for Mei").fill("add a striped scarf");
  await page.getByRole("button", { name: "Revise" }).nth(1).click();
  await expect(page.getByText("Updating sheet")).toBeVisible();
  await expect(page.getByText("Updating sheet")).toBeHidden({ timeout: 60_000 });
  await page.getByRole("button", { name: "Approve cast and write the script" }).click();

  // --- script gate: edit, save as new version, approve exactly that version
  await expect(page.getByRole("heading", { name: "Review the script" })).toBeVisible({ timeout: 90_000 });
  const line = page.getByLabel("Line 2 text").first();
  await line.fill("I calculated a 97 percent chance you were hungry.");
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve and draw" })).toBeDisabled();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/Version 2 · Saved \(edited by you\)/)).toBeVisible();
  await shot(page, "04-script");
  await page.getByRole("button", { name: "Approve and draw" }).click();
  await expect(page.getByRole("dialog")).toContainText(/holds 9 credits/);
  await page.getByRole("dialog").getByRole("button", { name: "Approve and draw" }).click();

  // --- progressive generation, then the editor
  await expect(page.getByRole("button", { name: "Export" })).toBeVisible({ timeout: 120_000 });
  await expect(page.getByRole("navigation", { name: "Pages" }).getByRole("button")).toHaveCount(2);
  await shot(page, "05-editor");

  // edit a balloon through the accessible list (no pointer needed)
  await page.getByRole("button", { name: /speech: I calculated a 97 percent/ }).click();
  await page.getByLabel("Text").fill("Edited in the editor.");
  await expect(page.getByText("All changes saved")).toBeVisible();
  await page.getByRole("button", { name: "Move right" }).click();
  await page.getByRole("button", { name: "Make larger" }).click();
  await expect(page.getByText("All changes saved")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: /speech: Edited in the editor\./ })).toBeVisible();

  // regenerate a panel and compare versions
  await page.getByRole("button", { name: "Panel 2", exact: true }).click();
  await page.getByRole("button", { name: "Regenerate" }).click();
  await expect(page.getByText("Redrawing").first()).toBeVisible();
  await expect(page.getByText("Redrawing")).toHaveCount(0, { timeout: 60_000 });
  await page.getByRole("button", { name: "Versions" }).click();
  await expect(page.getByRole("dialog").getByRole("listitem")).toHaveCount(2);
  await shot(page, "06-versions");
  await page.getByRole("dialog").getByRole("button", { name: "Use this version" }).click();
  await page.keyboard.press("Escape");

  // layout change
  await page.getByLabel("Page layout").selectOption({ index: 1 });
  await expect(page.getByLabel("Page layout")).toBeEnabled();

  // export
  await page.getByRole("button", { name: "Export" }).click();
  await page.getByRole("button", { name: /^PDF/ }).click();
  const download = page.getByRole("dialog").getByRole("link", { name: "Download" });
  await expect(download).toBeVisible({ timeout: 60_000 });
  const res = await page.request.get((await download.getAttribute("href"))!);
  expect(res.status()).toBe(200);
  expect((await res.body()).subarray(0, 5).toString()).toBe("%PDF-");
  await page.keyboard.press("Escape");

  // reader and billing
  await page.getByRole("link", { name: "Read" }).click();
  await expect(page.getByText("Page 1 of 2")).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByText("Page 2 of 2")).toBeVisible();
  await shot(page, "07-reader");
  await page.goto("/billing");
  await expect(page.getByRole("cell", { name: "Cast and reference sheets" })).toBeVisible();
  await expect(page.getByText("Purchases aren't switched on")).toBeVisible();
  await shot(page, "08-billing");

  // another account cannot open this comic
  await page.goto("/dashboard");
  const href = await page.getByRole("link", { name: /Noodles After Midnight/ }).getAttribute("href");
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, `other-${Date.now()}@example.com`);
  await page.goto(href!);
  await expect(page.getByText("We can't find that comic")).toBeVisible();
});
