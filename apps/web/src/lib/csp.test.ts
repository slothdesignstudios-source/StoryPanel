import { afterEach, describe, expect, it, vi } from "vitest";

async function cspWith(env: Record<string, string | undefined>): Promise<string> {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v as string);
  return (await import("../../next.config")).csp;
}

describe("content security policy", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("allows the production Clerk Frontend API origin when configured", async () => {
    const csp = await cspWith({ NEXT_PUBLIC_AUTH_MODE: "clerk", NEXT_PUBLIC_CLERK_FRONTEND_API: "https://clerk.storypanel.example" });
    expect(csp).toMatch(/script-src[^;]*https:\/\/clerk\.storypanel\.example/);
    expect(csp).toMatch(/connect-src[^;]*https:\/\/clerk\.storypanel\.example/);
    expect(csp).not.toContain("clerk.accounts.dev");
  });

  it("defaults to development-instance hosts, and none at all in dev sign-in mode", async () => {
    expect(await cspWith({ NEXT_PUBLIC_AUTH_MODE: "clerk", NEXT_PUBLIC_CLERK_FRONTEND_API: undefined })).toContain("https://*.clerk.accounts.dev");
    expect(await cspWith({ NEXT_PUBLIC_AUTH_MODE: "dev" })).not.toContain("clerk");
  });
});
