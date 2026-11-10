"use client";

/**
 * UI primitives in the shadcn/ui pattern: Radix primitives + class-variance-authority +
 * tailwind-merge, owned in this repository rather than imported as a styled library.
 */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { clsx, type ClassValue } from "clsx";
import { Loader2, X } from "lucide-react";
import { forwardRef, useId, type ButtonHTMLAttributes, type HTMLAttributes, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { twMerge } from "tailwind-merge";

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));

const button = cva(
  "inline-flex items-center justify-center gap-2 rounded-md border-2 border-ink font-semibold transition-transform active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink shadow-panel-sm hover:brightness-105",
        secondary: "bg-card text-ink shadow-panel-sm hover:bg-sun/40",
        ghost: "border-transparent bg-transparent hover:bg-ink/5",
        danger: "bg-card text-danger border-danger hover:bg-danger/10",
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
  return <div className={cn("rounded-lg border-2 border-ink bg-card shadow-panel", className)} {...props} />;
}

const badge = cva("inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold", {
  variants: {
    tone: {
      neutral: "border-line bg-paper text-muted",
      working: "border-teal/40 bg-teal/10 text-teal",
      action: "border-ink bg-sun text-ink",
      ok: "border-ok/40 bg-ok/10 text-ok",
      warn: "border-warn/40 bg-warn/10 text-warn",
      danger: "border-danger/40 bg-danger/10 text-danger",
    },
  },
  defaultVariants: { tone: "neutral" },
});
export function Badge({ className, tone, ...props }: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badge>) {
  return <span className={cn(badge({ tone }), className)} {...props} />;
}

const field = "w-full rounded-md border-2 border-ink/80 bg-card px-3 py-2 text-sm placeholder:text-muted/70 disabled:opacity-60 aria-[invalid=true]:border-danger";
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
      <label htmlFor={id} className="block text-sm font-semibold">{label}</label>
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
  const tones = { neutral: "border-line bg-card", warn: "border-warn bg-warn/5", danger: "border-danger bg-danger/5", ok: "border-ok bg-ok/5" };
  return (
    <div role={tone === "danger" ? "alert" : "status"} className={cn("flex flex-wrap items-start justify-between gap-3 rounded-lg border-2 p-4", tones[tone])}>
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
    <div className="halftone rounded-lg border-2 border-dashed border-ink/40 px-6 py-14 text-center">
      <h2 className="text-xl font-bold">{title}</h2>
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
      <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-ink/50" />
      <DialogPrimitive.Content className={cn("fixed left-1/2 top-1/2 z-50 max-h-[90dvh] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border-2 border-ink bg-card p-6 shadow-panel", className)}>
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <DialogPrimitive.Title className="font-display text-xl font-bold">{title}</DialogPrimitive.Title>
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
