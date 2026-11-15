import { expect, test } from "@playwright/test";
import { API, apiToken, finishedComic, signIn } from "./helpers";

/** Lettering is never set below the readable size: shrinking a balloon until its text cannot fit
 *  surfaces an overflow (shorten, enlarge or move) and the stored size stays at the floor. */
test("a balloon too small for its text is flagged, not shrunk below the readable size", async ({ page, request }) => {
  const email = `e2e-legible-${Date.now()}@example.com`;
  const token = await apiToken(request, email);
  const comic = await finishedComic(request, token, "A robot and a girl share noodles in the rain, as a 4 panel strip");
  await signIn(page, email);
  await page.goto(`/projects/${comic.id}`);
  const tools = page.getByRole("complementary", { name: "Editing tools" });
  const speech = tools.getByRole("button", { name: /^speech:/ }).first();
  const label = (await speech.textContent())!.replace(/^speech:\s*/, "");
  await speech.click();
  for (let i = 0; i < 14; i++) await tools.getByRole("button", { name: "Make smaller" }).click();
  await expect(tools.getByText("Text doesn't fit at a readable size")).toBeVisible({ timeout: 15_000 });
  await expect(tools.getByRole("note").filter({ hasText: "smallest readable size" })).toContainText("Shorten the text, make the balloon larger, or move it");
  await expect(tools.getByRole("button", { name: /^speech:/ }).filter({ hasText: "doesn't fit" }).filter({ hasText: label })).toBeVisible();
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/lettering-overflow.png`, fullPage: true });

  const h = { Authorization: `Bearer ${token}` };
  const p = await (await request.get(`${API}/v1/projects/${comic.id}`, { headers: h })).json();
  const b = p.pages.flatMap((pg: { panels: { balloons: { text: string; flags: string[]; geometry: { text_size: number; w: number } }[] }[] }) => pg.panels.flatMap((x) => x.balloons)).find((x: { text: string }) => x.text === label);
  expect(b.flags).toContain("overflow");
  expect(b.geometry.text_size).toBe(15);
});
