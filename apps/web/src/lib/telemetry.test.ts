import { describe, expect, it } from "vitest";
import { scrubBreadcrumb, scrubEvent, stripUrl } from "./telemetry";

const signed = "http://localhost:9000/storypanel-assets/users/u1/projects/p1/panel/x.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA123%2F20261111&X-Amz-Signature=deadbeef";

describe("browser telemetry scrubbing", () => {
  it("keeps only origin and path of URLs", () => {
    expect(stripUrl(signed)).toBe("http://localhost:9000/storypanel-assets/users/u1/projects/p1/panel/x.png");
  });

  it("removes signed-URL credentials, tokens, emails, headers, user and console output", () => {
    const event = scrubEvent({
      request: { url: signed, headers: { Authorization: "Bearer eyJabc.def.ghi" }, cookies: "s=1", data: "{}" },
      user: { email: "ada@example.com" },
      message: `failed to load ${signed} for ada@example.com with Bearer eyJabc.def.ghi`,
      exception: { values: [{ value: `GET ${signed} 403` }] },
      breadcrumbs: [
        { category: "fetch", data: { url: signed, method: "GET", Authorization: "Bearer x" } },
        { category: "console", message: "token eyJ..." },
        { category: "navigation", data: { from: "/projects/p1?x=1", to: "/billing?checkout=success" } },
      ],
      extra: { body: "secret" },
    });
    const text = JSON.stringify(event);
    for (const leak of ["X-Amz", "AKIA123", "deadbeef", "eyJabc", "ada@example.com", "Authorization", "cookies", "secret", "checkout=success"]) {
      expect(text).not.toContain(leak);
    }
    expect(event.breadcrumbs).toHaveLength(2);
    expect(scrubBreadcrumb({ category: "console" })).toBeNull();
  });
});
