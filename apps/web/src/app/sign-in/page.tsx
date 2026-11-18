"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Wordmark } from "@/components/marketing";
import { useEffect, useState, type FormEvent } from "react";
import { Button, Card, Field, Input } from "@/components/ui";
import { AUTH_MODE, ClerkSignIn, signedOutHere, useAuth } from "@/lib/auth";

/** Where to go after signing in: the page that asked, but only a path on this site. */
function safeNext(): string {
  if (typeof window === "undefined" || signedOutHere()) return "/dashboard";
  const next = new URLSearchParams(window.location.search).get("next") ?? "";
  return next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/\\") && !next.startsWith("/sign-in") ? next : "/dashboard";
}

export default function SignInPage() {
  const { ready, signedIn, devSignIn } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (ready && signedIn) router.replace(safeNext());
  }, [ready, signedIn, router]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await devSignIn?.(email.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main id="main" className="flex min-h-dvh flex-col bg-paper">
      <div className="mx-auto flex w-full max-w-6xl items-center px-4 py-5 sm:px-6">
        <Link href="/" aria-label="StoryPanel home" className="rounded-md"><Wordmark className="text-[1.65rem] leading-none" /></Link>
      </div>
      <div className="flex flex-1 items-center justify-center px-4 pb-16">
        <div className="w-full max-w-md space-y-8">
          <div className="text-center">
            <h1 className="text-3xl">Sign in to StoryPanel</h1>
            <p className="mt-2 text-muted">Turn your story idea into an editable comic.</p>
          </div>
          {AUTH_MODE === "clerk" ? (
            <div className="flex justify-center"><ClerkSignIn /></div>
          ) : (
            <Card className="p-6 sm:p-8">
              <form onSubmit={submit} className="space-y-5" noValidate>
                <Field label="Email" hint="Local development sign-in. No password, no email is sent." error={error}>
                  {(ids) => <Input {...ids} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />}
                </Field>
                <Button type="submit" variant="primary" size="lg" className="w-full" busy={busy} disabled={!/^[^@\s]+@[^@\s]+$/.test(email.trim())}>Sign in</Button>
              </form>
            </Card>
          )}
          <p className="text-center text-sm text-muted"><Link href="/" className="font-medium text-accent hover:underline">Back to the StoryPanel home page</Link></p>
        </div>
      </div>
    </main>
  );
}
