"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Plus, Trash2, Wand2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, Dialog, DialogClose, DialogContent, DialogTrigger, Field, Input, Notice, Select, Spinner, Textarea, cn } from "@/components/ui";
import { ApiError, api, describeError } from "@/lib/api";
import { wordCount } from "@/lib/text";
import { STATUS_LABEL, type Character, type DialogueItem, type PanelSpec, type Project, type Script } from "@/lib/types";
import { pk } from "@/lib/session";

const path = (id: string) => ({ params: { path: { project_id: id } } });

function useRefresh(id: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: pk("project", id) });
    void qc.invalidateQueries({ queryKey: pk("me") });
  };
}

// eslint-disable-next-line @next/next/no-img-element -- signed, short-lived asset URLs
const Img = ({ src, alt, className }: { src?: string | null; alt: string; className?: string }) => src ? <img src={src} alt={alt} className={className} /> : <div className={cn("halftone flex items-center justify-center text-xs text-muted", className)} role="img" aria-label={`${alt} (not ready)`}>Drawing…</div>;

// ------------------------------------------------------------------ working
export function Working({ project }: { project: Project }) {
  const p = project.progress;
  const steps = [
    { key: "intake", label: "Read the idea", done: !["queued", "intake"].includes(project.status) },
    { key: "planning", label: "Design the cast", done: !["queued", "intake", "awaiting_answers", "planning"].includes(project.status) },
    { key: "scripting", label: "Write the script", done: ["awaiting_script_approval", "illustrating", "completed", "partially_completed"].includes(project.status) },
    { key: "illustrating", label: "Draw the pages", done: ["completed", "partially_completed"].includes(project.status) },
  ];
  return (
    <div className="space-y-6">
      <Card className="p-6">
        <div className="flex items-center gap-3" role="status" aria-live="polite">
          <Spinner label={STATUS_LABEL[project.status] ?? "Working"} />
        </div>
        <ol className="mt-5 grid gap-3 sm:grid-cols-4">
          {steps.map((s) => (
            <li key={s.key} className={cn("rounded-md border-2 px-3 py-2 text-sm font-semibold", s.done ? "border-ok bg-ok/10 text-ok" : project.status === s.key ? "border-ink bg-sun" : "border-line text-muted")}>
              {s.done && <Check className="mr-1 inline h-4 w-4" aria-hidden />}{s.label}
              {project.status === s.key && s.key === "planning" && p.characters_total > 0 && <span className="block text-xs font-normal">{p.characters_ready} of {p.characters_total} character sheets ready</span>}
              {project.status === s.key && s.key === "illustrating" && <span className="block text-xs font-normal">{p.panels_done} of {p.panels_total} panels · {p.pages_ready} of {p.pages_total} pages</span>}
            </li>
          ))}
        </ol>
        <p className="mt-4 text-sm text-muted">You can leave this page. Your comic keeps going and will be here when you come back.</p>
      </Card>
      {project.characters.length > 0 && project.status === "planning" && (
        <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {project.characters.map((c) => (
            <li key={c.slug}><Card className="overflow-hidden"><Img src={c.images.portrait} alt={`${c.name} portrait`} className="aspect-[3/4] w-full object-cover" /><p className="p-3 font-bold">{c.name}</p></Card></li>
          ))}
        </ul>
      )}
      {project.status === "illustrating" && <PagePreviews project={project} />}
    </div>
  );
}

