"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Badge, Button, Card, Field, Notice, Select, Textarea, cn } from "@/components/ui";
import { ApiError, api, describeError } from "@/lib/api";
import { pk } from "@/lib/session";
import type { Project } from "@/lib/types";

type Rule = { id?: string | null; text: string; applies_to: "every_panel" | "character" | "location"; target?: string | null; strength: "must" | "prefer"; visual: boolean; source: "model" | "world_rule" | "user"; world_rule_index?: number | null };

/** Suggest a scope for a world rule from the cast names it mentions. Only a suggestion: the user
 *  sees and changes it before saving. The rule's wording is never changed. */
function suggestScope(text: string, project: Project): Pick<Rule, "applies_to" | "target"> {
  const named = project.characters.find((c) => new RegExp(`\\b${c.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(text));
  return named ? { applies_to: "character", target: named.slug } : { applies_to: "every_panel", target: null };
}

/**
 * Continuity rules: what every drawing must keep (a prop a character always carries, the weather
 * in a place). Editable before the cast is approved and once the comic is drawn (then they apply
 * to later redraws and fixes). World rules can be classified into rules; their wording is kept.
 */
export function ContinuityRules({ project, onDirty }: { project: Project; onDirty?: (dirty: boolean) => void }) {
  const qc = useQueryClient();
  const bible = project.bible!;
  const fromServer = (): Rule[] => (bible.continuity_rules ?? []).map((r) => ({ ...r, source: r.source ?? "model", strength: r.strength ?? "must", visual: r.visual ?? true }));
  const [rules, setRules] = useState<Rule[]>(fromServer);
  useEffect(() => setRules(fromServer()), [project.cast_version, JSON.stringify(bible.continuity_rules)]); // eslint-disable-line react-hooks/exhaustive-deps -- reset when the saved rules change
  const dirty = JSON.stringify(rules) !== JSON.stringify(fromServer());
  const report = useRef(onDirty);
  useEffect(() => { report.current = onDirty; });
  useEffect(() => report.current?.(dirty), [dirty]);
  const editable = ["awaiting_cast_approval", "completed", "partially_completed"].includes(project.status);
  const save = useMutation({
    mutationFn: async () => (await api.PUT("/v1/projects/{project_id}/continuity-rules", { params: { path: { project_id: project.id } }, body: { cast_version: project.cast_version, rules: rules.map((r) => ({ ...r, target: r.applies_to === "every_panel" ? null : r.target })) } })).data,
    onSuccess: () => { void qc.invalidateQueries({ queryKey: pk("project", project.id) }); toast.success("Continuity rules saved"); },
    onError: (e) => { if (e instanceof ApiError && e.code === "version_conflict") void qc.invalidateQueries({ queryKey: pk("project", project.id) }); },
  });
  const unclassified = bible.world_rules.map((text, index) => ({ text, index })).filter(({ index }) => !rules.some((r) => r.source === "world_rule" && r.world_rule_index === index));
  const set = (n: number, patch: Partial<Rule>) => setRules(rules.map((r, i) => (i === n ? { ...r, ...patch } : r)));
  const fieldError = (n: number) => (save.error instanceof ApiError ? save.error.fields.filter((f) => f.field.startsWith(`rules.${n}.`)).map((f) => f.message).join(" ") || null : null);
  const targets = (r: Rule) => (r.applies_to === "character" ? project.characters.map((c) => [c.slug, c.name] as const) : project.locations.map((l) => [l.slug, l.name] as const));

  return (
    <Card className="space-y-4 p-6" aria-labelledby="continuity-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="continuity-title" className="text-xl font-semibold">Continuity rules</h2>
        <span className="text-sm text-muted" role="status">{save.isPending ? "Saving…" : dirty ? "Unsaved changes" : "Saved"}</span>
      </div>
      <p className="text-sm text-muted">What every drawing must keep. <b>Must</b> rules go into every picture they apply to and are checked in the script and in each panel; <b>Prefer</b> rules are asked for where they fit. Prompts make these likelier, not certain: the checks flag panels that miss them.{project.status !== "awaiting_cast_approval" && " Changes apply to later redraws and fixes; existing pictures aren't redrawn."}</p>
      {rules.length === 0 && <p className="text-sm">No rules yet.</p>}
      <ul className="space-y-3">
        {rules.map((r, n) => (
          <li key={n} className={cn("space-y-2 rounded-md border p-3", fieldError(n) ? "border-danger" : "border-line")}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={r.strength === "must" ? "action" : "neutral"}>{r.strength === "must" ? "Must" : "Prefer"}</Badge>
              {r.source === "world_rule" && <Badge>From a world rule · wording kept</Badge>}
              {!r.visual && <Badge>Story only · not drawn</Badge>}
            </div>
            <Field label={`Rule ${n + 1}`} error={fieldError(n)}>
              {(ids) => <Textarea {...ids} className="min-h-12" maxLength={300} value={r.text} readOnly={r.source === "world_rule" || !editable} onChange={(e) => set(n, { text: e.target.value })} />}
            </Field>
            <div className="grid gap-2 sm:grid-cols-[minmax(10rem,1fr)_minmax(10rem,1fr)_minmax(8rem,auto)_auto] sm:items-end">
              <Field label="Applies to">{(ids) => (
                <Select {...ids} value={r.applies_to} disabled={!editable} onChange={(e) => { const a = e.target.value as Rule["applies_to"]; set(n, { applies_to: a, target: a === "every_panel" ? null : a === "character" ? project.characters[0]?.slug ?? null : project.locations[0]?.slug ?? null }); }}>
                  <option value="every_panel">Every panel</option><option value="character">A character</option><option value="location">A location</option>
                </Select>
              )}</Field>
              <Field label={r.applies_to === "every_panel" ? "Target" : r.applies_to === "character" ? "Character" : "Location"}>{(ids) => (
                <Select {...ids} value={r.target ?? ""} disabled={!editable || r.applies_to === "every_panel"} onChange={(e) => set(n, { target: e.target.value })}>
                  {r.applies_to === "every_panel" ? <option value="">—</option> : targets(r).map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                </Select>
              )}</Field>
              <Field label="Strength">{(ids) => (
                <Select {...ids} value={r.strength} disabled={!editable} onChange={(e) => set(n, { strength: e.target.value as Rule["strength"] })}><option value="must">Must</option><option value="prefer">Prefer</option></Select>
              )}</Field>
              <label className="flex h-11 items-center gap-2 text-sm"><input type="checkbox" checked={r.visual} disabled={!editable} onChange={(e) => set(n, { visual: e.target.checked })} />Visible in pictures</label>
            </div>
            {editable && <Button variant="ghost" size="sm" onClick={() => setRules(rules.filter((_, i) => i !== n))}><Trash2 className="h-4 w-4" aria-hidden />Remove rule</Button>}
          </li>
        ))}
      </ul>
      {unclassified.length > 0 && (
        <div className="space-y-2 rounded-md border border-dashed border-line p-3">
          <p className="text-sm font-semibold">World rules not used in pictures yet</p>
          <p className="text-xs text-muted">Choose which of these the pictures must show. The rule keeps its exact wording; you choose what it applies to and how strict it is.</p>
          <ul className="space-y-2">
            {unclassified.map(({ text, index }) => (
              <li key={index} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <span className="min-w-0 flex-1">{text}</span>
                <Button size="sm" disabled={!editable} onClick={() => setRules([...rules, { text, source: "world_rule", world_rule_index: index, strength: "must", visual: true, ...suggestScope(text, project) }])}><Plus className="h-4 w-4" aria-hidden />Use in pictures</Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {save.isError && <Notice tone="danger" title={save.error instanceof ApiError && save.error.code === "version_conflict" ? "This was changed in another window" : "Couldn't save the rules"}>{describeError(save.error)}</Notice>}
      {editable && (
        <div className="flex flex-wrap gap-3">
          <Button size="sm" onClick={() => setRules([...rules, { text: "", applies_to: "every_panel", target: null, strength: "must", visual: true, source: "user" }])} disabled={rules.length >= 12}><Plus className="h-4 w-4" aria-hidden />Add a rule</Button>
          <Button busy={save.isPending} disabled={!dirty || rules.some((r) => !r.text.trim())} onClick={() => save.mutate()}>Save rules</Button>
          <Button variant="ghost" disabled={!dirty || save.isPending} onClick={() => setRules(fromServer())}>Discard changes</Button>
        </div>
      )}
    </Card>
  );
}
