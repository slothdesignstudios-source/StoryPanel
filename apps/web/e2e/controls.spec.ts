import { expect, test } from "@playwright/test";
import { API, apiToken } from "./helpers";

/** Script review controls show their whole value: no clipped select labels, no hidden lettering
 *  text. Measured in the browser (rendered text width against the control), not from class names. */
test("script review controls are not clipped", async ({ page, request }) => {
  const email = `e2e-controls-${Date.now()}@example.com`;
  const token = await apiToken(request, email);
  const h = { Authorization: `Bearer ${token}` };
  const created = await (await request.post(`${API}/v1/projects`, { headers: h, data: { prompt: "A robot and a girl share noodles in the rain, as a 4 panel strip" } })).json();
  const get = async () => (await (await request.get(`${API}/v1/projects/${created.id}`, { headers: h })).json()) as { status: string; cast_version: number };
  let p = await get();
  for (let i = 0; i < 200 && p.status !== "awaiting_cast_approval"; i++) { await page.waitForTimeout(300); p = await get(); }
  await request.post(`${API}/v1/projects/${created.id}/characters/approve`, { headers: h, data: { cast_version: p.cast_version } });
  for (let i = 0; i < 200 && p.status !== "awaiting_script_approval"; i++) { await page.waitForTimeout(300); p = await get(); }

  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto(`/projects/${created.id}`);
  await expect(page.getByRole("heading", { name: "Review the script" })).toBeVisible();
  const first = page.locator("fieldset").filter({ hasText: "Panel 1" }).first();
  await first.getByLabel("Line 1 type").selectOption("caption");
  await first.getByLabel("Line 2 type").selectOption("thought");
  const long = "Meanwhile, on the windiest pier in the whole harbour, a very determined gull keeps watch over lunch.";
  await first.getByLabel("Line 1 text").fill(long);

  const clipped = await page.evaluate(() => {
    const ctx = document.createElement("canvas").getContext("2d")!;
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll("fieldset select")) as HTMLSelectElement[]) {
      const cs = getComputedStyle(el);
      ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      const text = el.selectedOptions[0]?.text ?? "";
      // Chrome's native dropdown arrow takes room beyond the padding (the earlier screenshot showed
      // "Captio" where padding alone said it fits), so allow 20px for it.
      const arrow = cs.appearance === "none" ? 0 : 20;
      const need = ctx.measureText(text).width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + arrow;
      if (need > el.clientWidth + 1) out.push(`${el.getAttribute("aria-label") ?? el.id}: "${text}" needs ${Math.ceil(need)}px, has ${el.clientWidth}px`);
    }
    for (const el of Array.from(document.querySelectorAll("fieldset textarea")) as HTMLTextAreaElement[]) {
      if (el.scrollHeight > el.clientHeight + 2) out.push(`${el.getAttribute("aria-label") ?? el.id}: text hidden (${el.scrollHeight} > ${el.clientHeight})`);
    }
    return out;
  });
  expect(clipped).toEqual([]);
});