export function PagePreviews({ project }: { project: Project }) {
  return (
    <ul className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
      {project.pages.map((page) => {
        const done = page.panels.filter((x) => !x.busy && x.status !== "pending").length;
        return (
          <li key={page.id}>
            <Card className="overflow-hidden">
              <Img src={page.preview_url} alt={`Page ${page.index + 1}`} className="w-full bg-paper object-contain" />
              {!page.preview_url && <div className="halftone" style={{ aspectRatio: `${page.size[0]} / ${page.size[1]}` }} />}
              <div className="flex items-center justify-between border-t-2 border-ink p-3 text-sm">
                <span className="font-bold">Page {page.index + 1}</span>
                <span className="text-muted">{page.status === "ready" ? "Lettered" : `${done} of ${page.panels.length} panels drawn`}</span>
              </div>
            </Card>
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------ questions
export function Questions({ project }: { project: Project }) {
  const refresh = useRefresh(project.id);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const submit = useMutation({
    mutationFn: async (body: Record<string, string>) => (await api.POST("/v1/projects/{project_id}/brief/answers", { ...path(project.id), body: { answers: body } })).data,
    onSuccess: refresh,
  });
  return (
    <Card className="mx-auto max-w-2xl space-y-6 p-6">
      <div>
        <h2 className="text-xl font-bold">A couple of quick questions</h2>
        <p className="text-sm text-muted">Each one is optional. Skip any of them and we&apos;ll use the suggested answer.</p>
      </div>
      {project.questions.map((q) => (
        <fieldset key={q.id} className="space-y-2">
          <legend className="font-semibold">{q.question}</legend>
          <div className="flex flex-wrap gap-2">
            {q.options.map((o) => {
              const selected = (answers[q.id] ?? "") === o;
              return (
                <label key={o} className={cn("cursor-pointer rounded-md border-2 px-3 py-2 text-sm font-semibold has-[:focus-visible]:outline has-[:focus-visible]:outline-3 has-[:focus-visible]:outline-teal", selected ? "border-ink bg-sun" : "border-line bg-card")}>
                  <input type="radio" className="sr-only" name={q.id} value={o} checked={selected} onChange={() => setAnswers((a) => ({ ...a, [q.id]: o }))} />
                  {o}{o === q.default && <span className="ml-1 font-normal text-muted">(suggested)</span>}
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
      {submit.isError && <Notice tone="danger" title="Couldn't send your answers">{describeError(submit.error)}</Notice>}
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" busy={submit.isPending} onClick={() => submit.mutate(answers)}>Continue</Button>
        <Button variant="ghost" disabled={submit.isPending} onClick={() => submit.mutate({})}>Skip all</Button>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ cast
function CharacterCard({ project, c }: { project: Project; c: Character }) {
  const refresh = useRefresh(project.id);
  const [instruction, setInstruction] = useState("");
  const revise = useMutation({
    mutationFn: async () => (await api.POST("/v1/projects/{project_id}/characters/{slug}/revise", { params: { path: { project_id: project.id, slug: c.slug } }, body: { instruction: instruction.trim() } })).data,
    onSuccess: () => { setInstruction(""); refresh(); toast.success(`Revising ${c.name}`); },
  });
  const v = c.spec.visual;
  const busy = c.revising || c.sheet_status !== "ready";
  return (
    <Card className="overflow-hidden">
      <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] border-b-2 border-ink">
        <Img src={c.images.portrait} alt={`${c.name}, hero portrait`} className="aspect-[3/4] h-full w-full border-r-2 border-ink object-cover" />
        <div className="grid grid-rows-2">
          <Img src={c.images.turnaround_sheet} alt={`${c.name}, turnaround: front, three-quarter, side and back views`} className="h-full w-full border-b-2 border-ink object-cover" />
          <Img src={c.images.expression_sheet} alt={`${c.name}, expressions: happy, angry, sad, surprised`} className="h-full w-full object-cover" />
        </div>
      </div>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-lg font-bold">{c.name}</h3>
          <Badge>{c.spec.role}</Badge>
          {busy && <Badge tone="working">Updating sheet</Badge>}
        </div>
        <p className="text-sm">{c.spec.personality}</p>
        <p className="text-sm text-muted">{[v.type, v.body, v.head, v.outfit].join(" · ")}</p>
        <ul className="flex flex-wrap gap-1.5" aria-label="Signature features">
          {v.signature_features.map((f) => <li key={f}><Badge tone="action">{f}</Badge></li>)}
        </ul>
        <ul className="flex gap-1" aria-label="Colour palette">
          {Object.entries(v.palette).map(([k, hex]) => <li key={k} title={`${k} ${hex}`} className="h-5 w-5 rounded-full border border-ink/40" style={{ background: hex }}><span className="sr-only">{k} {hex}</span></li>)}
        </ul>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (instruction.trim().length >= 3) revise.mutate(); }}>
          <label className="sr-only" htmlFor={`rev-${c.slug}`}>Change request for {c.name}</label>
          <Input id={`rev-${c.slug}`} value={instruction} maxLength={400} onChange={(e) => setInstruction(e.target.value)} placeholder="e.g. shorter hair, add a scarf" disabled={busy} />
          <Button type="submit" busy={revise.isPending} disabled={busy || instruction.trim().length < 3}><Wand2 className="h-4 w-4" aria-hidden />Revise</Button>
        </form>
        <p className="text-xs text-muted">A revision redraws the whole sheet and costs 1 credit if it succeeds.</p>
        {revise.isError && <p role="alert" className="text-sm text-danger">{describeError(revise.error)}</p>}
      </div>
    </Card>
  );
}

function BibleEditor({ project }: { project: Project }) {
  const refresh = useRefresh(project.id);
  const b = project.bible!;
  const [form, setForm] = useState({ title: b.title, logline: b.logline, setting: b.setting, tone: b.tone });
  useEffect(() => setForm({ title: b.title, logline: b.logline, setting: b.setting, tone: b.tone }), [b.title, b.logline, b.setting, b.tone]);
  const dirty = form.title !== b.title || form.logline !== b.logline || form.setting !== b.setting || form.tone !== b.tone;
  const save = useMutation({
    mutationFn: async () => (await api.PATCH("/v1/projects/{project_id}/bible", { ...path(project.id), body: { cast_version: project.cast_version, ...form } })).data,
    onSuccess: () => { refresh(); toast.success("Story Bible saved"); },
    onError: (e) => { if (e instanceof ApiError && e.code === "version_conflict") refresh(); },
  });
  return (
    <Card className="space-y-4 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-bold">Story Bible</h2>
        <span className="text-sm text-muted" role="status">{save.isPending ? "Saving…" : dirty ? "Unsaved changes" : "Saved"}</span>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Title">{(ids) => <Input {...ids} maxLength={200} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />}</Field>
        <Field label="Tone">{(ids) => <Input {...ids} maxLength={200} value={form.tone} onChange={(e) => setForm({ ...form, tone: e.target.value })} />}</Field>
        <Field label="Logline">{(ids) => <Textarea {...ids} maxLength={600} value={form.logline} onChange={(e) => setForm({ ...form, logline: e.target.value })} />}</Field>
        <Field label="Setting">{(ids) => <Textarea {...ids} maxLength={600} value={form.setting} onChange={(e) => setForm({ ...form, setting: e.target.value })} />}</Field>
      </div>
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
        <div><dt className="font-semibold">Themes</dt><dd className="text-muted">{b.themes.join(", ") || "—"}</dd></div>
        <div><dt className="font-semibold">World rules</dt><dd className="text-muted">{b.world_rules.join("; ") || "—"}</dd></div>
      </dl>
      {save.isError && <Notice tone="danger" title={save.error instanceof ApiError && save.error.code === "version_conflict" ? "This was changed in another window" : "Couldn't save"}>{save.error instanceof ApiError && save.error.code === "version_conflict" ? "We reloaded the latest version. Re-apply your change and save again." : describeError(save.error)}</Notice>}
      <Button busy={save.isPending} disabled={!dirty || !form.title.trim()} onClick={() => save.mutate()}>Save Story Bible</Button>
    </Card>
  );
}

export function Cast({ project }: { project: Project }) {
  const refresh = useRefresh(project.id);
  const approve = useMutation({
    mutationFn: async () => (await api.POST("/v1/projects/{project_id}/characters/approve", { ...path(project.id), body: { cast_version: project.cast_version } })).data,
    onSuccess: refresh,
    onError: (e) => { if (e instanceof ApiError && e.status === 409) refresh(); },
  });
  const updating = project.characters.some((c) => c.revising || c.sheet_status !== "ready");
  const waiting = project.approved_cast_version === project.cast_version;
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Meet the cast</h2>
          <p className="max-w-2xl text-sm text-muted">These reference sheets lock each character&apos;s look for every panel. Tweak anything now: it&apos;s much cheaper than fixing pages later.</p>
        </div>
        <div className="text-right">
          <Button variant="primary" size="lg" busy={approve.isPending || waiting} disabled={updating} onClick={() => approve.mutate()}>{waiting ? "Starting the script" : "Approve cast and write the script"}</Button>
          <p className="mt-1 text-xs text-muted">{updating ? "Wait for the sheet to finish updating." : "The script is already covered by the credits held at the start."}</p>
        </div>
      </div>
      {approve.isError && <Notice tone="danger" title={approve.error instanceof ApiError && approve.error.code === "version_conflict" ? "The cast changed since you opened this page" : "Couldn't approve the cast"}>{approve.error instanceof ApiError && approve.error.code === "version_conflict" ? "We've loaded the latest version. Review it and approve again." : describeError(approve.error)}</Notice>}
      <ul className="grid gap-6 lg:grid-cols-2">
        {project.characters.map((c) => <li key={c.slug}><CharacterCard project={project} c={c} /></li>)}
      </ul>
      {project.locations.length > 0 && (
        <section aria-labelledby="locations">
          <h3 id="locations" className="mb-3 text-lg font-bold">Recurring locations</h3>
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {project.locations.map((l) => (
              <li key={l.slug}><Card className="overflow-hidden"><Img src={l.image_url} alt={`${l.name} reference`} className="aspect-video w-full object-cover" /><div className="border-t-2 border-ink p-3"><p className="font-bold">{l.name}</p><p className="text-sm text-muted">{l.description}</p></div></Card></li>
            ))}
          </ul>
        </section>
      )}
      {project.bible && <BibleEditor project={project} />}
    </div>
  );
}

// ------------------------------------------------------------------ script
const DIALOGUE_TYPES: DialogueItem["type"][] = ["speech", "thought", "caption", "sfx"];

function PanelEditor({ panel, onChange, characters, locations, limits, errors, label }: { panel: PanelSpec; onChange: (p: PanelSpec) => void; characters: Character[]; locations: Project["locations"]; limits: Record<string, number>; errors: string[]; label: string }) {
  const present = panel.characters.map((c) => c.id);
  const maxWords = limits.max_words_per_balloon ?? 25;
  const maxBalloons = limits.max_balloons_per_panel ?? 3;
  const dialogue = panel.dialogue ?? [];
  const setLine = (i: number, patch: Partial<DialogueItem>) => onChange({ ...panel, dialogue: dialogue.map((d, n) => (n === i ? { ...d, ...patch } : d)) });
  return (
    <fieldset className={cn("space-y-3 rounded-md border-2 p-4", errors.length ? "border-danger" : "border-line")}>
      <legend className="px-1 text-sm font-bold">{label}{panel.emphasis === "large" && <Badge tone="action" className="ml-2">Large panel</Badge>}</legend>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Shot">{(ids) => <Select {...ids} value={panel.shot} onChange={(e) => onChange({ ...panel, shot: e.target.value as PanelSpec["shot"] })}><option value="wide">Wide</option><option value="medium">Medium</option><option value="close_up">Close-up</option></Select>}</Field>
        <Field label="Location">{(ids) => <Select {...ids} value={panel.location_id} onChange={(e) => onChange({ ...panel, location_id: e.target.value })}>{locations.map((l) => <option key={l.slug} value={l.slug}>{l.name}</option>)}</Select>}</Field>
        <Field label="Size">{(ids) => <Select {...ids} value={panel.emphasis ?? "normal"} onChange={(e) => onChange({ ...panel, emphasis: e.target.value as "normal" | "large" })}><option value="normal">Normal</option><option value="large">Large (climax)</option></Select>}</Field>
      </div>
      <Field label="What we see" hint={`In frame: ${present.map((id) => characters.find((c) => c.slug === id)?.name ?? id).join(", ") || "no characters"}`}>
        {(ids) => <Textarea {...ids} className="min-h-16" maxLength={500} value={panel.action} onChange={(e) => onChange({ ...panel, action: e.target.value })} />}
      </Field>
      <div className="space-y-2">
        <p className="text-sm font-semibold">Lettering ({dialogue.length} of {maxBalloons})</p>
        {dialogue.map((d, i) => {
          const words = wordCount(d.text);
          return (
            <div key={i} className="grid grid-cols-[7rem_8rem_1fr_auto] items-start gap-2 max-sm:grid-cols-2">
              <Select aria-label={`Line ${i + 1} type`} value={d.type} onChange={(e) => { const type = e.target.value as DialogueItem["type"]; setLine(i, { type, speaker: type === "speech" || type === "thought" ? d.speaker ?? present[0] ?? null : null }); }}>
                {DIALOGUE_TYPES.map((t) => <option key={t} value={t}>{t === "sfx" ? "Sound effect" : t[0]!.toUpperCase() + t.slice(1)}</option>)}
              </Select>
              <Select aria-label={`Line ${i + 1} speaker`} value={d.speaker ?? ""} disabled={d.type === "caption" || d.type === "sfx"} onChange={(e) => setLine(i, { speaker: e.target.value || null })}>
                <option value="">—</option>
                {characters.map((c) => <option key={c.slug} value={c.slug}>{c.name}</option>)}
              </Select>
              <div className="max-sm:col-span-2">
                <Input aria-label={`Line ${i + 1} text`} aria-invalid={words > maxWords || undefined} value={d.text} maxLength={300} onChange={(e) => setLine(i, { text: e.target.value })} />
                <p className={cn("mt-0.5 text-xs", words > maxWords ? "font-semibold text-danger" : "text-muted")}>{words} of {maxWords} words</p>
              </div>
              <Button type="button" variant="ghost" size="icon" aria-label={`Remove line ${i + 1}`} onClick={() => onChange({ ...panel, dialogue: dialogue.filter((_, n) => n !== i) })}><Trash2 className="h-4 w-4" aria-hidden /></Button>
            </div>
          );
        })}
        <Button type="button" size="sm" disabled={dialogue.length >= maxBalloons} onClick={() => onChange({ ...panel, dialogue: [...dialogue, { type: "speech", speaker: present[0] ?? null, text: "" }] })}><Plus className="h-4 w-4" aria-hidden />Add line</Button>
      </div>
      {errors.length > 0 && <ul role="alert" className="list-inside list-disc text-sm text-danger">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
    </fieldset>
  );
}

export function ScriptReview({ project }: { project: Project }) {
  const refresh = useRefresh(project.id);
  const qc = useQueryClient();
  const script = project.script!;
  const [draft, setDraft] = useState<Script>(script.content);
  const [base, setBase] = useState(script.version);
  useEffect(() => { setDraft(script.content); setBase(script.version); }, [script.version]); // eslint-disable-line react-hooks/exhaustive-deps -- reset only when the server version changes
  const dirty = useMemo(() => JSON.stringify(draft) !== JSON.stringify(script.content), [draft, script.content]);
  const meta = useQuery({ queryKey: ["meta"], staleTime: Infinity, queryFn: async () => (await api.GET("/v1/meta")).data! });
  const estimate = useQuery({ queryKey: pk("estimate", project.id, script.version), queryFn: async () => (await api.GET("/v1/projects/{project_id}/estimate", path(project.id))).data! });
  const save = useMutation({
    mutationFn: async () => (await api.PATCH("/v1/projects/{project_id}/script", { ...path(project.id), body: { base_version: base, script: draft } })).data!,
    onSuccess: () => { refresh(); void qc.invalidateQueries({ queryKey: pk("estimate", project.id) }); toast.success("Script saved as a new version"); },
  });
  const approve = useMutation({
    mutationFn: async () => (await api.POST("/v1/projects/{project_id}/script/approve", { ...path(project.id), body: { script_version: script.version } })).data,
    onSuccess: refresh,
    onError: (e) => { if (e instanceof ApiError && e.status === 409) refresh(); },
  });
  const issuesFor = (pi: number, ni: number) => script.issues.filter((i) => i.path.startsWith(`pages[${pi}].panels[${ni}]`) && (i.severity ?? "error") === "error").map((i) => i.message);
  const pageIssues = script.issues.filter((i) => !i.path.includes(".panels[") && (i.severity ?? "error") === "error");
  const warnings = script.issues.filter((i) => i.severity === "warning");
  const conflict = save.error instanceof ApiError && save.error.code === "version_conflict";
  const waiting = project.approved_script_version === script.version;
  const setPanel = (pi: number, ni: number, p: PanelSpec) => setDraft({ pages: draft.pages.map((page, a) => (a === pi ? { panels: page.panels.map((old, b) => (b === ni ? p : old)) } : page)) });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold">Review the script</h2>
          <p className="max-w-2xl text-sm text-muted">Change any line or panel. Drawing only starts when you approve this exact version.</p>
        </div>
        <span className="text-sm text-muted" role="status">Version {script.version} · {save.isPending ? "Saving…" : dirty ? "Unsaved changes" : script.source === "user" ? "Saved (edited by you)" : "Saved"}</span>
      </div>
      <Card className="p-5">
        <h3 className="mb-2 font-bold">Beat sheet</h3>
        <ol className="grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-5">
          {script.beats.beats.map((b, i) => <li key={i} className="rounded-md border-2 border-line p-2"><span className="font-semibold capitalize">{b.name}</span><span className="block text-muted">{b.summary}</span></li>)}
        </ol>
      </Card>
      {script.has_errors && <Notice tone="danger" title="This script breaks a few comic rules">Fix the highlighted panels, then save. {pageIssues.map((i) => i.message).join(" ")}</Notice>}
      {warnings.length > 0 && !script.has_errors && <Notice tone="warn" title="Suggestions">{warnings.map((w) => w.message).join(" ")}</Notice>}
      {conflict && <Notice tone="danger" title="The script was changed in another window" action={<Button onClick={() => { save.reset(); refresh(); }}>Load latest</Button>}>Loading the latest version will replace your unsaved edits here.</Notice>}
      {save.isError && !conflict && <Notice tone="danger" title="Couldn't save the script">{describeError(save.error)}</Notice>}
      {draft.pages.map((page, pi) => (
        <section key={pi} aria-labelledby={`page-${pi}`} className="space-y-3">
          <h3 id={`page-${pi}`} className="text-lg font-bold">Page {pi + 1} <span className="text-sm font-normal text-muted">· {page.panels.length} panels</span></h3>
          <div className="grid gap-4 xl:grid-cols-2">
            {page.panels.map((panel, ni) => <PanelEditor key={ni} label={`Panel ${ni + 1}`} panel={panel} onChange={(p) => setPanel(pi, ni, p)} characters={project.characters} locations={project.locations} limits={meta.data?.limits ?? {}} errors={dirty ? [] : issuesFor(pi, ni)} />)}
          </div>
        </section>
      ))}
      <div className="sticky bottom-4 z-20">
        <Card className="flex flex-wrap items-center justify-between gap-4 p-4">
          <div className="text-sm">
            {estimate.data ? <p className="font-semibold">Drawing {estimate.data.breakdown.panels ?? "these"} panels holds {estimate.data.credits} credits <span className="font-normal text-muted">(you have {estimate.data.available})</span></p> : <Spinner label="Calculating cost" />}
            <p className="text-muted">{estimate.data?.note}</p>
          </div>
          <div className="flex gap-3">
            <Button busy={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>Save changes</Button>
            <Dialog>
              <DialogTrigger asChild><Button variant="primary" disabled={dirty || script.has_errors || waiting || estimate.data?.sufficient === false} busy={waiting}>{waiting ? "Starting illustration" : "Approve and draw"}</Button></DialogTrigger>
              <DialogContent title="Start drawing?" description={`This approves script version ${script.version} and holds ${estimate.data?.credits ?? "the"} credits. You're charged one credit per panel that is delivered; automatic quality retries are free.`}>
                {approve.isError && <Notice tone="danger" title="Couldn't start">{approve.error instanceof ApiError && approve.error.code === "insufficient_credits" ? <>You don&apos;t have enough credits. <Link className="underline" href="/billing">Add credits</Link></> : describeError(approve.error)}</Notice>}
                <div className="mt-4 flex justify-end gap-3">
                  <DialogClose asChild><Button variant="ghost">Not yet</Button></DialogClose>
                  <Button variant="primary" busy={approve.isPending} onClick={() => approve.mutate()}>Approve and draw</Button>
                </div>
              </DialogContent>
            </Dialog>
          </div>
          {estimate.data?.sufficient === false && <p className="w-full text-sm font-semibold text-warn">Not enough credits to draw this script. <Link className="underline" href="/billing">Add credits</Link> or shorten it.</p>}
          {dirty && <p className="w-full text-sm text-muted">Save your changes before approving, so you approve exactly what will be drawn.</p>}
        </Card>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ stopped
export function Stopped({ project }: { project: Project }) {
  const refresh = useRefresh(project.id);
  const resume = useMutation({ mutationFn: async () => (await api.POST("/v1/projects/{project_id}/resume", path(project.id))).data, onSuccess: refresh });
  const f = project.failure;
  const canResume = (project.status === "failed" || project.status === "expired") && !!project.resume_from;
  const titles: Record<string, string> = { failed: "This comic stopped before it finished", expired: "This comic was waiting for you", cancelled: "You cancelled this comic", blocked: "We can't make this comic" };
  return (
    <Card className="mx-auto max-w-2xl space-y-4 p-6">
      <h2 className="text-xl font-bold">{titles[project.status] ?? "This comic stopped"}</h2>
      <p>{f?.message as string ?? "Something interrupted this comic."}</p>
      {typeof f?.suggestion === "string" && <p className="text-sm text-muted">Tip: {f.suggestion}</p>}
      <p className="text-sm text-muted">{project.status === "blocked" ? "Nothing was charged for the blocked step." : "Credits held for unfinished work were released. Finished work is kept."}</p>
      {resume.isError && <Notice tone="danger" title="Couldn't resume">{describeError(resume.error)}</Notice>}
      <div className="flex flex-wrap gap-3">
        {canResume && <Button variant="primary" busy={resume.isPending} onClick={() => resume.mutate()}>{project.status === "expired" ? "Pick it back up" : "Try again from where it stopped"}</Button>}
        <Button asChild><Link href="/new">Start a new comic</Link></Button>
      </div>
      {typeof f?.code === "string" && <p className="text-xs text-muted">Reference: {f.code}</p>}
    </Card>
  );
}
