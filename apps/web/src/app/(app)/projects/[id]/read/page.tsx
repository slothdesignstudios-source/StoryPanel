"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button, Notice, Spinner } from "@/components/ui";
import { api, describeError } from "@/lib/api";
import { pk } from "@/lib/session";

const PageCanvas = dynamic(() => import("@/components/project/page-canvas"), { ssr: false });

export default function Reader() {
  const { id } = useParams<{ id: string }>();
  const q = useQuery({ queryKey: pk("project", id), queryFn: async () => (await api.GET("/v1/projects/{project_id}", { params: { path: { project_id: id } } })).data! });
  const [index, setIndex] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const count = q.data?.pages.length ?? 0;
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e!.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [count]);
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight") setIndex((i) => Math.min(count - 1, i + 1));
      if (e.key === "ArrowLeft") setIndex((i) => Math.max(0, i - 1));
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, [count]);
  if (q.isPending) return <Spinner label="Opening your comic" />;
  if (q.isError) return <Notice tone="danger" title="We couldn't open this comic" action={<Button onClick={() => void q.refetch()}>Try again</Button>}>{describeError(q.error)}</Notice>;
  const page = q.data.pages[index];
  if (!page) return <Notice title="Nothing to read yet" action={<Button asChild><Link href={`/projects/${id}`}>Back to the comic</Link></Button>}>Pages appear here once they are drawn.</Notice>;
  // Text alternative: the canvas is a picture, so the script is exposed for screen readers.
  const transcript = page.panels.map((p) => `Panel ${p.index + 1}. ${String(p.spec.action ?? "")} ${p.balloons.map((b) => `${b.speaker_id ? `${b.speaker_id}: ` : ""}${b.text}`).join(" ")}`).join(" ");
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h1 className="text-2xl font-semibold">{q.data.title}</h1><p className="text-sm text-muted" aria-live="polite">Page {index + 1} of {count}</p></div>
        <div className="flex gap-2">
          <Button size="icon" aria-label="Previous page" disabled={index === 0} onClick={() => setIndex(index - 1)}><ChevronLeft className="h-4 w-4" aria-hidden /></Button>
          <Button size="icon" aria-label="Next page" disabled={index >= count - 1} onClick={() => setIndex(index + 1)}><ChevronRight className="h-4 w-4" aria-hidden /></Button>
          <Button asChild><Link href={`/projects/${id}`}>Edit</Link></Button>
        </div>
      </div>
      <div ref={ref} className="overflow-hidden rounded-lg border border-line bg-card shadow-raised" role="img" aria-label={`Page ${index + 1}. ${transcript}`}>
        {width > 0 && <PageCanvas page={page} width={width} />}
      </div>
    </div>
  );
}
