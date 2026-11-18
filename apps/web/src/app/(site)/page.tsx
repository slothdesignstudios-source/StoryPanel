import type { Metadata } from "next";
import Link from "next/link";
import { BookOpenCheck, Download, PencilLine, ScrollText, Users } from "lucide-react";
import { GalleryGrid, SampleArt, Section } from "@/components/marketing";
import { CreditPrices, HoldsExplained } from "@/components/pricing";
import { Button } from "@/components/ui";
import { SAMPLES, anyUnapprovedShown } from "@/content/samples";
import { getPublicMeta } from "@/lib/public-meta";

export const revalidate = 300;

const DESCRIPTION = "Describe a story in a sentence or two. StoryPanel proposes a cast and a script for you to approve, draws the pages, and letters them, with every balloon left editable.";

export const metadata: Metadata = {
  title: { absolute: "StoryPanel: turn your story idea into an editable comic" },
  description: DESCRIPTION,
  alternates: { canonical: "/" },
  openGraph: { title: "Turn your story idea into an editable comic", description: DESCRIPTION, url: "/" },
  ...(anyUnapprovedShown() ? { robots: { index: false, follow: false } } : {}),
};

const STEPS = [
  { title: "Describe your idea", body: "One or two sentences is enough. Choose a length, an art style and an audience, or let StoryPanel suggest them." },
  { title: "Approve the cast", body: "Each character gets a reference sheet with turnaround views and expressions, plus a Story Bible you can edit. Nothing is drawn until you approve." },
  { title: "Review the script", body: "Read the beat sheet and every panel: the shot, what we see and each line of dialogue. Change anything, then approve that exact version." },
  { title: "Read, edit and export", body: "Pages are drawn and lettered. Move or rewrite any balloon, redraw a panel, then export a PDF, PNG pages or CBZ." },
];

const BENEFITS = [
  { icon: Users, title: "Character references", body: "Reference sheets and continuity rules are used for every panel, and each panel is checked against them. Panels the checks can't confirm are marked for you to look at." },
  { icon: ScrollText, title: "Script review before drawing", body: "You approve the story beats and every line before any page is drawn, so the art follows the story you chose." },
  { icon: PencilLine, title: "Editable lettering", body: "Balloons are real text, not part of the picture. Drag them, resize them, rewrite them; they stay sharp in every export." },
  { icon: BookOpenCheck, title: "Honest quality notes", body: "When a panel needs attention, you see why: a missing detail, a covered face, a check that couldn't confirm. The workflow never hides it." },
  { icon: Download, title: "Print-ready exports", body: "PDF with print bleed, PNG pages for posting, or CBZ for comic reader apps. Each export reports its print resolution." },
];

const FAQ: [string, string][] = [
  ["Will my characters look the same in every panel?", "StoryPanel works hard at it: every panel is drawn from the approved reference sheets and checked against them, and the continuity rules you set. It isn't guaranteed. When a check finds a difference, or can't confirm a panel, the panel is marked for review and you can redraw it or fix it with an instruction."],
  ["Can I change the story before it's drawn?", "Yes. You approve the cast and Story Bible, then the script. You can edit any panel description or line of dialogue, and drawing starts only when you approve that exact version."],
  ["Can I edit the text after the pages are drawn?", "Yes. Lettering is placed as editable balloons on top of the art. You can rewrite, move, resize, add or delete them at any time, and exports use your latest version."],
  ["What does it cost?", "Each step costs a set number of credits, shown before it starts. Credits are held when work begins and charged only for what's delivered; failed or blocked panels aren't charged. See the pricing page for the current numbers."],
  ["What can I make?", "Original stories: comic strips and short comics of a few pages, for all ages, teens or mature themes. Real people and existing characters or franchises aren't allowed."],
  ["Who owns the comics?", "Your comics stay in your account until you delete them. They are drawn by AI image models, and the rights in AI-generated images vary by country, so check the terms that apply to you before commercial use."],
];

