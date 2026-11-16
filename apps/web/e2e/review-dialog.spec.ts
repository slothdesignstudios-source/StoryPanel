import { expect, test, type Page } from "@playwright/test";
import { API, apiToken } from "./helpers";

/** The approval dialog's confirmations: labelled, unchecked at start and on every reopen, cleared
 *  when the items change, and sent as exactly the current keys. Review items are injected into the
 *  project response (simulated judgments aren't available to the shared stack); the server-side
 *  binding of keys to content is tested in tests/test_story_fidelity.py. */
const item = (key: string, panel: number) => ({
  key, message: `Panel ${panel}: can't tell whether it keeps “Kiro wears the red apron”: hidden by the counter.`,
  kind: "Couldn't confirm a continuity rule is kept", page: 1, panel, rule_id: "apron_always",
  rule_text: "Kiro wears the red apron", beat: null, character: null, issue: "can't tell whether it keeps “Kiro wears the red apron”: hidden by the counter.",
});

async function scriptGate(page: Page, request: Parameters<typeof apiToken>[0], email: string) {
  const token = await apiToken(request, email);
  const h = { Authorization: `Bearer ${token}` };
  const created = await (await request.post(`${API}/v1/projects`, { headers: h, data: { prompt: "A robot and a girl share noodles in the rain, as a 4 panel strip" } })).json();
  const get = async () => (await (await request.get(`${API}/v1/projects/${created.id}`, { headers: h })).json()) as { status: string; cast_version: number };
  let p = await get();
  for (let i = 0; i < 200 && p.status !== "awaiting_cast_approval"; i++) { await page.waitForTimeout(300); p = await get(); }
  await request.post(`${API}/v1/projects/${created.id}/characters/approve`, { headers: h, data: { cast_version: p.cast_version } });
  for (let i = 0; i < 200 && p.status !== "awaiting_script_approval"; i++) { await page.waitForTimeout(300); p = await get(); }
  return created.id as string;
}

test("review confirmations start unchecked, reset on reopen and on change, and send current keys", async ({ page, request }) => {
  const email = `e2e-review-${Date.now()}@example.com`;
  const pid = await scriptGate(page, request, email);
  let items = [item("rule_uncertain@pages[0].panels[0].rule:apron_always#aaa", 1), item("rule_uncertain@pages[0].panels[1].rule:apron_always#bbb", 2)];
  await page.route(`**/v1/projects/${pid}`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    if (body.script) body.script.review_required = items;
    await route.fulfill({ response: res, json: body });
  });
  let sent: string[] | null = null;
  await page.route(`**/v1/projects/${pid}/script/approve`, async (route) => {
    sent = (route.request().postDataJSON() as { acknowledged: string[] }).acknowledged;
    await route.fulfill({ status: 200, json: { request_id: "test" } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto(`/projects/${pid}`);
  await expect(page.getByRole("heading", { name: "Review the script" })).toBeVisible();

  const dialog = page.getByRole("dialog");
  const open = async () => { await page.getByRole("button", { name: "Approve and draw" }).click(); await expect(dialog).toBeVisible(); };
  await open();
  await expect(dialog.getByText("Page 1 · Panel 1:")).toBeVisible();
  await expect(dialog.getByText("Page 1 · Panel 2:")).toBeVisible();
  await expect(dialog.getByText("Rule: “Kiro wears the red apron”").first()).toBeVisible();
  const boxes = dialog.getByRole("checkbox");
  await expect(boxes).toHaveCount(2);
  for (const b of await boxes.all()) await expect(b).not.toBeChecked();
  const go = dialog.getByRole("button", { name: "Approve and draw" });
  await expect(go).toBeDisabled();
  await boxes.nth(0).check();
  await expect(go).toBeDisabled(); // every item needs its own confirmation
  await boxes.nth(1).check();
  await expect(go).toBeEnabled();

  // close and reopen: confirmations start over
  await dialog.getByRole("button", { name: "Not yet" }).click();
  await open();
  for (const b of await dialog.getByRole("checkbox").all()) await expect(b).not.toBeChecked();

  // another tab saves an edit while this dialog is open: live updates refetch, the items change,
  // and the ticks made for the old items are cleared in place
  await dialog.getByRole("checkbox").nth(0).check();
  items = [item("rule_uncertain@pages[0].panels[0].rule:apron_always#ccc", 1)];
  const h = { Authorization: `Bearer ${await apiToken(request, email)}` };
  const cur = await (await request.get(`${API}/v1/projects/${pid}`, { headers: h })).json();
  cur.script.content.pages[0].panels[0].mood = "edited in another tab";
  const saved = await request.patch(`${API}/v1/projects/${pid}/script`, { headers: h, data: { base_version: cur.script_version, script: cur.script.content } });
  expect(saved.ok()).toBeTruthy();
  await expect(dialog.getByRole("checkbox")).toHaveCount(1, { timeout: 15_000 });
  await expect(dialog.getByRole("checkbox")).not.toBeChecked();
  await expect(dialog.getByRole("button", { name: "Approve and draw" })).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Approve and draw" }).click();
  await expect.poll(() => sent).toEqual(["rule_uncertain@pages[0].panels[0].rule:apron_always#ccc"]);
});

test("a cast approval rejected by a stale page shows the flagged items and reloads into the dialog", async ({ page, request }) => {
  const email = `e2e-stale-cast-${Date.now()}@example.com`;
  const token = await apiToken(request, email);
  const h = { Authorization: `Bearer ${token}` };
  const created = await (await request.post(`${API}/v1/projects`, { headers: h, data: { prompt: "A robot and a girl share noodles in the rain, as a 4 panel strip" } })).json();
  const pid = created.id as string;
  for (let i = 0; i < 200; i++) {
    const p = await (await request.get(`${API}/v1/projects/${pid}`, { headers: h })).json();
    if (p.status === "awaiting_cast_approval" && p.characters.every((c: { sheet_check: { status?: string } | null }) => c.sheet_check?.status !== "pending")) break;
    await page.waitForTimeout(300);
  }
  const flag = "Kiro's reference sheet was flagged: Kiro carries a glowing lantern in the fog: no lantern in the side view.";
  let flagged = false; // the page first loads before the review exists (as if live updates were missed)
  await page.route(`**/v1/projects/${pid}`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    if (flagged) body.characters[0].review = { key: "sheet:kiro:v1#abc", message: flag, kind: "Reference sheet flagged", character: body.characters[0].name, issue: "no lantern in the side view", page: null, panel: null, rule_id: null, rule_text: null, beat: null };
    await route.fulfill({ response: res, json: body });
  });
  await page.route(`**/v1/projects/${pid}/characters/approve`, async (route) => {
    flagged = true;
    await route.fulfill({ status: 422, json: { error: { code: "validation_error", message: "Review the flagged items and confirm to approve the cast anyway.", retryable: false, request_id: "t", fields: [{ field: "acknowledged", message: flag }] } } });
  });
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto(`/projects/${pid}`);
  const reloaded = page.waitForResponse((r) => r.url().endsWith(`/v1/projects/${pid}`) && flagged);
  await page.getByRole("button", { name: "Approve cast and write the script" }).click();
  await reloaded;
  await expect(page.getByText(flag).first()).toBeVisible(); // the reason is shown, not only the summary
  await expect(page.getByText("This page was out of date and has been reloaded")).toBeVisible();
  await page.getByRole("button", { name: "Approve cast and write the script" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("checkbox")).toHaveCount(1);
  await expect(dialog.getByRole("button", { name: "Approve anyway" })).toBeDisabled();
});
