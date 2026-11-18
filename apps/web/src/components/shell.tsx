"use client";

import { useQuery } from "@tanstack/react-query";
import { Coins, LayoutGrid, LogOut, Plus, Settings } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { api } from "@/lib/api";
import { signedOutHere, useAuth } from "@/lib/auth";
import { Wordmark } from "./marketing";
import { Button, Spinner, cn } from "./ui";
import { pk } from "@/lib/session";

export function useMe() {
  const { signedIn } = useAuth();
  return useQuery({ queryKey: pk("me"), enabled: signedIn, queryFn: async () => (await api.GET("/v1/me")).data! });
}

export function AppShell({ children }: { children: ReactNode }) {
  const { ready, signedIn, signOut } = useAuth();
  const router = useRouter();
  const path = usePathname();
  const me = useMe();
  // Someone who arrives signed out comes back to the page they asked for after signing in; after a
  // sign-out in this tab, never (see signedOutHere).
  useEffect(() => {
    if (ready && !signedIn) router.replace(signedOutHere() ? "/sign-in" : `/sign-in?next=${encodeURIComponent(path)}`);
  }, [ready, signedIn, router, path]);
  if (!ready || !signedIn) return <div className="flex min-h-dvh items-center justify-center"><Spinner label="Checking your session" /></div>;

  const nav = [
    { href: "/dashboard", label: "Comics", icon: LayoutGrid },
    { href: "/billing", label: "Credits", icon: Coins },
    { href: "/settings", label: "Settings", icon: Settings },
  ];
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b border-line bg-card/95 backdrop-blur-sm">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-1.5 px-4 sm:gap-3 sm:px-6">
          <Link href="/dashboard" className="mr-1 rounded-md sm:mr-2" aria-label="StoryPanel home"><Wordmark className="text-[1.35rem] leading-none sm:text-[1.6rem]" /></Link>
          <nav aria-label="Main" className="flex items-center gap-1">
            {nav.map(({ href, label, icon: Icon }) => {
              const current = path.startsWith(href);
              return (
                <Link key={href} href={href} aria-current={current ? "page" : undefined}
                  className={cn("flex h-10 items-center gap-2 rounded-lg px-3 text-sm font-medium text-muted hover:bg-ink/5 hover:text-ink", current && "bg-accent-soft text-accent hover:bg-accent-soft hover:text-accent")}>
                  <Icon className="h-4 w-4" aria-hidden /><span className="hidden sm:inline">{label}</span><span className="sr-only sm:hidden">{label}</span>
                </Link>
              );
            })}
          </nav>
          <Link href="/billing" className="ml-auto hidden rounded-full border border-line bg-paper px-3 py-1 text-sm text-ink hover:border-field md:block">
            {me.data ? <><span className="font-semibold tabular-nums">{me.data.available}</span> credits<span className="sr-only"> available</span>{me.data.reserved ? <span className="text-muted"> · {me.data.reserved} held<span className="sr-only"> for work in progress</span></span> : null}</> : <span aria-label="Credits loading">…</span>}
          </Link>
          <Button asChild variant="primary" size="sm" className="ml-auto max-sm:w-9 max-sm:px-0 md:ml-0"><Link href="/new"><Plus className="h-4 w-4" aria-hidden /><span className="max-sm:sr-only">New comic</span></Link></Button>
          <Button variant="ghost" size="icon" aria-label="Sign out" onClick={() => void signOut()}><LogOut className="h-4 w-4" aria-hidden /></Button>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-8 sm:px-6 sm:py-10">{children}</main>
    </div>
  );
}
