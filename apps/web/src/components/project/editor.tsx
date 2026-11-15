"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, BookOpen, Download, History, Minus, Plus, RefreshCw, ThumbsDown, ThumbsUp, Trash2, Wand2 } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { create } from "zustand";
import { Badge, Button, Card, Dialog, DialogContent, DialogTrigger, Field, Notice, Select, Spinner, Textarea, cn } from "@/components/ui";
import { ApiError, api, describeError } from "@/lib/api";
import { wordCount } from "@/lib/text";
import { QA_STATE_LABEL, type Balloon, type ExportOut, type Geometry, type Page, type Panel, type Project } from "@/lib/types";
import { onIdentityReset, pk } from "@/lib/session";
import { LETTERING_SUPPORTED_SUMMARY, unsupportedLetteringChars, unsupportedLetteringMessage } from "@/lib/lettering-charset";
import { ContinuityRules } from "./continuity";

const PageCanvas = dynamic(() => import("./page-canvas"), { ssr: false, loading: () => <div className="halftone aspect-[2/3] w-full" /> });

type SaveState = "saved" | "dirty" | "saving" | "error" | "conflict";
type EditorState = {
  pageIndex: number; panelId: string | null; balloonId: string | null;
  drafts: Record<string, Geometry>; texts: Record<string, string>; save: SaveState;
  set: (patch: Partial<EditorState>) => void;
};
/** Ephemeral editor state: selection, in-flight drafts and the visible save status. */
const EMPTY = { pageIndex: 0, panelId: null, balloonId: null, drafts: {}, texts: {}, save: "saved" as SaveState };
const useEditor = create<EditorState>((set) => ({ ...EMPTY, set: (patch) => set(patch) }));
// Unsaved drafts belong to the person who typed them: discard on any identity change.
onIdentityReset(() => useEditor.setState(EMPTY));

const SAVE_LABEL: Record<SaveState, string> = { saved: "All changes saved", dirty: "Unsaved changes…", saving: "Saving…", error: "Couldn't save. Retrying when you edit again.", conflict: "Changed elsewhere. Latest version loaded." };

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e!.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

