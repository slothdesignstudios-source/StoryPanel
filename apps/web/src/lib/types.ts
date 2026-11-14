import type { components } from "@api-types";

type S = components["schemas"];
export type Project = S["ProjectOut"];
export type ProjectSummary = S["ProjectSummary"];
export type Character = S["CharacterOut"];
export type Page = S["PageOut"];
export type Panel = S["PanelOut"];
export type Balloon = S["BalloonOut"];
export type Script = S["ComicScript"];
export type PanelSpec = S["PanelSpec"];
export type DialogueItem = S["DialogueItem"];
export type Estimate = S["EstimateOut"];
export type Credits = S["CreditsOut"];
export type Meta = S["MetaOut"];
export type Version = S["VersionOut"];
export type ExportOut = S["ExportOut"];
/** Balloon geometry in page units. `lines` and `text_size` are the server's typesetting
 *  (lettering.typeset): render them as-is, never re-wrap. */
export type Geometry = { x: number; y: number; w: number; h: number; font_size: number; rotation?: number; tail?: { x: number; y: number } | null; text_size?: number; lines?: string[] };

export const ACTIVE = new Set(["queued", "intake", "awaiting_answers", "planning", "awaiting_cast_approval", "scripting", "awaiting_script_approval", "illustrating"]);
export const WORKING = new Set(["queued", "intake", "planning", "scripting", "illustrating"]);

export const STATUS_LABEL: Record<string, string> = {
  queued: "Starting", intake: "Reading your idea", awaiting_answers: "Needs your answers",
  planning: "Designing the cast", awaiting_cast_approval: "Cast ready for review",
  scripting: "Writing the script", awaiting_script_approval: "Script ready for review",
  illustrating: "Drawing", completed: "Finished", partially_completed: "Finished with gaps",
  failed: "Stopped", cancelled: "Cancelled", blocked: "Not allowed", expired: "Waiting too long",
};

/** Status text that keeps "the work finished" apart from "every panel passed its checks". */
export function projectStatus(p: { status: string; panel_counts?: Record<string, number> }): { label: string; review: number } {
  const c = p.panel_counts ?? {};
  const review = c.needs_attention ?? 0;
  if (p.status === "completed" && review > 0) return { label: `Drawn · ${review} panel${review > 1 ? "s" : ""} to review`, review };
  if (p.status === "partially_completed") return { label: review ? `Drawn with gaps · ${review} to review` : "Drawn with gaps", review };
  return { label: STATUS_LABEL[p.status] ?? p.status, review };
}

/** Short state of one panel's checks, for lists and badges. */
export const QA_STATE_LABEL: Record<string, string> = { passed: "passed checks", issues: "problems found", uncertain: "couldn't confirm", not_checked: "not checked" };

