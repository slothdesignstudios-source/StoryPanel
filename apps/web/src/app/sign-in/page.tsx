"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { Button, Card, Field, Input } from "@/components/ui";
import { AUTH_MODE, ClerkSignIn, useAuth } from "@/lib/auth";

export default function SignInPage() {
  const { ready, signedIn, devSignIn } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (ready && signedIn) router.replace("/dashboard");
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
    <main id="main" className="halftone flex min-h-dvh items-center justify-center p-4">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center">
          <p className="font-sfx text-5xl tracking-wide text-accent">StoryPanel</p>
          <h1 className="mt-2 text-xl font-bold">One idea in. A finished comic out.</h1>
        </div>
        {AUTH_MODE === "clerk" ? (
          <div className="flex justify-center"><ClerkSignIn /></div>
        ) : (
          <Card className="p-6">
            <form onSubmit={submit} className="space-y-4" noValidate>
              <Field label="Email" hint="Local development sign-in. No password, no email is sent." error={error}>
                {(ids) => <Input {...ids} type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />}
              </Field>
              <Button type="submit" variant="primary" size="lg" className="w-full" busy={busy} disabled={!/^[^@\s]+@[^@\s]+$/.test(email.trim())}>Sign in</Button>
            </form>
          </Card>
        )}
      </div>
    </main>
  );
}
