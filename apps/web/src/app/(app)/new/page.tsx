"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Button, Card, Field, Notice, Select, Textarea, cn } from "@/components/ui";
import { ApiError, api, describeError } from "@/lib/api";

const STARTERS = [
  { label: "Birthday comic", prompt: "A birthday comic for my friend who loves hiking and terrible puns. 2 pages, warm and funny." },
  { label: "Explain a concept", prompt: "Explain how photosynthesis works as an adventure story with a curious seedling. 4 pages, for ages 8 and up." },
  { label: "Bedtime story", prompt: "A sleepy dragon who is afraid of the dark learns that the stars keep watch. 4 pages, gentle storybook style." },
  { label: "Gag strip", prompt: "A cat who is convinced the vacuum cleaner is its nemesis. A 4 panel strip." },
];

export default function NewComic() {
  const router = useRouter();
  const qc = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [open, setOpen] = useState(false);
  const [length, setLength] = useState("");
  const [style, setStyle] = useState("");
  const [audience, setAudience] = useState("");
  const meta = useQuery({ queryKey: ["meta"], staleTime: Infinity, queryFn: async () => (await api.GET("/v1/meta")).data! });
  const estimate = useQuery({
    queryKey: ["estimate", length],
    queryFn: async () => (await api.GET("/v1/estimate", { params: { query: length === "strip" ? { format: "strip" } : length ? { pages: Number(length) } : {} } })).data!,
  });
  const create = useMutation({
    mutationFn: async () =>
      (await api.POST("/v1/projects", {
        body: {
          prompt: prompt.trim(),
          ...(length === "strip" ? { format: "strip" as const } : length ? { pages: Number(length), format: "western_page" as const } : {}),
          ...(style ? { style_preset: style } : {}),
          ...(audience ? { audience: audience as "all_ages" | "teen" | "mature" } : {}),
        },
      })).data!,
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: ["projects"] });
      void qc.invalidateQueries({ queryKey: ["me"] });
      router.push(`/projects/${p.id}`);
    },
  });
  const err = create.error instanceof ApiError ? create.error : null;
  const promptError = err?.fields.find((f) => f.field === "prompt")?.message ?? (err?.code === "moderation_blocked" ? err.message : null);
  const tooShort = prompt.trim().length < 10;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!tooShort) create.mutate();
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">What&apos;s the story?</h1>
        <p className="text-sm text-muted">One or two sentences is enough. You&apos;ll approve the cast and the script before any page is drawn.</p>
      </div>
      <Card className="p-6">
        <form onSubmit={submit} className="space-y-5" noValidate>
          <Field label="Your idea" hint={`${prompt.trim().length}/2000 characters. Original characters only: no real people or existing franchises.`} error={promptError}>
            {(ids) => <Textarea {...ids} rows={5} maxLength={2000} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="A shy robot who runs a night-time noodle stall in a rainy city learns to make friends." />}
          </Field>
          <div>
            <p className="mb-2 text-sm font-semibold">Need a starting point?</p>
            <div className="flex flex-wrap gap-2">
              {STARTERS.map((s) => <Button key={s.label} type="button" size="sm" onClick={() => setPrompt(s.prompt)}>{s.label}</Button>)}
            </div>
          </div>
          <div className="rounded-md border-2 border-line">
            <button type="button" className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-semibold" aria-expanded={open} aria-controls="options" onClick={() => setOpen((v) => !v)}>
              Options (all optional: we&apos;ll pick sensible defaults or ask)
              <ChevronDown className={cn("h-4 w-4 transition-transform", open && "rotate-180")} aria-hidden />
            </button>
            {open && (
              <div id="options" className="grid gap-4 border-t-2 border-line p-4 sm:grid-cols-3">
                <Field label="Length">
                  {(ids) => (
                    <Select {...ids} value={length} onChange={(e) => setLength(e.target.value)}>
                      <option value="">Decide for me</option>
                      <option value="strip">3–4 panel strip</option>
                      {Array.from({ length: meta.data?.max_pages ?? 8 }, (_, i) => i + 1).map((n) => <option key={n} value={n}>{n} page{n > 1 ? "s" : ""}</option>)}
                    </Select>
                  )}
                </Field>
                <Field label="Art style">
                  {(ids) => (
                    <Select {...ids} value={style} onChange={(e) => setStyle(e.target.value)}>
                      <option value="">Decide for me</option>
                      {meta.data?.styles.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                    </Select>
                  )}
                </Field>
                <Field label="Audience">
                  {(ids) => (
                    <Select {...ids} value={audience} onChange={(e) => setAudience(e.target.value)}>
                      <option value="">Decide for me</option>
                      <option value="all_ages">All ages</option>
                      <option value="teen">Teen</option>
                      <option value="mature">Mature themes</option>
                    </Select>
                  )}
                </Field>
              </div>
            )}
          </div>
          {estimate.data && (
            <Notice tone={estimate.data.sufficient ? "neutral" : "warn"} title={`Starting holds ${estimate.data.credits} credits (you have ${estimate.data.available} available)`}>
              <p>Covers the cast, reference sheets and script. {estimate.data.note}</p>
              {!estimate.data.sufficient && <p className="mt-1 font-semibold text-warn">You need more credits to start. Visit Credits to add some.</p>}
            </Notice>
          )}
          {create.isError && !promptError && <Notice tone="danger" title="We couldn't start your comic">{describeError(create.error)}</Notice>}
          <Button type="submit" variant="primary" size="lg" busy={create.isPending} disabled={tooShort || estimate.data?.sufficient === false}>Create my cast</Button>
        </form>
      </Card>
    </div>
  );
}
