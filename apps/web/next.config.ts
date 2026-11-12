import type { NextConfig } from "next";

const api = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
// Asset hosts the browser may load signed images from (S3/R2 endpoint), space separated.
const assets = process.env.NEXT_PUBLIC_ASSET_ORIGINS ?? "http://localhost:9000 http://127.0.0.1:9000";
const clerk = process.env.NEXT_PUBLIC_AUTH_MODE === "clerk";
// Clerk Frontend API origin: production instances serve it from your own domain
// (e.g. https://clerk.example.com); development instances from *.clerk.accounts.dev.
const clerkFapi = process.env.NEXT_PUBLIC_CLERK_FRONTEND_API ?? "https://*.clerk.accounts.dev";
const dev = process.env.NODE_ENV !== "production";

const csp = [
  "default-src 'self'",
  // Next.js injects inline bootstrap scripts; 'unsafe-eval' is only needed by the dev server.
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}${clerk ? ` ${clerkFapi} https://challenges.cloudflare.com` : ""}`,
  "style-src 'self' 'unsafe-inline'",
  `img-src 'self' data: blob: ${assets}${clerk ? " https://img.clerk.com" : ""}`,
  "font-src 'self' data:",
  `connect-src 'self' ${api}${clerk ? ` ${clerkFapi}` : ""}${process.env.NEXT_PUBLIC_SENTRY_DSN ? " https://*.sentry.io" : ""}`,
  `frame-src ${clerk ? "https://challenges.cloudflare.com" : "'none'"}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [{
      source: "/:path*",
      headers: [
        { key: "Content-Security-Policy", value: csp },
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
      ],
    }];
  },
};

export { csp };
export default config;
