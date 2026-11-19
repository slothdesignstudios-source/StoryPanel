import type { Metadata } from "next";
import { CaptureStage } from "@/components/hero-demo/capture";

export const metadata: Metadata = { title: "Hero demo capture", robots: { index: false, follow: false } };

/** Video capture of the hero demo: /demo/capture?layout=landscape|portrait&t=12.5
 *  The page renders one frame at a time; scripts/capture-hero-demo.mjs seeks with window.__heroDemo. */
export default async function CapturePage({ searchParams }: { searchParams: Promise<{ layout?: string; t?: string }> }) {
  const sp = await searchParams;
  const layout = sp.layout === "portrait" ? "portrait" : "landscape";
  const t = Number.isFinite(Number(sp.t)) ? Number(sp.t) : 0;
  return <CaptureStage layout={layout} t={t} />;
}
