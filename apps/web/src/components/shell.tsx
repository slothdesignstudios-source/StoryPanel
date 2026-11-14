"use client";

import { useQuery } from "@tanstack/react-query";
import { Coins, LayoutGrid, LogOut, Plus, Settings } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
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
  useEffect(() => {
    if (ready && !signedIn) router.replace("/sign-in");
  }, [ready, signedIn, router]);
  if (!ready || !signedIn) return <div className="flex min-h-dvh items-center justify-center"><Spinner label="Checking your session" /></div>;

  const nav = [
    { href: "/dashboard", label: "Comics", icon: LayoutGrid },
    { href: "/billing", label: "Credits", icon: Coins },
    { href: "/settings", label: "Settings", icon: Settings },
  ];
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b-2 border-ink bg-paper/95 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-2 px-4 sm:gap-4">
          <Link href="/dashboard" className="font-sfx text-2xl tracking-wide text-accent sm:text-3xl" aria-label="StoryPanel home">StoryPanel</Link>
          <nav aria-label="Main" className="ml-auto flex items-center gap-1">
            {nav.map(({ href, label, icon: Icon }) => (
              <Link key={href} href={href} aria-current={path.startsWith(href) ? "page" : undefined}
                className={cn("flex h-10 items-center gap-2 rounded-md px-3 text-sm font-semibold hover:bg-ink/5", path.startsWith(href) && "bg-ink text-paper hover:bg-ink")}>
                <Icon className="h-4 w-4" aria-hidden /><span className="hidden sm:inline">{label}</span><span className="sr-only sm:hidden">{label}</span>
              </Link>
            ))}
          </nav>
          <Link href="/billing" className="hidden rounded-full border-2 border-ink bg-sun px-3 py-1 text-sm font-bold md:block">
            {me.data ? <>{me.data.available} credits<span className="sr-only"> available</span>{me.data.reserved ? <span className="font-normal"> · {me.data.reserved} held<span className="sr-only"> for work in progress</span></span> : null}</> : <span aria-label="Credits loading">…</span>}
          </Link>
          <Button asChild variant="primary" size="sm"><Link href="/new"><Plus className="h-4 w-4" aria-hidden />New comic</Link></Button>
          <Button variant="ghost" size="icon" aria-label="Sign out" onClick={() => void signOut()}><LogOut className="h-4 w-4" aria-hidden /></Button>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-7xl px-4 py-8">{children}</main>
    </div>
  );
}
