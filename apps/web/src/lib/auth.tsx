"use client";

import { ClerkProvider, SignIn, useAuth as useClerkAuth, useClerk } from "@clerk/clerk-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { API_URL, setTokenSource } from "./api";

export const AUTH_MODE = process.env.NEXT_PUBLIC_AUTH_MODE === "clerk" ? "clerk" : "dev";
const KEY = "sp.dev.session";

type Auth = { ready: boolean; signedIn: boolean; email: string | null; signOut: () => Promise<void>; devSignIn?: (email: string) => Promise<void> };
const Ctx = createContext<Auth>({ ready: false, signedIn: false, email: null, signOut: async () => {} });
export const useAuth = () => useContext(Ctx);

/** Local development sign-in. The API only serves this when SP_AUTH_MODE=dev outside staging/production. */
function DevAuth({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ token: string; email: string; exp: number } | null>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (parsed && parsed.exp > Date.now()) setSession(parsed);
    } catch {
      /* ignore unreadable storage */
    }
    setReady(true);
  }, []);
  // Set during render, not in an effect: child queries start in their own effects, which
  // run before a parent's, and must already see the token.
  setTokenSource(async () => session?.token ?? null);
  const devSignIn = useCallback(async (email: string) => {
    const res = await fetch(`${API_URL}/v1/dev/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
    if (!res.ok) throw new Error("Sign-in failed. Check the email address and try again.");
    const body = (await res.json()) as { token: string; expires_in: number };
    const next = { token: body.token, email, exp: Date.now() + body.expires_in * 1000 - 60_000 };
    setTokenSource(async () => next.token);
    sessionStorage.setItem(KEY, JSON.stringify(next));
    setSession(next);
  }, []);
  const signOut = useCallback(async () => {
    sessionStorage.removeItem(KEY);
    setSession(null);
  }, []);
  const value = useMemo(() => ({ ready, signedIn: !!session, email: session?.email ?? null, signOut, devSignIn }), [ready, session, signOut, devSignIn]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useClerkAuth();
  const clerk = useClerk();
  setTokenSource(() => getToken());
  const value = useMemo(
    () => ({ ready: isLoaded, signedIn: !!isSignedIn, email: clerk.user?.primaryEmailAddress?.emailAddress ?? null, signOut: () => clerk.signOut() }),
    [isLoaded, isSignedIn, clerk],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  if (AUTH_MODE === "clerk") {
    return (
      <ClerkProvider publishableKey={process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY ?? ""} afterSignOutUrl="/sign-in">
        <ClerkBridge>{children}</ClerkBridge>
      </ClerkProvider>
    );
  }
  return <DevAuth>{children}</DevAuth>;
}

export function ClerkSignIn() {
  return <SignIn routing="hash" fallbackRedirectUrl="/dashboard" />;
}
