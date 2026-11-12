/**
 * Scrubbing for browser error reports. Signed asset URLs carry credentials in their query
 * (X-Amz-Credential, X-Amz-Signature) and API URLs can carry ids; request headers can carry
 * the session token. Reports keep only origin + path, never headers, cookies, bodies or user.
 */

const SECRETISH = /(authorization|cookie|token|secret|signature|credential|password|session)/i;

export function stripUrl(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  try {
    const u = new URL(raw, "http://local.invalid");
    const kept = u.origin === "http://local.invalid" ? u.pathname : `${u.origin}${u.pathname}`;
    return kept;
  } catch {
    return raw.split("?")[0];
  }
}

function scrubText(s: string): string {
  return s
    .replace(/Bearer\s+[A-Za-z0-9._-]+/g, "Bearer [redacted]")
    .replace(/(https?:\/\/[^\s"'?]+)\?[^\s"']*/g, "$1")
    .replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, "[email]");
}

type Event = {
  request?: { url?: string; headers?: unknown; cookies?: unknown; data?: unknown; query_string?: unknown };
  user?: unknown;
  breadcrumbs?: Breadcrumb[];
  message?: string;
  exception?: { values?: { value?: string }[] };
  extra?: Record<string, unknown>;
};
type Breadcrumb = { category?: string; message?: string; data?: Record<string, unknown> };

export function scrubBreadcrumb<B extends Breadcrumb>(b: B): B | null {
  if (b.category === "console") return null; // console output may echo anything
  const data = b.data ? { ...b.data } : undefined;
  if (data) {
    for (const k of Object.keys(data)) {
      if (SECRETISH.test(k)) delete data[k];
      else if (k === "url" || k === "from" || k === "to") data[k] = stripUrl(data[k]);
    }
  }
  return { ...b, message: b.message ? scrubText(b.message) : b.message, data };
}

export function scrubEvent<E extends Event>(e: E): E {
  const out: E = { ...e };
  delete out.user;
  if (out.request) out.request = { url: stripUrl(out.request.url) as string | undefined };
  if (out.message) out.message = scrubText(out.message);
  if (out.exception?.values) out.exception = { values: out.exception.values.map((v) => ({ ...v, value: v.value ? scrubText(v.value) : v.value })) };
  if (out.breadcrumbs) out.breadcrumbs = out.breadcrumbs.map(scrubBreadcrumb).filter((b): b is Breadcrumb => b !== null);
  delete out.extra;
  return out;
}
