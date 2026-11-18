/** The public origin used for canonical URLs, the sitemap and social previews. Set
 *  NEXT_PUBLIC_SITE_URL at build time for a real deployment. */
export const SITE_URL = (process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000").replace(/\/$/, "");

/** Account and project pages: never indexed (robots.txt, X-Robots-Tag and page metadata). */
export const PRIVATE_PATHS = ["/dashboard", "/new", "/projects", "/billing", "/settings", "/sign-in"];
