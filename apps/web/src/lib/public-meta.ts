import type { Meta } from "./types";

/** The public product catalogue (`GET /v1/meta`): credit prices, styles and limits, straight from the
 *  backend's configuration. Public pages never hard-code prices; when the API can't be reached they say
 *  so instead of showing numbers. Cached for five minutes. */
export async function getPublicMeta(): Promise<Meta | null> {
  const base = process.env.SP_INTERNAL_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  try {
    const res = await fetch(`${base}/v1/meta`, { next: { revalidate: 300 }, signal: AbortSignal.timeout(2500) });
    if (!res.ok) return null;
    return (await res.json()) as Meta;
  } catch {
    return null;
  }
}

/** Worked examples, computed from the live prices (never from copies of them). */
export function examples(p: Record<string, number>) {
  const start = (p.cast ?? 0) + (p.script ?? 0);
  return {
    start,
    strip4: start + 4 * (p.panel ?? 0),
    page4: start + 4 * (p.panel ?? 0),
    page6: start + 6 * (p.panel ?? 0),
  };
}
