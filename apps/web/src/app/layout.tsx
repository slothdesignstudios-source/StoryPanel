import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Providers } from "@/components/providers";
import { SITE_URL } from "@/lib/site";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: "StoryPanel", template: "%s · StoryPanel" },
  description: "Turn your story idea into an editable comic: approve the cast and script, then edit every page and balloon.",
  applicationName: "StoryPanel",
  openGraph: { type: "website", siteName: "StoryPanel", images: [{ url: "/og.png", width: 1200, height: 630, alt: "StoryPanel: turn your story idea into an editable comic" }] },
  twitter: { card: "summary_large_image", images: ["/og.png"] },
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#f7f6f2" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-ink focus:px-4 focus:py-2 focus:text-paper">Skip to content</a>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
