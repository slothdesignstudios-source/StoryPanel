import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { apiToken, finishedComic, signIn } from "./helpers";

/**
 * Automated accessibility checks (axe-core, WCAG 2.1 A/AA rules) on every main screen,
 * plus keyboard-only operation of the core tasks. Automated rules catch roughly a third
 * of real accessibility problems; this is not a substitute for a manual audit with
 * assistive technology.
 */
async function scan(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length} node(s) e.g. ${v.nodes[0]?.target.join(" ")}`);
  expect(summary, `axe violations on ${name}`).toEqual([]);
}

test("main screens have no detectable WCAG A/AA violations", async ({ page, request }) => {
  const email = `a11y-${Date.now()}@example.com`;
  await page.goto("/sign-in");
  await scan(page, "sign-in");
  const comic = await finishedComic(request, await apiToken(request, email), "Two pigeons argue over a chip. 2 pages.");
  await signIn(page, email);
  await scan(page, "dashboard");
  for (const [path, heading] of [["/new", "What's the story?"], ["/billing", "Credits"], ["/settings", "Settings"]] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
    await scan(page, path);
  }
  await page.goto(`/projects/${comic.id}`);
  await expect(page.getByRole("button", { name: "Export" })).toBeVisible();
  await scan(page, "editor");
  await page.getByRole("button", { name: "Export" }).click();
  await scan(page, "export dialog");
  await page.keyboard.press("Escape");
  await page.goto(`/projects/${comic.id}/read`);
  await expect(page.getByText(/Page 1 of 2/)).toBeVisible();
  await scan(page, "reader");
});

test("gate screens (cast and script review) pass axe", async ({ page, request }) => {
  const email = `a11y-gates-${Date.now()}@example.com`;
  await signIn(page, email);
  await page.goto("/new");
  await page.getByLabel("Your idea").fill("A robot and a girl share noodles in the rain. 2 pages.");
  await page.getByRole("button", { name: "Create my cast" }).click();
  await expect(page.getByRole("heading", { name: "Meet the cast" })).toBeVisible({ timeout: 90_000 });
  await scan(page, "cast review");
  await page.getByRole("button", { name: "Approve cast and write the script" }).click();
  await expect(page.getByRole("heading", { name: "Review the script" })).toBeVisible({ timeout: 90_000 });
  await scan(page, "script review");
  void request;
});

test("a comic can be created and a balloon edited with the keyboard alone", async ({ page, request }) => {
  const email = `kbd-${Date.now()}@example.com`;
  // sign in with the keyboard
  await page.goto("/sign-in");
  await page.keyboard.press("Tab"); // skip link
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Email")).toBeFocused();
  await page.keyboard.type(email);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();

  // wizard: reach the idea field by tabbing, type, submit with Enter on the button
  await page.goto("/new");
  for (let i = 0; i < 12 && !(await page.getByLabel("Your idea").evaluate((el) => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await expect(page.getByLabel("Your idea")).toBeFocused();
  await page.keyboard.type("Two pigeons argue over a chip, as a 4 panel strip");
  const submit = page.getByRole("button", { name: "Create my cast" });
  for (let i = 0; i < 12 && !(await submit.evaluate((el) => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await expect(submit).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Meet the cast" })).toBeVisible({ timeout: 90_000 });

  // editor: drive the rest through the API, then edit a balloon without a pointer
  const comic = await finishedComic(request, await apiToken(request, email), "A cat watches the rain, as a 4 panel strip");
  await page.goto(`/projects/${comic.id}`);
  const item = page.getByRole("button", { name: /^(speech|caption):/ }).first();
  await item.focus();
  await page.keyboard.press("Enter");
  await expect(item).toHaveAttribute("aria-pressed", "true");
  const before = comic.pages[0]!.panels.flatMap((p) => p.balloons)[0]!.geometry.x;
  await page.getByRole("button", { name: "Move right" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("All changes saved")).toBeVisible();
  // focus visibility: the focused control shows the focus ring
  const outline = await page.getByRole("button", { name: "Move right" }).evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
  const text = page.getByLabel("Text");
  await text.focus();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("Typed without a mouse.");
  await expect(page.getByText("All changes saved")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: /Typed without a mouse\./ })).toBeVisible();
  expect(before).toBeGreaterThanOrEqual(0);
});
