import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell";

/** Everything here is private to the signed-in person: never indexed. */
export const metadata: Metadata = { robots: { index: false, follow: false } };

export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
