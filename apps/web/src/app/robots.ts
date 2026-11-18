import type { MetadataRoute } from "next";
import { anyUnapprovedShown } from "@/content/samples";
import { PRIVATE_PATHS, SITE_URL } from "@/lib/site";

export default function robots(): MetadataRoute.Robots {
  // A local preview that shows unapproved sample art is never indexable.
  if (anyUnapprovedShown()) return { rules: [{ userAgent: "*", disallow: "/" }] };
  return { rules: [{ userAgent: "*", allow: "/", disallow: PRIVATE_PATHS }], sitemap: `${SITE_URL}/sitemap.xml` };
}
