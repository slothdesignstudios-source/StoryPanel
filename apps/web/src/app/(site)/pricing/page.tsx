import type { Metadata } from "next";
import Link from "next/link";
import { CreditPrices, HoldsExplained } from "@/components/pricing";
import { Button } from "@/components/ui";
import { getPublicMeta } from "@/lib/public-meta";

export const revalidate = 300;

const DESCRIPTION = "StoryPanel uses credits. Each step shows its cost before it starts, credits are held while work runs, and you're charged only for what's delivered.";

export const metadata: Metadata = {
  title: "Pricing",
  description: DESCRIPTION,
  alternates: { canonical: "/pricing" },
  openGraph: { title: "StoryPanel pricing", description: DESCRIPTION, url: "/pricing" },
};

const RULES: [string, string][] = [
  ["Automatic quality retries are free.", "If a panel fails our checks, StoryPanel may redraw it on its own before delivering it. You aren't charged for those attempts."],
  ["Failed and blocked panels aren't charged.", "If a panel can't be drawn, or every attempt is blocked by the safety check, its credits are released."],
  ["A delivered panel is charged even when it's flagged.", "Our checks may mark a delivered panel for review. It was drawn, so it's charged; redraw or fix it if you're not happy with it."],
  ["Redraws and fixes are charged only when they produce an image.", "If you start another change before one finishes, the earlier one is still charged if it delivers an image."],
  ["Cancelling releases what's held.", "Credits held for unfinished work come back. Finished steps stay charged."],
];

export default async function Pricing() {
  const meta = await getPublicMeta();
  return (
    <div className="mx-auto max-w-6xl px-4 pt-12 sm:px-6 sm:pt-16">
      <div className="max-w-2xl">
        <p className="text-sm font-semibold text-accent">Pricing</p>
        <h1 className="mt-1 text-4xl sm:text-5xl">Credits for each step, charged only on delivery</h1>
        <p className="mt-4 text-lg text-muted">Every step shows its cost before it starts. These numbers come from the live product configuration.</p>
      </div>

      <section aria-labelledby="costs" className="mt-12">
        <h2 id="costs" className="sr-only">What each step costs</h2>
        <CreditPrices meta={meta} />
      </section>

      <section aria-labelledby="holds" className="mt-16">
        <h2 id="holds" className="text-2xl sm:text-3xl">How a credit hold works</h2>
        <p className="mt-2 max-w-2xl text-muted">Your balance shows two numbers: credits available, and credits held for work in progress.</p>
        <div className="mt-6"><HoldsExplained /></div>
      </section>

      <section aria-labelledby="rules" className="mt-16">
        <h2 id="rules" className="text-2xl sm:text-3xl">The fine print, in plain words</h2>
        <dl className="mt-6 divide-y divide-line border-y border-line">
          {RULES.map(([t, d]) => (
            <div key={t} className="grid gap-1 py-5 md:grid-cols-[2fr_3fr] md:gap-10">
              <dt className="font-semibold">{t}</dt>
              <dd className="max-w-prose text-muted">{d}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-6 max-w-2xl text-sm text-muted">Credits pay for creating comics; they don&apos;t buy a particular result. Quality checks flag problems for you to review, but can&apos;t guarantee every panel is right.</p>
      </section>

      <div className="mt-16 flex flex-wrap items-center gap-4">
        <Button asChild variant="primary" size="lg"><Link href="/new">Start a comic</Link></Button>
        <Link href="/sign-in" className="text-sm font-semibold text-accent hover:underline">Sign in to see your balance</Link>
      </div>
    </div>
  );
}
