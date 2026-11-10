import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";

export async function signIn(page: Page, email: string) {
  await page.goto("/sign-in");
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your comics" })).toBeVisible();
}

export async function apiToken(request: APIRequestContext, email: string): Promise<string> {
  const r = await request.post(`${API}/v1/dev/login`, { data: { email } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()).token as string;
}

type Project = { id: string; status: string; cast_version: number; script_version: number; pages: { preview_url: string | null; size: number[]; panels: { balloons: { id: string; type: string; text: string; geometry: { x: number; y: number; w: number; h: number } }[] }[] }[] };

/** Create a comic through the API and drive it through both gates to completion. */
export async function finishedComic(request: APIRequestContext, token: string, prompt: string): Promise<Project> {
  const h = { Authorization: `Bearer ${token}` };
  const get = async (id: string) => (await (await request.get(`${API}/v1/projects/${id}`, { headers: h })).json()) as Project;
  const until = async (id: string, ok: (p: Project) => boolean) => {
    for (let i = 0; i < 300; i++) {
      const p = await get(id);
      if (ok(p)) return p;
      if (["failed", "blocked", "cancelled", "expired"].includes(p.status)) throw new Error(`comic ended ${p.status}`);
      await new Promise((r) => setTimeout(r, 300));
    }
    throw new Error("timed out");
  };
  const created = await (await request.post(`${API}/v1/projects`, { headers: h, data: { prompt } })).json();
  let p = await until(created.id, (x) => x.status === "awaiting_cast_approval");
  await request.post(`${API}/v1/projects/${p.id}/characters/approve`, { headers: h, data: { cast_version: p.cast_version } });
  p = await until(p.id, (x) => x.status === "awaiting_script_approval");
  await request.post(`${API}/v1/projects/${p.id}/script/approve`, { headers: h, data: { script_version: p.script_version } });
  return until(p.id, (x) => x.status === "completed" && x.pages.every((pg) => pg.preview_url));
}