export default async function Landing() {
  const meta = await getPublicMeta();
  const [hero, second] = SAMPLES;
  return (
    <>
      {/* hero */}
      <section className="mx-auto grid max-w-6xl items-center gap-12 px-4 pb-16 pt-12 sm:px-6 sm:pt-20 lg:grid-cols-[1fr_1.1fr] lg:gap-14">
        <div>
          <p className="text-sm font-semibold text-accent">From one idea to a finished comic</p>
          <h1 className="mt-3 text-[2.6rem] leading-[1.05] sm:text-6xl">Turn your story idea into an editable comic.</h1>
          <p className="mt-6 max-w-xl text-lg text-muted">Describe a story in a sentence or two. StoryPanel proposes the cast and the script for you to approve, draws every panel, and letters the pages, with every balloon left editable.</p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button asChild variant="primary" size="lg"><Link href="/new">Start a comic</Link></Button>
            <Link href="/gallery" className="rounded-md px-3 py-2 text-sm font-semibold text-accent hover:underline">See sample comics</Link>
          </div>
          <p className="mt-4 text-sm text-muted">You review the cast and the script before anything is drawn.</p>
        </div>
        <div className="relative mx-auto w-full max-w-lg lg:max-w-none">
          <div className="grid grid-cols-[1.2fr_0.8fr] items-end gap-5">
            {hero && <SampleArt sample={hero} priority sizes="(min-width: 1024px) 32vw, 55vw" />}
            {second && <SampleArt sample={second} sizes="(min-width: 1024px) 22vw, 40vw" className="mb-10" />}
          </div>
        </div>
      </section>

      <div className="space-y-24 sm:space-y-28">
        <Section id="how-it-works" eyebrow="How it works" title="You stay in charge of the story" lead="Four steps, with an approval before anything is drawn.">
          <ol className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s.title} className="border-t-2 border-ink pt-4">
                <p className="font-display text-sm font-semibold text-accent">0{i + 1}</p>
                <h3 className="mt-2 text-xl">{s.title}</h3>
                <p className="mt-2 text-[0.95rem] text-muted">{s.body}</p>
              </li>
            ))}
          </ol>
        </Section>

        <Section id="gallery" eyebrow="Gallery" title="Real pages, as delivered" lead="Each page was drawn and lettered by StoryPanel from an approved script.">
          <div className="max-w-4xl"><GalleryGrid /></div>
          <div className="mt-10"><Link href="/gallery" className="text-sm font-semibold text-accent hover:underline">Open the gallery</Link></div>
        </Section>

        <Section id="features" eyebrow="What you get" title="Made to be edited, not just generated">
          <ul className="grid gap-x-10 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
            {BENEFITS.map(({ icon: Icon, title, body }) => (
              <li key={title} className="space-y-2">
                <Icon className="h-5 w-5 text-accent" aria-hidden />
                <h3 className="text-lg">{title}</h3>
                <p className="text-[0.95rem] text-muted">{body}</p>
              </li>
            ))}
          </ul>
        </Section>

        <Section id="pricing" eyebrow="Pricing" title="Pay with credits, only for what's delivered" lead="Every step shows its cost before it starts. These are the current prices.">
          <CreditPrices meta={meta} />
          <div className="mt-10"><HoldsExplained /></div>
          <div className="mt-8"><Link href="/pricing" className="text-sm font-semibold text-accent hover:underline">Full pricing details</Link></div>
        </Section>

        <Section id="faq" eyebrow="Questions" title="Good to know">
          <dl className="divide-y divide-line border-y border-line">
            {FAQ.map(([q, a]) => (
              <div key={q} className="grid gap-2 py-6 md:grid-cols-[2fr_3fr] md:gap-10">
                <dt className="font-display text-lg font-semibold">{q}</dt>
                <dd className="max-w-prose text-[0.95rem] text-muted">{a}</dd>
              </div>
            ))}
          </dl>
        </Section>

        <section aria-labelledby="cta-title" className="mx-auto max-w-6xl px-4 sm:px-6">
          <div className="rounded-2xl bg-ink px-6 py-14 text-center text-paper sm:px-12">
            <h2 id="cta-title" className="text-3xl text-paper sm:text-4xl">Start with one sentence.</h2>
            <p className="mx-auto mt-3 max-w-xl text-paper/80">Describe your story. You&apos;ll see the cast and the script, and their cost, before anything is drawn.</p>
            <div className="mt-8 flex justify-center"><Button asChild size="lg" className="border-paper bg-paper text-ink hover:bg-white"><Link href="/new">Start a comic</Link></Button></div>
          </div>
        </section>
      </div>
    </>
  );
}
