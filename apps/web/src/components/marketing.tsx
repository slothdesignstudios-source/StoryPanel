import Link from "next/link";
import type { ReactNode } from "react";
import { SAMPLES, visible, type Sample } from "@/content/samples";
import { SiteAuthLinks } from "./site-auth-links";
import { cn } from "@/lib/cn";

/** The StoryPanel wordmark: Bangers, set in ink. Used only as the logo. */
export function Wordmark({ className }: { className?: string }) {
  return <span className={cn("font-sfx tracking-[0.04em] text-ink", className)}>StoryPanel</span>;
}

export function SiteHeader() {
  const nav = [
    { href: "/gallery", label: "Gallery" },
    { href: "/pricing", label: "Pricing" },
    { href: "/#faq", label: "FAQ" },
  ];
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-paper/90 backdrop-blur-sm">
      <div className="mx-auto flex h-16 max-w-6xl items-center gap-4 px-4 sm:px-6">
        <Link href="/" aria-label="StoryPanel home" className="rounded-md"><Wordmark className="text-[1.65rem] leading-none" /></Link>
        <nav aria-label="Main" className="ml-4 hidden items-center gap-1 md:flex">
          {nav.map((n) => <Link key={n.href} href={n.href} className="rounded-md px-3 py-2 text-sm font-medium text-muted hover:text-ink">{n.label}</Link>)}
        </nav>
        <div className="ml-auto flex items-center gap-2"><SiteAuthLinks /></div>
      </div>
      <nav aria-label="Main (small screens)" className="flex gap-1 overflow-x-auto border-t border-line px-2 md:hidden">
        {nav.map((n) => <Link key={n.href} href={n.href} className="rounded-md px-3 py-2 text-sm font-medium text-muted hover:text-ink">{n.label}</Link>)}
      </nav>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="mt-24 border-t border-line bg-card">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[2fr_1fr_1fr]">
        <div className="max-w-sm space-y-3">
          <Wordmark className="text-2xl" />
          <p className="text-sm text-muted">Turn your story idea into an editable comic. You approve the cast and the script before anything is drawn, and every balloon stays editable.</p>
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">Product</h2>
          <ul className="mt-3 space-y-2 text-sm">
            <li><Link className="text-muted hover:text-ink" href="/gallery">Gallery</Link></li>
            <li><Link className="text-muted hover:text-ink" href="/pricing">Pricing</Link></li>
            <li><Link className="text-muted hover:text-ink" href="/#how-it-works">How it works</Link></li>
            <li><Link className="text-muted hover:text-ink" href="/#faq">Questions</Link></li>
          </ul>
        </div>
        <div>
          <h2 className="text-sm font-semibold text-ink">Your account</h2>
          <ul className="mt-3 space-y-2 text-sm">
            <li><Link className="text-muted hover:text-ink" href="/sign-in">Sign in</Link></li>
            <li><Link className="text-muted hover:text-ink" href="/new">Start a comic</Link></li>
          </ul>
        </div>
      </div>
      <div className="border-t border-line">
        <p className="mx-auto max-w-6xl px-4 py-5 text-xs text-muted sm:px-6">Comics are drawn by AI image models from your approved script. Rights in AI-generated images vary by country: check the terms before commercial use.</p>
      </div>
    </footer>
  );
}

/** A sample page, or a neutral placeholder when the sample isn't approved for publication. */
export function SampleArt({ sample, priority = false, sizes = "(min-width: 1024px) 40vw, 90vw", className }: { sample: Sample; priority?: boolean; sizes?: string; className?: string }) {
  const page = sample.pages[0]!;
  if (!visible(sample)) {
    return (
      <div className={cn("halftone flex aspect-[2/3] w-full flex-col items-center justify-center gap-2 rounded-lg border border-line p-6 text-center", className)} role="img" aria-label={`${sample.title}: sample artwork not yet available`}>
        <span className="font-display text-lg text-ink/70">{sample.title}</span>
        <span className="text-xs text-muted">Sample artwork coming soon</span>
      </div>
    );
  }
  return (
    <figure className={cn("relative", className)}>
      {/* eslint-disable-next-line @next/next/no-img-element -- static files; explicit sizes and srcset, no optimizer needed */}
      <img src={page.src800} srcSet={`${page.src800} 800w, ${page.src1600} 1600w`} sizes={sizes} width={page.width} height={page.height}
        alt={page.alt} loading={priority ? "eager" : "lazy"} fetchPriority={priority ? "high" : "auto"} decoding="async"
        className="h-auto w-full rounded-lg border border-line bg-card shadow-float" />
      {!sample.approved && <PreviewLabel />}
    </figure>
  );
}

export function PreviewLabel() {
  return <figcaption className="mt-2 text-center text-[11px] font-semibold uppercase tracking-wide text-warn">Local preview · not approved for publication</figcaption>;
}

export function Section({ id, eyebrow, title, lead, children, className }: { id?: string; eyebrow?: string; title: string; lead?: ReactNode; children: ReactNode; className?: string }) {
  const hid = id ? `${id}-title` : undefined;
  return (
    <section id={id} aria-labelledby={hid} className={cn("mx-auto max-w-6xl scroll-mt-24 px-4 sm:px-6", className)}>
      <div className="max-w-2xl">
        {eyebrow && <p className="text-sm font-semibold text-accent">{eyebrow}</p>}
        <h2 id={hid} className="mt-1 text-3xl sm:text-4xl">{title}</h2>
        {lead && <p className="mt-3 text-base text-muted">{lead}</p>}
      </div>
      <div className="mt-10">{children}</div>
    </section>
  );
}

export function GalleryGrid({ samples = SAMPLES }: { samples?: Sample[] }) {
  return (
    <ul className="grid gap-x-8 gap-y-12 sm:grid-cols-2">
      {samples.map((s) => (
        <li key={s.slug}>
          <article className="space-y-4">
            {/* the title link below is the accessible link; this one is a larger pointer target for the same page */}
            <Link href={`/gallery/${s.slug}`} className="block rounded-lg" tabIndex={-1} aria-hidden>
              <SampleArt sample={s} sizes="(min-width: 1024px) 34rem, (min-width: 640px) 45vw, 92vw" />
            </Link>
            <div>
              <h3 className="text-xl"><Link href={`/gallery/${s.slug}`} className="hover:underline">{s.title}</Link></h3>
              <p className="mt-1 text-sm text-muted">{s.format}{s.style ? ` · ${s.style}` : ""}</p>
              <p className="mt-2 max-w-prose text-[0.95rem]">{s.logline}</p>
            </div>
          </article>
        </li>
      ))}
    </ul>
  );
}