// ------------------------------------------------------------------ exports
function Exports({ project }: { project: Project }) {
  const qc = useQueryClient();
  const refresh = () => void qc.invalidateQueries({ queryKey: pk("project", project.id) });
  const start = useMutation({
    mutationFn: async (format: "pdf" | "png" | "cbz") => (await api.POST("/v1/projects/{project_id}/exports", { params: { path: { project_id: project.id } }, body: { format } })).data!,
    onSuccess: refresh, onError: (e) => toast.error(describeError(e)),
  });
  const retry = useMutation({ mutationFn: async (id: string) => (await api.POST("/v1/exports/{export_id}/retry", { params: { path: { export_id: id } } })).data, onSuccess: refresh, onError: (e) => toast.error(describeError(e)) });
  const row = (e: ExportOut) => (
    <li key={e.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border-2 border-line px-3 py-2 text-sm">
      <span className="font-semibold uppercase">{e.format}</span>
      <span className="text-muted">{new Date(e.created_at).toLocaleTimeString()}</span>
      {e.status === "ready" && e.edit_version < project.edit_version && <Badge tone="warn">Before your latest edits</Badge>}
      {e.status === "ready" && e.download_url ? <Button asChild size="sm" variant="primary"><a href={e.download_url}><Download className="h-4 w-4" aria-hidden />Download</a></Button>
        : e.status === "failed" ? <Button size="sm" busy={retry.isPending} onClick={() => retry.mutate(e.id)}>Export failed · Retry</Button>
        : e.status === "expired" ? <Badge>Expired</Badge> : <Spinner label="Rendering" />}
      {e.status === "ready" && <PrintQuality q={e.print_quality} />}
    </li>
  );
  return (
    <Dialog>
      <DialogTrigger asChild><Button variant="primary"><Download className="h-4 w-4" aria-hidden />Export</Button></DialogTrigger>
      <DialogContent title="Export your comic" description="Exports capture the comic exactly as it is now. Later edits don't change a file that's already rendering.">
        <div className="grid gap-3 sm:grid-cols-3">
          {([["pdf", "PDF", "Print and share"], ["png", "PNG pages", "Post page by page"], ["cbz", "CBZ", "Comic reader apps"]] as const).map(([f, name, hint]) => (
            <button key={f} type="button" disabled={start.isPending} onClick={() => start.mutate(f)} className="rounded-md border-2 border-ink bg-card p-3 text-left shadow-panel-sm hover:bg-sun/40 disabled:opacity-50">
              <span className="block font-bold">{name}</span><span className="text-xs text-muted">{hint}</span>
            </button>
          ))}
        </div>
        {project.exports.length > 0 && <ul className="mt-4 space-y-2" aria-label="Recent exports" aria-live="polite">{project.exports.slice(0, 6).map(row)}</ul>}
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------ versions
function Versions({ panel, onDone }: { panel: Panel; onDone: () => void }) {
  const q = useQuery({ queryKey: pk("versions", panel.id, panel.current_version_id), queryFn: async () => (await api.GET("/v1/panels/{panel_id}/versions", { params: { path: { panel_id: panel.id } } })).data!.versions });
  const restore = useMutation({
    mutationFn: async (vid: string) => (await api.POST("/v1/panels/{panel_id}/versions/{version_id}/restore", { params: { path: { panel_id: panel.id, version_id: vid } } })).data,
    onSuccess: () => { onDone(); toast.success("Version restored"); }, onError: (e) => toast.error(describeError(e)),
  });
  if (q.isPending) return <Spinner label="Loading versions" />;
  if (q.isError) return <Notice tone="danger" title="Couldn't load versions">{describeError(q.error)}</Notice>;
  if (q.data.length === 0) return <p className="text-sm text-muted">No versions to show for this panel.</p>;
  return (
    <ul className="grid gap-4 sm:grid-cols-2">
      {q.data.map((v, i) => (
        <li key={v.id} className={cn("overflow-hidden rounded-md border-2", v.is_current ? "border-teal" : "border-line")}>
          {/* eslint-disable-next-line @next/next/no-img-element -- signed asset URL */}
          <img src={v.image_url ?? ""} alt={`Version ${q.data.length - i}${v.is_current ? ", current" : ""}`} className="aspect-[4/3] w-full bg-paper object-contain" />
          <div className="space-y-2 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-bold">Version {q.data.length - i}</span>
              {v.is_current && <Badge tone="working">Current</Badge>}
              <Badge tone={v.qa_state === "passed" ? "ok" : "warn"}>{v.qa_state === "passed" ? "Passed checks" : QA_STATE_LABEL[v.qa_state] ?? "Needs attention"}</Badge>
            </div>
            <p className="text-muted">{v.kind === "edit" ? `Edit: “${v.instruction}”` : v.kind === "regenerate" ? "Regenerated" : "First draw"} · {new Date(v.created_at).toLocaleString()}</p>
            {v.qa_reasons.length > 0 && <ul className="list-inside list-disc text-xs text-warn">{v.qa_reasons.map((r) => <li key={r.code + (r.subject ?? "")}>{r.label}</li>)}</ul>}
            {!v.is_current && <Button size="sm" busy={restore.isPending} onClick={() => restore.mutate(v.id)}>Use this version</Button>}
          </div>
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ side panels
const QA_NOTICE: Record<string, [string, string]> = {
  issues: ["Our checks found problems", "Automatic retries didn't fix them. Regenerate it, describe a fix, or keep it as is."],
  uncertain: ["Our checks couldn't confirm this panel", "It may be fine. Please look, then keep it, regenerate it or describe a fix."],
  not_checked: ["This panel wasn't fully checked", "The checking service had a problem, so this says nothing about the picture. It may be fine; please look."],
};

/** Why a delivered panel needs a look: the actual reasons, never presenting a failed check as a defect. */
function QaNotice({ panel }: { panel: Panel }) {
  const [title, body] = QA_NOTICE[panel.qa_state ?? "issues"] ?? QA_NOTICE.issues!;
  const shown = panel.qa_reasons.filter((r) => panel.qa_state !== "issues" || r.category === "visual_mismatch" || r.category === "missing");
  const other = panel.qa_reasons.filter((r) => !shown.includes(r));
  return (
    <Notice tone="warn" title={title}>
      {shown.length > 0 && <ul className="list-inside list-disc">{shown.map((r) => <li key={r.code + (r.subject ?? "")}>{r.label}</li>)}</ul>}
      {other.length > 0 && <p className="mt-1 text-xs">Also: {other.map((r) => r.label).join("; ")}.</p>}
      <p className="mt-1">{body} This panel was delivered, so it was charged.</p>
    </Notice>
  );
}

function PanelTools({ project, page, panel }: { project: Project; page: Page; panel: Panel }) {
  const qc = useQueryClient();
  const refresh = () => { void qc.invalidateQueries({ queryKey: pk("project", project.id) }); void qc.invalidateQueries({ queryKey: pk("me") }); };
  const [instruction, setInstruction] = useState("");
  const [addText, setAddText] = useState("");
  const regen = useMutation({ mutationFn: async () => (await api.POST("/v1/panels/{panel_id}/regenerate", { params: { path: { panel_id: panel.id } } })).data, onSuccess: refresh, onError: (e) => toast.error(describeError(e)) });
  const edit = useMutation({ mutationFn: async () => (await api.POST("/v1/panels/{panel_id}/edit", { params: { path: { panel_id: panel.id } }, body: { instruction: instruction.trim() } })).data, onSuccess: () => { setInstruction(""); refresh(); }, onError: (e) => toast.error(describeError(e)) });
  const rate = useMutation({ mutationFn: async (rating: 1 | -1) => api.POST("/v1/panels/{panel_id}/feedback", { params: { path: { panel_id: panel.id } }, body: { rating } }), onSuccess: () => toast.success("Thanks for the feedback") });
  const add = useMutation({ mutationFn: async () => (await api.POST("/v1/panels/{panel_id}/balloons", { params: { path: { panel_id: panel.id } }, body: { type: "caption", text: addText.trim() } })).data!, onSuccess: (b) => { setAddText(""); refresh(); useEditor.getState().set({ balloonId: b.id }); }, onError: (e) => toast.error(describeError(e)) });
  const order = page.panels.map((p) => p.id);
  const idx = order.indexOf(panel.id);
  const reorder = useMutation({
    mutationFn: async (to: number) => { const next = [...order]; next.splice(idx, 1); next.splice(to, 0, panel.id); return (await api.POST("/v1/pages/{page_id}/reorder", { params: { path: { page_id: page.id } }, body: { panel_ids: next } })).data; },
    onSuccess: refresh, onError: (e) => toast.error(describeError(e)),
  });
  const hasImage = !!panel.image_url;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-bold">Panel {panel.index + 1}</h3>
        {panel.busy ? <Badge tone="working">Redrawing</Badge> : panel.status === "needs_attention" ? <Badge tone="warn">{(QA_STATE_LABEL[panel.qa_state ?? ""] ?? "needs attention").replace(/^./, (c) => c.toUpperCase())}</Badge> : panel.status === "blocked" ? <Badge tone="danger">Blocked by safety check</Badge> : panel.status === "failed" ? <Badge tone="danger">Couldn&apos;t draw</Badge> : <Badge tone="ok">Looks good</Badge>}
      </div>
      <p className="text-sm text-muted">{String(panel.spec.action ?? "")}</p>
      {panel.status === "needs_attention" && <QaNotice panel={panel} />}
      {panel.status === "blocked" && <Notice tone="danger" title="This panel can't be shown">Every attempt failed our safety check, so no image is available and you weren&apos;t charged. Try regenerating.</Notice>}
      {panel.status === "failed" && <Notice tone="danger" title="This panel wasn't drawn">The illustration service had a problem. You weren&apos;t charged. Regenerate to try again.</Notice>}
      <div className="flex flex-wrap gap-2">
        <Button busy={regen.isPending || panel.busy} onClick={() => regen.mutate()}><RefreshCw className="h-4 w-4" aria-hidden />Regenerate</Button>
        <Dialog>
          <DialogTrigger asChild><Button disabled={!hasImage}><History className="h-4 w-4" aria-hidden />Versions</Button></DialogTrigger>
          <DialogContent title={`Panel ${panel.index + 1} versions`} description="Compare versions side by side and restore any of them." className="max-w-3xl"><Versions panel={panel} onDone={refresh} /></DialogContent>
        </Dialog>
        <Button variant="ghost" size="icon" aria-label="This panel looks good" disabled={!hasImage} onClick={() => rate.mutate(1)}><ThumbsUp className="h-4 w-4" aria-hidden /></Button>
        <Button variant="ghost" size="icon" aria-label="This panel has problems" disabled={!hasImage} onClick={() => rate.mutate(-1)}><ThumbsDown className="h-4 w-4" aria-hidden /></Button>
      </div>
      <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); if (instruction.trim().length >= 3) edit.mutate(); }}>
        <Field label="Fix by instruction" hint="Each regenerate or fix holds 1 credit. It is charged if it produces an image, even if you start another change before it finishes; if it fails or is blocked, the credit is released.">
          {(ids) => <Textarea {...ids} className="min-h-16" maxLength={400} value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="make her smile · add rain" disabled={!hasImage || panel.busy} />}
        </Field>
        <Button type="submit" busy={edit.isPending} disabled={!hasImage || panel.busy || instruction.trim().length < 3}><Wand2 className="h-4 w-4" aria-hidden />Apply fix</Button>
      </form>
      <div className="flex flex-wrap items-center gap-2 border-t-2 border-line pt-4">
        <span className="text-sm font-semibold">Reading order</span>
        <Button size="sm" disabled={idx <= 0 || reorder.isPending} onClick={() => reorder.mutate(idx - 1)}>Move earlier</Button>
        <Button size="sm" disabled={idx >= order.length - 1 || reorder.isPending} onClick={() => reorder.mutate(idx + 1)}>Move later</Button>
      </div>
      <form className="flex items-end gap-2 border-t-2 border-line pt-4" onSubmit={(e) => { e.preventDefault(); if (addText.trim()) add.mutate(); }}>
        <div className="flex-1"><Field label="Add a caption">{(ids) => <input {...ids} className="h-11 w-full rounded-md border-2 border-ink/80 bg-card px-3 text-sm" value={addText} maxLength={200} onChange={(e) => setAddText(e.target.value)} disabled={panel.balloons.length >= 3} />}</Field></div>
        <Button type="submit" busy={add.isPending} disabled={!addText.trim() || panel.balloons.length >= 3}><Plus className="h-4 w-4" aria-hidden />Add</Button>
      </form>
    </div>
  );
}

function BalloonTools({ balloon, geometry, pageSize, onGeometry, onText, onDelete, textValue }: { balloon: Balloon; geometry: Geometry; pageSize: [number, number]; onGeometry: (g: Geometry) => void; onText: (t: string) => void; onDelete: () => void; textValue: string }) {
  const nudge = (dx: number, dy: number) => onGeometry({ ...geometry, x: Math.min(Math.max(0, geometry.x + dx), pageSize[0] - geometry.w), y: Math.min(Math.max(0, geometry.y + dy), pageSize[1] - geometry.h), tail: geometry.tail ? { x: geometry.tail.x + dx, y: geometry.tail.y + dy } : geometry.tail });
  const size = (k: number) => onGeometry({ ...geometry, w: Math.max(50, geometry.w * k), h: Math.max(36, geometry.h * k) });
  const words = wordCount(textValue);
  const unsupported = unsupportedLetteringChars(textValue);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2"><h3 className="text-lg font-bold capitalize">{balloon.type === "sfx" ? "Sound effect" : balloon.type}</h3>{FLAG_LABELS.filter(([f]) => balloon.flags.includes(f)).map(([f, label]) => <Badge key={f} tone="warn">{label}</Badge>)}</div>
      {balloon.flags.includes("overflow") && <p role="note" className="text-sm text-warn">This text is already at the smallest readable size and still doesn&apos;t fit, so it spills out of the balloon. Shorten the text, make the balloon larger, or move it where there&apos;s more room.</p>}
      <Field label="Text" hint={`${words} of 25 words. ${LETTERING_SUPPORTED_SUMMARY}`} error={words > 25 ? "Balloons hold at most 25 words. Shorten this line." : unsupported.length ? unsupportedLetteringMessage(unsupported) : null}>
        {(ids) => <Textarea {...ids} className="min-h-20 font-comic text-base" maxLength={300} value={textValue} onChange={(e) => onText(e.target.value)} />}
      </Field>
      {/* Keyboard and screen-reader path for everything the canvas does with a pointer. */}
      <div role="group" aria-label="Move balloon" className="space-y-2">
        <p className="text-sm font-semibold">Position <span className="font-normal text-muted">(or drag on the page; arrow keys work too)</span></p>
        <div className="grid w-36 grid-cols-3 gap-1">
          <span /><Button size="icon" aria-label="Move up" onClick={() => nudge(0, -10)}><ArrowUp className="h-4 w-4" aria-hidden /></Button><span />
          <Button size="icon" aria-label="Move left" onClick={() => nudge(-10, 0)}><ArrowLeft className="h-4 w-4" aria-hidden /></Button>
          <Button size="icon" aria-label="Move down" onClick={() => nudge(0, 10)}><ArrowDown className="h-4 w-4" aria-hidden /></Button>
          <Button size="icon" aria-label="Move right" onClick={() => nudge(10, 0)}><ArrowRight className="h-4 w-4" aria-hidden /></Button>
        </div>
      </div>
      <div role="group" aria-label="Resize balloon" className="flex items-center gap-2">
        <span className="text-sm font-semibold">Size</span>
        <Button size="icon" aria-label="Make smaller" onClick={() => size(0.9)}><Minus className="h-4 w-4" aria-hidden /></Button>
        <Button size="icon" aria-label="Make larger" onClick={() => size(1.1)}><Plus className="h-4 w-4" aria-hidden /></Button>
      </div>
      <Button variant="danger" size="sm" onClick={onDelete}><Trash2 className="h-4 w-4" aria-hidden />Delete balloon</Button>
    </div>
  );
}

// ------------------------------------------------------------------ editor
/** Lettering problems the server flags; each needs the user to move or shorten the balloon. */
const FLAG_LABELS: [string, string][] = [
  ["overflow", "Text doesn't fit at a readable size"],
  ["overlaps_lettering", "Overlaps other lettering"],
  ["tail_crosses_lettering", "Tail crosses other lettering"],
  ["unsupported_characters", "Contains characters that can't be printed"],
  ["covers_face", "Covers a face"],
  ["covers_subject", "Covers a character"],
];


type PrintQualityReport = { min_dpi?: number | null; min_letter_pt?: number | null; min_letter_px?: number | null; warnings?: string[]; note?: string; print_width_in?: number } | null | undefined;

/** Effective print resolution of the exported panels (local resampling never adds detail). */
function PrintQuality({ q }: { q: PrintQualityReport }) {
  if (!q || q.min_dpi == null) return null;
  return (
    <div className="w-full text-xs text-muted">
      <span>Lowest panel resolution: {q.min_dpi} DPI at a {q.print_width_in ?? 6}-inch print width. {q.note}</span>
      {q.min_letter_pt != null && <span> Smallest lettering: {q.min_letter_pt} pt at that width, {q.min_letter_px} px in the image files.</span>}
      {(q.warnings ?? []).map((w) => <p key={w} role="note" className="font-semibold text-warn">{w}</p>)}
    </div>
  );
}

export function Editor({ project }: { project: Project }) {
  const qc = useQueryClient();
  const { pageIndex, panelId, balloonId, drafts, texts, save, set } = useEditor();
  const [ref, width] = useWidth();
  const page = project.pages[Math.min(pageIndex, project.pages.length - 1)]!;
  const panel = page.panels.find((p) => p.id === panelId) ?? null;
  const balloon = page.panels.flatMap((p) => p.balloons).find((b) => b.id === balloonId) ?? null;
  const refresh = useCallback(() => void qc.invalidateQueries({ queryKey: pk("project", project.id) }), [qc, project.id]);
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const versions = useRef<Record<string, number>>({});
  // Pending autosaves belong to this editor and this session: never fire after unmount
  // (navigation, sign-out, or another account taking over the tab).
  useEffect(() => () => { for (const t of Object.values(timers.current)) clearTimeout(t); timers.current = {}; }, []);
  useEffect(() => {
    // Track the newest server version of each balloon for optimistic-concurrency saves.
    for (const b of project.pages.flatMap((p) => p.panels).flatMap((p) => p.balloons)) versions.current[b.id] = Math.max(versions.current[b.id] ?? 0, b.version);
  }, [project.pages]);

  /** Debounced, version-checked save. A 409 means another tab or device edited this balloon. */
  const persist = useCallback((id: string, delay = 500) => {
    clearTimeout(timers.current[id]);
    set({ save: "dirty" });
    timers.current[id] = setTimeout(async () => {
      const s = useEditor.getState();
      const body = { version: versions.current[id]!, ...(s.drafts[id] ? { geometry: { ...s.drafts[id]!, rotation: s.drafts[id]!.rotation ?? 0 } } : {}), ...(s.texts[id] !== undefined && s.texts[id]!.trim() && !unsupportedLetteringChars(s.texts[id]!).length ? { text: s.texts[id]! } : {}) };  // text the fonts can't draw stays an unsaved draft; the field says why
      if (!("geometry" in body) && !("text" in body)) return;
      s.set({ save: "saving" });
      try {
        const out = (await api.PATCH("/v1/balloons/{balloon_id}", { params: { path: { balloon_id: id } }, body })).data!;
        versions.current[id] = out.version;
        const now = useEditor.getState();
        const d = { ...now.drafts }, t = { ...now.texts };
        if (JSON.stringify(d[id]) === JSON.stringify(body.geometry ? s.drafts[id] : d[id])) delete d[id];
        if (t[id] === s.texts[id]) delete t[id];
        now.set({ drafts: d, texts: t, save: Object.keys(d).length || Object.keys(t).length ? "dirty" : "saved" });
        refresh();
      } catch (e) {
        if (e instanceof ApiError && e.code === "session_changed") return; // the person signed out meanwhile
        if (e instanceof ApiError && e.code === "version_conflict") {
          const now = useEditor.getState();
          const d = { ...now.drafts }, t = { ...now.texts };
          delete d[id]; delete t[id];
          now.set({ drafts: d, texts: t, save: "conflict" });
          toast.error("This balloon was changed elsewhere. We loaded the latest version.");
          refresh();
        } else {
          useEditor.getState().set({ save: "error" });
          toast.error(describeError(e));
        }
      }
    }, delay);
  }, [refresh, set]);

  const changeGeometry = useCallback((id: string, g: Geometry, commit: boolean) => {
    set({ drafts: { ...useEditor.getState().drafts, [id]: g } });
    if (commit) persist(id);
  }, [persist, set]);

  const remove = useMutation({ mutationFn: async (id: string) => api.DELETE("/v1/balloons/{balloon_id}", { params: { path: { balloon_id: id } } }), onSuccess: () => { set({ balloonId: null }); refresh(); }, onError: (e) => toast.error(describeError(e)) });
  const layout = useMutation({ mutationFn: async (id: string) => (await api.PATCH("/v1/pages/{page_id}/layout", { params: { path: { page_id: page.id } }, body: { layout_template_id: id } })).data, onSuccess: refresh, onError: (e) => toast.error(describeError(e)) });
  const resume = useMutation({ mutationFn: async () => (await api.POST("/v1/projects/{project_id}/resume", { params: { path: { project_id: project.id } } })).data, onSuccess: refresh, onError: (e) => toast.error(describeError(e)) });

  // Warn before leaving with unsaved edits; arrow keys move the selected balloon.
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (["dirty", "saving"].includes(useEditor.getState().save)) e.preventDefault(); };
    const keys = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (!balloon || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
      const d = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] }[e.key];
      if (e.key === "Escape") set({ balloonId: null });
      if (!d) return;
      e.preventDefault();
      const step = e.shiftKey ? 20 : 5;
      const g = useEditor.getState().drafts[balloon.id] ?? (balloon.geometry as Geometry);
      changeGeometry(balloon.id, { ...g, x: g.x + d[0]! * step, y: g.y + d[1]! * step, tail: g.tail ? { x: g.tail.x + d[0]! * step, y: g.tail.y + d[1]! * step } : g.tail }, true);
    };
    window.addEventListener("beforeunload", warn);
    window.addEventListener("keydown", keys);
    return () => { window.removeEventListener("beforeunload", warn); window.removeEventListener("keydown", keys); };
  }, [balloon, changeGeometry, set]);

  const gaps = project.pages.flatMap((p) => p.panels).filter((p) => p.status === "failed" || p.status === "blocked" || p.status === "pending").length;
  return (
    <div className="space-y-4">
      {project.status === "partially_completed" && gaps > 0 && (
        <Notice tone="warn" title={`${gaps} panel${gaps > 1 ? "s" : ""} couldn't be drawn`} action={<Button busy={resume.isPending} onClick={() => resume.mutate()}>Retry missing panels</Button>}>
          Everything else is finished and editable. Retrying only draws what&apos;s missing, and you&apos;re only charged for panels that are delivered.
        </Notice>
      )}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <nav aria-label="Pages" className="flex flex-wrap gap-2">
          {project.pages.map((p, i) => (
            <button key={p.id} type="button" aria-current={i === pageIndex ? "page" : undefined} onClick={() => set({ pageIndex: i, panelId: null, balloonId: null })} className={cn("h-10 rounded-md border-2 border-ink px-3 text-sm font-semibold", i === pageIndex ? "bg-ink text-paper" : "bg-card hover:bg-sun/40")}>
              Page {i + 1}{p.panels.some((x) => x.status !== "ok") && <span className="ml-1 text-warn" aria-label="has panels needing attention">●</span>}
            </button>
          ))}
        </nav>
        <div className="flex flex-wrap items-center gap-3">
          <span role="status" aria-live="polite" className={cn("text-sm", save === "error" || save === "conflict" ? "font-semibold text-danger" : "text-muted")}>{SAVE_LABEL[save]}</span>
          <Button asChild><Link href={`/projects/${project.id}/read`}><BookOpen className="h-4 w-4" aria-hidden />Read</Link></Button>
          <Exports project={project} />
        </div>
      </div>
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <Card className="overflow-hidden p-2 sm:p-4">
          <div ref={ref} className="mx-auto w-full max-w-3xl">
            {width > 0 && <PageCanvas page={page} width={width} editable selectedPanel={panelId} selectedBalloon={balloonId} drafts={drafts} onSelectPanel={(id) => set({ panelId: id })} onSelectBalloon={(id) => set({ balloonId: id })} onBalloonChange={changeGeometry} />}
          </div>
        </Card>
        <aside aria-label="Editing tools" className="space-y-4">
          <Card className="p-4">
            <Field label="Page layout" hint="Balloons move with their panels.">
              {(ids) => <Select {...ids} value={page.layout_template_id} disabled={layout.isPending || page.layout_options.length < 2} onChange={(e) => layout.mutate(e.target.value)}>{page.layout_options.map((o) => <option key={o.id} value={o.id}>{o.id.replace(/^[ws]\d_/, "").replace(/_/g, " ")}</option>)}</Select>}
            </Field>
          </Card>
          <Card className="p-4">
            {balloon ? (
              <BalloonTools balloon={balloon} geometry={drafts[balloon.id] ?? (balloon.geometry as Geometry)} pageSize={page.size as [number, number]} textValue={texts[balloon.id] ?? balloon.text}
                onGeometry={(g) => changeGeometry(balloon.id, g, true)} onText={(t) => { set({ texts: { ...useEditor.getState().texts, [balloon.id]: t } }); if (wordCount(t) <= 25 && t.trim()) persist(balloon.id, 700); else set({ save: "dirty" }); }} onDelete={() => remove.mutate(balloon.id)} />
            ) : panel ? <PanelTools project={project} page={page} panel={panel} /> : <p className="text-sm text-muted">Select a panel or a balloon on the page, or pick one from the list below.</p>}
          </Card>
          <Card className="p-4">
            <h3 className="mb-2 font-bold">On this page</h3>
            <ol className="space-y-2 text-sm">
              {page.panels.map((p) => (
                <li key={p.id}>
                  <button type="button" aria-pressed={panelId === p.id && !balloonId} onClick={() => set({ panelId: p.id, balloonId: null })} className={cn("w-full rounded-md px-2 py-1 text-left font-semibold hover:bg-ink/5", panelId === p.id && !balloonId && "bg-sun")}>Panel {p.index + 1}{p.status !== "ok" && <span className="ml-2 font-normal text-warn">{p.busy ? "redrawing" : p.status === "needs_attention" ? QA_STATE_LABEL[p.qa_state ?? ""] ?? "needs attention" : p.status.replace("_", " ")}</span>}</button>
                  <ul className="ml-3 border-l-2 border-line pl-2">
                    {p.balloons.map((b) => <li key={b.id}><button type="button" aria-pressed={balloonId === b.id} onClick={() => set({ panelId: p.id, balloonId: b.id })} className={cn("w-full truncate rounded-md px-2 py-1 text-left hover:bg-ink/5", balloonId === b.id && "bg-sun")}><span className="text-muted">{b.type}: </span>{b.flags.includes("overflow") && <span className="font-semibold text-warn">doesn&apos;t fit · </span>}{texts[b.id] ?? b.text}</button></li>)}
                  </ul>
                </li>
              ))}
            </ol>
          </Card>
        </aside>
      </div>
      {project.bible && <ContinuityRules project={project} />}
    </div>
  );
}
