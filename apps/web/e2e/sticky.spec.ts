import { expect, test } from "@playwright/test";
import { API, apiToken } from "./helpers";

/** No script field, and no validation message, can end up hidden behind the sticky approval bar
 *  or the sticky header when it receives keyboard focus or is scrolled into view. */
test("script fields and messages are never hidden by sticky bars", async ({ page, request }) => {
  const email = `e2e-sticky-${Date.now()}@example.com`;
  const token = await apiToken(request, email);
  const h = { Authorization: `Bearer ${token}` };
  const created = await (await request.post(`${API}/v1/projects`, { headers: h, data: { prompt: "A robot and a girl share noodles in the rain, as a 4 panel strip" } })).json();
  const get = async () => (await (await request.get(`${API}/v1/projects/${created.id}`, { headers: h })).json());
  let p = await get();
  for (let i = 0; i < 200 && p.status !== "awaiting_cast_approval"; i++) { await page.waitForTimeout(300); p = await get(); }
  await request.post(`${API}/v1/projects/${created.id}/characters/approve`, { headers: h, data: { cast_version: p.cast_version } });
  for (let i = 0; i < 200 && p.status !== "awaiting_script_approval"; i++) { await page.waitForTimeout(300); p = await get(); }
  // a saved version with an error on the LAST panel, so a validation message sits at the bottom
  const script = p.script.content;
  const last = script.pages[0].panels[script.pages[0].panels.length - 1];
  last.dialogue[0].text = Array(30).fill("word").join(" ");
  await request.patch(`${API}/v1/projects/${created.id}/script`, { headers: h, data: { base_version: p.script_version, script } });

  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto(`/projects/${created.id}`);
  await expect(page.getByRole("heading", { name: "Review the script" })).toBeVisible();
  await expect(page.locator("fieldset [role=alert]").first()).toBeVisible();

  const hidden = await page.evaluate(async () => {
    const bar = document.querySelector("[data-sticky-approval]") as HTMLElement | null ?? document.querySelector(".sticky.bottom-4") as HTMLElement | null;
    const header = document.querySelector("header") as HTMLElement | null;
    const out: string[] = [];
    const targets = [...document.querySelectorAll("fieldset input, fieldset select, fieldset textarea, fieldset button")] as HTMLElement[];
    const messages = [...document.querySelectorAll("fieldset [role=alert]")] as HTMLElement[];
    const check = (el: HTMLElement, what: string) => {
      const r = el.getBoundingClientRect();
      const b = bar?.getBoundingClientRect();
      const top = header?.getBoundingClientRect().bottom ?? 0;
      const covered = (b && r.bottom > b.top + 1 && r.top < b.bottom) || r.top < top - 1 || r.bottom > window.innerHeight + 1;
      if (covered) out.push(`${what} ${el.getAttribute("aria-label") ?? el.tagName} at ${Math.round(r.top)}-${Math.round(r.bottom)}, bar ${Math.round(b?.top ?? 0)}, header ${Math.round(top)}, viewport ${window.innerHeight}`);
    };
    for (const el of targets) { el.focus(); await new Promise((r) => setTimeout(r, 30)); check(el, "focused"); }
    for (const el of messages) { el.scrollIntoView({ block: "nearest" }); await new Promise((r) => setTimeout(r, 30)); check(el, "message"); }
    return out;
  });
  expect(hidden).toEqual([]);
});
