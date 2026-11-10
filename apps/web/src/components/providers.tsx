"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Toaster } from "sonner";
import { ApiError } from "@/lib/api";
import { AuthProvider } from "@/lib/auth";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            refetchOnWindowFocus: true,
            // Retry only what the API marks retryable; never loop on 4xx.
            retry: (count, error) => count < 2 && (!(error instanceof ApiError) || error.retryable),
          },
          mutations: { retry: false },
        },
      }),
  );
  useEffect(() => {
    const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;
    if (!dsn) return;
    // Browser error reporting: no PII, no request bodies, no session replay.
    void import("@sentry/browser").then((Sentry) =>
      Sentry.init({ dsn, tracesSampleRate: 0, environment: process.env.NEXT_PUBLIC_ENV ?? "local", release: process.env.NEXT_PUBLIC_RELEASE }),
    ).catch(() => undefined);
  }, []);
  return (
    <QueryClientProvider client={client}>
      <AuthProvider>{children}</AuthProvider>
      <Toaster position="bottom-right" richColors closeButton />
    </QueryClientProvider>
  );
}
