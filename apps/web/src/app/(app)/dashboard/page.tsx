"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Badge, Button, Card, EmptyState, Notice, Spinner } from "@/components/ui";
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

function ProjectCard({ p }: { p: ProjectSummary }) {
  return (
    <Link href={`/projects/${p.id}`} className="group block rounded-lg">
      <Card className="h-full overflow-hidden transition-transform group-hover:-translate-y-0.5">
        <div className="halftone flex aspect-[4/3] items-center justify-center overflow-hidden border-b-2 border-ink bg-paper">
          {p.cover_url ? (
            // eslint-disable-next-line @next/next/no-img-element -- signed, short-lived URLs are not cacheable by the image optimizer
            <img src={p.cover_url} alt="" className="h-full w-full object-cover object-top" />
          ) : (
            <span className="font-sfx text-3xl text-ink/25">{ACTIVE.has(p.status) ? "In progress" : "No pages"}</span>
          )}
        </div>
        <div className="space-y-2 p-4">
          <h2 className="line-clamp-1 text-lg font-bold">{p.title}</h2>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={statusTone(p.status)}>{STATUS_LABEL[p.status] ?? p.status}</Badge>
            <span className="text-xs text-muted">{p.format === "strip" ? "Strip" : `${p.pages_requested} pages`} · {new Date(p.created_at).toLocaleDateString()}</span>
          </div>
        </div>
      </Card>
    </Link>
  );
}

export default function Dashboard() {
  const q = useQuery({ queryKey: pk("projects"), queryFn: async () => (await api.GET("/v1/projects")).data!.projects, refetchInterval: (query) => (query.state.data?.some((p) => WORKING.has(p.status)) ? 4000 : false) });
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold">Your comics</h1>
          <p className="text-sm text-muted">Pick up where you left off, or start something new.</p>
        </div>
      </div>
      {q.isPending ? <Spinner label="Loading your comics" /> : q.isError ? (
        <Notice tone="danger" title="We couldn't load your comics" action={<Button onClick={() => void q.refetch()}>Try again</Button>}>{describeError(q.error)}</Notice>
      ) : q.data.length === 0 ? (
        <EmptyState title="Your first comic is one sentence away" action={<Button asChild variant="primary" size="lg"><Link href="/new">Start a comic</Link></Button>}>
          Describe an idea. StoryPanel proposes the cast and script for you to approve, then draws and letters every page.
        </EmptyState>
      ) : (
        <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {q.data.map((p) => <li key={p.id}><ProjectCard p={p} /></li>)}
        </ul>
      )}
    </div>
  );
}
