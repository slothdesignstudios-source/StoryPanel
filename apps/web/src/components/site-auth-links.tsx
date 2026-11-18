"use client";

import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { Button } from "./ui";

/** Header actions on public pages: "Your comics" when signed in, otherwise "Sign in". The primary
 *  action is always "Start a comic" (signed-out visitors are asked to sign in first, then return). */
export function SiteAuthLinks() {
  const { ready, signedIn } = useAuth();
  return (
    <>
      {ready && signedIn ? (
        <Link href="/dashboard" className="hidden rounded-md px-3 py-2 text-sm font-medium text-ink hover:bg-ink/5 sm:block">Your comics</Link>
      ) : (
        <Link href="/sign-in" className="rounded-md px-3 py-2 text-sm font-medium text-ink hover:bg-ink/5">Sign in</Link>
      )}
      <Button asChild variant="primary" size="sm"><Link href="/new">Start a comic</Link></Button>
    </>
  );
}
