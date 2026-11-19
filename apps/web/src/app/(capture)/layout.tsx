import type { ReactNode } from "react";

/** Bare pages for tooling (video capture). No site chrome; never indexed. */
export default function CaptureLayout({ children }: { children: ReactNode }) {
  return <main id="main">{children}</main>;
}
