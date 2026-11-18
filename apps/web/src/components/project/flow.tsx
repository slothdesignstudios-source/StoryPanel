import { Check } from "lucide-react";
import { cn } from "@/lib/cn";

const STEPS = ["Idea", "Cast", "Script", "Drawing", "Edit & export"] as const;

/** Which step of the creation flow a comic is on. Workflow progress only: quality review is shown
 *  separately, so "finished" never hides panels that still need a look. */
function stepOf(status: string): number {
  if (["queued", "intake", "awaiting_answers"].includes(status)) return 0;
  if (["planning", "awaiting_cast_approval"].includes(status)) return 1;
  if (["scripting", "awaiting_script_approval"].includes(status)) return 2;
  if (status === "illustrating") return 3;
  return 4;
}

export function FlowSteps({ status }: { status: string }) {
  const stopped = ["failed", "cancelled", "blocked", "expired"].includes(status);
  const current = stepOf(status);
  const done = (i: number) => i < current || (i === 4 && (status === "completed" || status === "partially_completed"));
  return (
    <ol aria-label="Creation steps" className="flex flex-wrap items-center gap-x-1 gap-y-2 text-sm">
      {STEPS.map((label, i) => {
        const isCurrent = i === current && !done(i) && !stopped;
        return (
          <li key={label} className="flex items-center gap-1" aria-current={isCurrent ? "step" : undefined}>
            <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium",
              done(i) ? "text-ok" : isCurrent ? "bg-accent-soft text-accent" : "text-muted")}>
              {done(i) ? <Check className="h-3.5 w-3.5" aria-hidden /> : <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", isCurrent ? "bg-accent" : "bg-field/60")} />}
              {label}
              <span className="sr-only">{done(i) ? " (done)" : isCurrent ? " (current step)" : ""}</span>
            </span>
            {i < STEPS.length - 1 && <span aria-hidden className="h-px w-3 bg-line sm:w-5" />}
          </li>
        );
      })}
    </ol>
  );
}
