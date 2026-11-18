import type { Metadata } from "next";
import Link from "next/link";
import { GalleryGrid } from "@/components/marketing";
import { Button } from "@/components/ui";
import { anyUnapprovedShown } from "@/content/samples";

const DESCRIPTION = "Sample comic pages drawn and lettered by StoryPanel from approved scripts.";

export const metadata: Metadata = {
  title: "Gallery",
  description: DESCRIPTION,
  alternates: { canonical: "/gallery" },
  openGraph: { title: "StoryPanel gallery", description: DESCRIPTION, url: "/gallery" },
  ...(anyUnapprovedShown() ? { robots: { index: false, follow: false } } : {}),
};

export default function Gallery() {
  return (
    <div className="mx-auto max-w-6xl px-4 pt-12 sm:px-6 sm:pt-16">
      <div className="max-w-2xl">
        <p className="text-sm font-semibold text-accent">Gallery</p>
        <h1 className="mt-1 text-4xl sm:text-5xl">Comics made with StoryPanel</h1>
        <p className="mt-4 text-lg text-muted">Each page is shown as it was delivered: drawn from the approved script and lettered automatically. Open one to read it at full size.</p>
      </div>
      <div className="mt-12"><GalleryGrid /></div>
      <div className="mt-16 rounded-2xl border border-line bg-card p-8 shadow-raised sm:flex sm:items-center sm:justify-between sm:gap-8">
        <div>
          <h2 className="text-2xl">Make your own</h2>
          <p className="mt-2 text-muted">Describe your idea; you&apos;ll approve the cast and the script before any page is drawn.</p>
        </div>
        <Button asChild variant="primary" size="lg" className="mt-6 sm:mt-0"><Link href="/new">Start a comic</Link></Button>
      </div>
    </div>
  );
}
