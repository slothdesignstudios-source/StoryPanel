"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { toast } from "sonner";
import { statusTone } from "../../dashboard/page";
import { Editor } from "@/components/project/editor";
import { Cast, Questions, ScriptReview, Stopped, Working } from "@/components/project/stages";
import { Badge, Button, Dialog, DialogClose, DialogContent, DialogTrigger, Notice, Spinner } from "@/components/ui";
import { ApiError, api, describeError } from "@/lib/api";
import { useProjectEvents } from "@/lib/events";
import { ACTIVE, STATUS_LABEL, WORKING, projectStatus, type Project } from "@/lib/types";
import { FlowSteps } from "@/components/project/flow";
import { pk } from "@/lib/session";

function Stage({ project }: { project: Project }) {
  switch (project.status) {
    case "awaiting_answers": return <Questions project={project} />;
    case "awaiting_cast_approval": return <Cast project={project} />;
    case "awaiting_script_approval": return project.script ? <ScriptReview project={project} /> : <Working project={project} />;
    case "completed":
    case "partially_completed": return project.pages.length ? <Editor project={project} /> : <Stopped project={project} />;
    case "failed": case "cancelled": case "blocked": case "expired": return <Stopped project={project} />;
    default: return <Working project={project} />;
  }
}

const PROVIDER_LABEL: Record<string, string> = { openai: "OpenAI", gemini: "Google Gemini", fal: "fal" };

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: pk("project", id),
    queryFn: async () => (await api.GET("/v1/projects/{project_id}", { params: { path: { project_id: id } } })).data!,
    // Events drive updates; this slow poll is the safety net if the stream is blocked.
    refetchInterval: (query) => (query.state.data && WORKING.has(query.state.data.status) ? 8000 : false),
  });
  const live = !!q.data;
  const stream = useProjectEvents(id, live, (type, data) => {
    if (type === "character.revision_rejected" || type === "script.rejected") toast.error(String(data.message ?? "That change wasn't allowed."));
    if (type === "panel.updated" && data.result === "failed" && data.kind !== "generate") toast.error("That panel change didn't work. You weren't charged.");
    if (type === "export.updated" && data.status === "ready") toast.success("Your export is ready to download.");
  });
  const cancel = useMutation({ mutationFn: async () => (await api.POST("/v1/projects/{project_id}/cancel", { params: { path: { project_id: id } } })).data, onSuccess: () => void qc.invalidateQueries({ queryKey: pk("project", id) }), onError: (e) => toast.error(describeError(e)) });
  const remove = useMutation({ mutationFn: async () => api.DELETE("/v1/projects/{project_id}", { params: { path: { project_id: id } } }), onSuccess: () => { void qc.invalidateQueries({ queryKey: pk("projects") }); router.push("/dashboard"); }, onError: (e) => toast.error(describeError(e)) });

  if (q.isPending) return <Spinner label="Loading your comic" />;
  if (q.isError) {
    const missing = q.error instanceof ApiError && q.error.status === 404;
    return <Notice tone="danger" title={missing ? "We can't find that comic" : "We couldn't load this comic"} action={missing ? <Button onClick={() => router.push("/dashboard")}>Back to comics</Button> : <Button onClick={() => void q.refetch()}>Try again</Button>}>{missing ? "It may have been deleted, or it belongs to another account." : describeError(q.error)}</Notice>;
  }
  const project = q.data;
  const review = projectStatus(project).review;
  return (
    <div className="space-y-8">
      <div className="space-y-4 border-b border-line pb-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="truncate text-3xl sm:text-4xl">{project.title}</h1>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge tone={statusTone(project.status)}>{STATUS_LABEL[project.status] ?? project.status}</Badge>
              {review > 0 && <Badge tone="warn">{review} panel{review > 1 ? "s" : ""} to review</Badge>}
              <span className="text-xs text-muted">Illustrated with {PROVIDER_LABEL[project.image_provider ?? "fal"] ?? project.image_provider}{project.image_model ? ` (${project.image_model})` : ""}</span>
              {ACTIVE.has(project.status) && <span className="text-xs text-muted" role="status">{stream === "live" ? "Live updates on" : stream === "reconnecting" ? "Reconnecting… your comic is still running" : "Connecting…"}</span>}
            </div>
          </div>
          <div className="flex gap-2">
            {ACTIVE.has(project.status) && (
              <Dialog>
                <DialogTrigger asChild><Button size="sm">Cancel comic</Button></DialogTrigger>
                <DialogContent title="Cancel this comic?" description="Work in progress stops. Credits held for unfinished work are released; finished steps stay charged.">
                  <div className="flex justify-end gap-3"><DialogClose asChild><Button variant="ghost">Keep going</Button></DialogClose><DialogClose asChild><Button variant="danger" onClick={() => cancel.mutate()}>Cancel comic</Button></DialogClose></div>
                </DialogContent>
              </Dialog>
            )}
            <Dialog>
              <DialogTrigger asChild><Button size="sm" variant="ghost">Delete</Button></DialogTrigger>
              <DialogContent title="Delete this comic?" description="The comic, its images and its exports are removed. This can't be undone.">
                <div className="flex justify-end gap-3"><DialogClose asChild><Button variant="ghost">Keep it</Button></DialogClose><Button variant="danger" busy={remove.isPending} onClick={() => remove.mutate()}>Delete comic</Button></div>
              </DialogContent>
            </Dialog>
          </div>
        </div>
        <FlowSteps status={project.status} />
      </div>
      <Stage project={project} />
    </div>
  );
}
