"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui";

export default function ErrorBoundary({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    void import("@sentry/browser").then((s) => s.captureException(error)).catch(() => undefined);
  }, [error]);
  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-md flex-col items-center justify-center gap-4 p-6 text-center">
      <p className="font-sfx text-5xl text-accent">Oops!</p>
      <h1 className="text-2xl font-bold">This page hit a problem</h1>
      <p className="text-sm text-muted">Your comics are safe. Try again, or go back to your dashboard.</p>
      {error.digest && <p className="text-xs text-muted">Reference: {error.digest}</p>}
      <div className="flex gap-3">
        <Button variant="primary" onClick={reset}>Try again</Button>
        <Button asChild><a href="/dashboard">Dashboard</a></Button>
      </div>
    </main>
  );
}
