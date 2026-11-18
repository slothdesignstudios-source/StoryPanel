import type { MetadataRoute } from "next";
import { SAMPLES } from "@/content/samples";
import { SITE_URL } from "@/lib/site";

/** Public pages only. Samples are listed once they are approved for publication. */
export default function sitemap(): MetadataRoute.Sitemap {
  const pages = ["/", "/gallery", "/pricing"].map((p) => ({ url: `${SITE_URL}${p}`, changeFrequency: "weekly" as const, priority: p === "/" ? 1 : 0.7 }));
  const samples = SAMPLES.filter((s) => s.approved).map((s) => ({ url: `${SITE_URL}/gallery/${s.slug}`, changeFrequency: "monthly" as const, priority: 0.5 }));
  return [...pages, ...samples];
}
