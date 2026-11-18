import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { PreviewLabel, SampleArt, Wordmark } from "@/components/marketing";
import { Button } from "@/components/ui";
import { SAMPLES, sampleBySlug, visible } from "@/content/samples";

type Props = { params: Promise<{ slug: string }> };

export function generateStaticParams() {
  return SAMPLES.map((s) => ({ slug: s.slug }));
}
export const dynamicParams = false;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const s = sampleBySlug((await params).slug);
  if (!s) return {};
  return {
    title: s.title,
    description: s.logline,
    alternates: { canonical: `/gallery/${s.slug}` },
    openGraph: { title: `${s.title} · StoryPanel`, description: s.logline, url: `/gallery/${s.slug}` },
    // unapproved samples are never indexed, even in a preview build
    ...(!s.approved ? { robots: { index: false, follow: false } } : {}),
  };
}

/** A distraction-free reader for a public sample: the page at a readable size, nothing else. */
export default async function SampleReader({ params }: Props) {
  const s = sampleBySlug((await params).slug);
  if (!s) notFound();
  return (
    <div className="min-h-dvh bg-paper">
      <header className="border-b border-line bg-paper">
        <div className="mx-auto flex h-14 max-w-4xl items-center gap-3 px-4">
          <Link href="/gallery" className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm font-medium text-muted hover:text-ink"><ArrowLeft className="h-4 w-4" aria-hidden />Gallery</Link>
          <Link href="/" aria-label="StoryPanel home" className="ml-auto rounded-md"><Wordmark className="text-xl" /></Link>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-4xl px-4 pb-20 pt-8">
        <div className="mx-auto max-w-[52rem]">
          <h1 className="text-3xl sm:text-4xl">{s.title}</h1>
          <p className="mt-2 text-sm text-muted">{s.format}{s.style ? ` · ${s.style}` : ""}</p>
          <p className="mt-3 max-w-prose text-muted">{s.logline}</p>
        </div>
        <div className="mx-auto mt-8 max-w-[52rem]">
          {visible(s) ? (
            s.pages.map((p, i) => (
              <figure key={p.src800} className="mb-10">
                {/* eslint-disable-next-line @next/next/no-img-element -- static files with explicit sizes and srcset */}
                <img src={p.src800} srcSet={`${p.src800} 800w, ${p.src1600} 1600w`} sizes="(min-width: 900px) 52rem, 96vw" width={p.width} height={p.height}
                  alt={p.alt} loading={i === 0 ? "eager" : "lazy"} fetchPriority={i === 0 ? "high" : "auto"} className="h-auto w-full rounded-md border border-line bg-card shadow-float" />
                {s.pages.length > 1 && <figcaption className="mt-2 text-center text-sm text-muted">Page {i + 1} of {s.pages.length}</figcaption>}
                {!s.approved && <PreviewLabel />}
              </figure>
            ))
          ) : (
            <SampleArt sample={s} />
          )}
        </div>
        <div className="mx-auto mt-6 flex max-w-[52rem] flex-wrap items-center justify-between gap-4 border-t border-line pt-8">
          <p className="text-muted">Made with StoryPanel from an approved script.</p>
          <Button asChild variant="primary"><Link href="/new">Start your own comic</Link></Button>
        </div>
      </main>
    </div>
  );
}
