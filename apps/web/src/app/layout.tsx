import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "StoryPanel", template: "%s · StoryPanel" },
  description: "Turn one idea into a finished comic: cast, script, art and lettering you can edit panel by panel.",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#fbf7f0" };

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
