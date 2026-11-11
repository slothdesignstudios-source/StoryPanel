"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useMemo, type ReactNode } from "react";
import { Toaster } from "sonner";
import { ApiError } from "@/lib/api";
import { AuthProvider, useAuth } from "@/lib/auth";
import { setIdentity } from "@/lib/session";

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: true,
        // Retry only what the API marks retryable; never loop on 4xx.
        retry: (count, error) => count < 2 && (!(error instanceof ApiError) || error.retryable),
      },
      mutations: { retry: false },
    },
  });
}

/**
 * One QueryClient per identity. On a change the previous client's queries are cancelled
 * and its cache cleared, and the whole app subtree remounts (discarding component state),
 * so nothing cached for one person can render for the next. See lib/session.ts.
 */
function SessionScope({ children }: { children: ReactNode }) {
  const { identity } = useAuth();
  setIdentity(identity); // during render, before any child query runs under the new identity
  const client = useMemo(() => makeClient(), [identity]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(
    () => () => {
      void client.cancelQueries();
      client.clear();
    },
    [client],
  );
  return (
    <QueryClientProvider client={client} key={identity ?? "anonymous"}>
      {children}
    </QueryClientProvider>
  );
}

export function Providers({ children }: { children: ReactNode }) {
  useEffect(() => {
    const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
    if (!dsn) return;
    // Browser error reporting: no PII, no request bodies, no session replay.
    void import("@sentry/browser").then((Sentry) =>
      Sentry.init({ dsn, tracesSampleRate: 0, environment: process.env.NEXT_PUBLIC_ENV ?? "local", release: process.env.NEXT_PUBLIC_RELEASE }),
    ).catch(() => undefined);
  }, []);
  return (
    <AuthProvider>
      <SessionScope>{children}</SessionScope>
      <Toaster position="bottom-right" richColors closeButton />
    </AuthProvider>
  );
}
