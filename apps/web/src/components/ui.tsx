"use client";

/**
 * UI primitives in the shadcn/ui pattern: Radix primitives + class-variance-authority +
 * tailwind-merge, owned in this repository rather than imported as a styled library.
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { ChevronDown, Loader2, X } from "lucide-react";
import { forwardRef, useEffect, useId, useState, type ButtonHTMLAttributes, type HTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export { cn };

const button = cva(
  "inline-flex items-center justify-center gap-2 rounded-lg border font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 whitespace-nowrap",
  {
    variants: {
      variant: {
        primary: "border-accent bg-accent text-accent-ink shadow-raised hover:border-accent-strong hover:bg-accent-strong",
        secondary: "border-line bg-card text-ink shadow-raised hover:border-field hover:bg-paper",
        ghost: "border-transparent bg-transparent text-ink hover:bg-ink/5",
        danger: "border-danger/40 bg-card text-danger hover:border-danger hover:bg-danger/5",
      },
      size: { sm: "h-9 px-3 text-sm", md: "h-11 px-4 text-sm", lg: "h-12 px-6 text-base", icon: "h-10 w-10" },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof button> & { asChild?: boolean; busy?: boolean };
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({ className, variant, size, asChild, busy, children, disabled, ...props }, ref) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp ref={ref} className={cn(button({ variant, size }), className)} disabled={disabled || busy} aria-busy={busy || undefined} {...props}>
      {asChild ? children : <>{busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}{children}</>}
    </Comp>
  );
});

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-xl border border-line bg-card shadow-raised", className)} {...props} />;
}

const badge = cva("inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold leading-5", {
  variants: {
    tone: {
      neutral: "border-line bg-paper text-muted",
      working: "border-accent/25 bg-accent/10 text-accent",
      action: "border-accent/25 bg-accent-soft text-accent",
      ok: "border-ok/30 bg-ok/10 text-ok",
      warn: "border-warn/30 bg-warn/10 text-warn",
      danger: "border-danger/30 bg-danger/10 text-danger",
    },
  },
  defaultVariants: { tone: "neutral" },
});
export function Badge({ className, tone, ...props }: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badge>) {
  return <span className={cn(badge({ tone }), className)} {...props} />;
}

const field = "w-full rounded-lg border border-field bg-card px-3 py-2 text-sm text-ink placeholder:text-muted/80 transition-colors focus-visible:border-accent focus-visible:outline-accent disabled:cursor-not-allowed disabled:bg-paper disabled:opacity-60 aria-[invalid=true]:border-danger";
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...props }, ref) {
  return <input ref={ref} className={cn(field, "h-11", className)} {...props} />;
});
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...props }, ref) {
  return <textarea ref={ref} className={cn(field, "min-h-24 resize-y leading-relaxed", className)} {...props} />;
});
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, ...props }, ref) {
  return <select ref={ref} className={cn(field, "h-11 pr-8", className)} {...props} />;
});

/** Label + control + hint/error, wired together for assistive technology. */
export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string | null; children: (ids: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode }) {
  const id = useId();
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-sm font-semibold text-ink">{label}</label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {error ? <p id={`${id}-error`} role="alert" className="text-sm text-danger">{error}</p> : hint ? <p id={`${id}-hint`} className="text-sm text-muted">{hint}</p> : null}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-2 text-sm text-muted">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      <span>{label}…</span>
    </div>
  );
}

export function Notice({ tone = "neutral", title, children, action }: { tone?: "neutral" | "warn" | "danger" | "ok"; title: string; children?: ReactNode; action?: ReactNode }) {
  const tones = { neutral: "border-line bg-card", warn: "border-warn/30 border-l-warn bg-warn/5", danger: "border-danger/30 border-l-danger bg-danger/5", ok: "border-ok/30 border-l-ok bg-ok/5" };
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex flex-wrap items-start justify-between gap-3 rounded-xl border border-l-4 p-4", tones[tone])}>
      <div className="min-w-0 space-y-1">
        <p className="font-semibold">{title}</p>
        {children && <div className="text-sm text-muted">{children}</div>}
      </div>
      {action}
    </div>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-field/60 bg-card px-6 py-14 text-center">
      <h2 className="text-xl">{title}</h2>
      {children && <p className="mx-auto mt-2 max-w-md text-sm text-muted">{children}</p>}
      {action && <div className="mt-6 flex justify-center">{action}</div>}
    </div>
  );
}

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;
export function DialogContent({ title, description, children, className }: { title: string; description?: string; children: ReactNode; className?: string }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-ink/40" />
      <DialogPrimitive.Content className={cn("fixed left-1/2 top-1/2 z-50 max-h-[90dvh] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl border border-line bg-card p-6 shadow-float", className)}>
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <DialogPrimitive.Title className="font-display text-xl font-semibold">{title}</DialogPrimitive.Title>
            {description ? <DialogPrimitive.Description className="mt-1 text-sm text-muted">{description}</DialogPrimitive.Description> : <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>}
          </div>
          <DialogPrimitive.Close asChild>
            <Button variant="ghost" size="icon" aria-label="Close dialog"><X className="h-4 w-4" aria-hidden /></Button>
          </DialogPrimitive.Close>
        </div>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

/** A button that shows or hides a region. The region's children are rendered only while open, so
 *  hidden fields never take focus or count as "covered" (see e2e/sticky.spec.ts). `forceOpen` opens
 *  it when its content has something the user must see, such as a validation error. */
export function Disclosure({ label, summary, children, defaultOpen = false, forceOpen = false, className }: { label: string; summary?: ReactNode; children: ReactNode; defaultOpen?: boolean; forceOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen || forceOpen);
  const id = useId();
  useEffect(() => { if (forceOpen) setOpen(true); }, [forceOpen]);
  return (
    <div className={cn("rounded-lg border border-line", className)}>
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)} className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-semibold hover:bg-ink/[0.03]">
        <span className="min-w-0">{label}{summary && <span className="ml-2 font-normal text-muted">{summary}</span>}</span>
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      <div id={id} hidden={!open}>{open && <div className="space-y-3 border-t border-line p-3">{children}</div>}</div>
    </div>
  );
}

/** A labelled page header: one h1, an optional lead and actions. */
export function PageHeader({ title, lead, actions, eyebrow }: { title: ReactNode; lead?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0 max-w-3xl">
        {eyebrow && <p className="mb-1 text-sm font-semibold text-accent">{eyebrow}</p>}
        <h1 className="text-3xl sm:text-4xl">{title}</h1>
        {lead && <p className="mt-2 text-base text-muted">{lead}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}
