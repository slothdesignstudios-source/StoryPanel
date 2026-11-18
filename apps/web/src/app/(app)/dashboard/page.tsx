"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { Badge, Button, EmptyState, Notice, PageHeader, Spinner, cn } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { ACTIVE, STATUS_LABEL, WORKING, type ProjectSummary } from "@/lib/types";
import { pk } from "@/lib/session";

export function statusTone(status: string) {
  if (status.startsWith("awaiting")) return "action" as const;
  if (WORKING.has(status)) return "working" as const;
  if (status === "completed") return "ok" as const;
  if (status === "partially_completed" || status === "expired") return "warn" as const;
  if (status === "failed" || status === "blocked") return "danger" as const;
  return "neutral" as const;
}

/** What the person can do next on this comic, in words. */
const NEXT_ACTION: Record<string, string> = {
  awaiting_answers: "Answer a few questions",
  awaiting_cast_approval: "Review the cast",
  awaiting_script_approval: "Review the script",
  completed: "Read and edit",
  partially_completed: "Read, edit and retry missing panels",
  failed: "See what happened",
  expired: "Pick it back up",
};

const needsYou = (p: ProjectSummary) => p.status.startsWith("awaiting");

function ProjectCard({ p }: { p: ProjectSummary }) {
  const review = p.panel_counts?.needs_attention ?? 0;
  const next = NEXT_ACTION[p.status];
  return (
    <Link href={`/projects/${p.id}`} className="group block h-full rounded-xl">
      <div className="flex h-full flex-col overflow-hidden rounded-xl border border-line bg-card shadow-raised transition-shadow group-hover:shadow-float">
        <div className="halftone flex aspect-[4/3] items-center justify-center overflow-hidden border-b border-line">
          {p.cover_url ? (
            // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived URLs are not cacheable by the image optimizer
            <img src={p.cover_url} alt="" className="h-full w-full object-cover object-top" />
          ) : (
            <span className="text-sm text-muted">{ACTIVE.has(p.status) ? "Pages appear here once drawn" : "No pages"}</span>
          )}
        </div>
        <div className="flex flex-1 flex-col gap-3 p-4">
          <div>
            <h2 className="line-clamp-2 text-lg leading-snug">{p.title}</h2>
            <p className="mt-1 text-xs text-muted">{p.format === "strip" ? "Strip" : `${p.pages_requested} page${p.pages_requested === 1 ? "" : "s"}`} · {new Date(p.created_at).toLocaleDateString()}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={statusTone(p.status)}>{STATUS_LABEL[p.status] ?? p.status}</Badge>
            {review > 0 && <Badge tone="warn">{review} panel{review > 1 ? "s" : ""} to review</Badge>}
          </div>
          {next && <p className={cn("mt-auto flex items-center gap-1 pt-1 text-sm font-semibold", needsYou(p) ? "text-accent" : "text-ink")}>{next}<ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" aria-hidden /></p>}
        </div>
      </div>
    </Link>
  );
}

function Grid({ items }: { items: ProjectSummary[] }) {
  return <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">{items.map((p) => <li key={p.id}><ProjectCard p={p} /></li>)}</ul>;
}

export default function Dashboard() {
  const q = useQuery({ queryKey: pk("projects"), queryFn: async () => (await api.GET("/v1/projects")).data!.projects, refetchInterval: (query) => (query.state.data?.some((p) => WORKING.has(p.status)) ? 4000 : false) });
  const waiting = q.data?.filter(needsYou) ?? [];
  const rest = q.data?.filter((p) => !needsYou(p)) ?? [];
  return (
    <div className="space-y-10">
      <PageHeader title="Your comics" lead="Pick up where you left off, or start something new." />
      {q.isPending ? <Spinner label="Loading your comics" /> : q.isError ? (
        <Notice tone="danger" title="We couldn't load your comics" action={<Button onClick={() => void q.refetch()}>Try again</Button>}>{describeError(q.error)}</Notice>
      ) : q.data.length === 0 ? (
        <EmptyState title="Your first comic is one sentence away" action={<Button asChild variant="primary" size="lg"><Link href="/new">Start a comic</Link></Button>}>
          Describe an idea. StoryPanel proposes the cast and script for you to approve, then draws and letters every page.
        </EmptyState>
      ) : (
        <>
          {waiting.length > 0 && (
            <section aria-labelledby="needs-you" className="space-y-4">
              <h2 id="needs-you" className="text-xl">Waiting for you <span className="font-sans text-base font-normal text-muted">({waiting.length})</span></h2>
              <Grid items={waiting} />
            </section>
          )}
          {rest.length > 0 && (
            <section aria-labelledby="all-comics" className="space-y-4">
              {waiting.length > 0 && <h2 id="all-comics" className="text-xl">Everything else</h2>}
              {waiting.length === 0 && <h2 id="all-comics" className="sr-only">All comics</h2>}
              <Grid items={rest} />
            </section>
          )}
        </>
      )}
    </div>
  );
}
