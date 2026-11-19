"use client";

/** Deterministic rendering for video capture: no timers, no controls; time is set by `window.__heroDemo.seek`. */
import { useEffect, useRef } from "react";
import { applyFrame, Stage } from "./stage";
import { DURATION, frame, type Layout } from "./timeline";

declare global {
  interface Window { __heroDemo?: { seek: (t: number) => void; ready: Promise<void>; duration: number } }
}

const noop = () => {};

export function CaptureStage({ layout, t }: { layout: Layout; t: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current!;
    const seek = (s: number) => applyFrame(el, frame(Math.max(0, Math.min(DURATION, s)), layout));
    const imgs = [...el.querySelectorAll("img")];
    const ready = Promise.all([document.fonts.ready, ...imgs.map((i) => i.decode().catch(noop))]).then(() => { seek(t); });
    window.__heroDemo = { seek, ready, duration: DURATION };
    seek(t);
  }, [layout, t]);
  return (
    <div style={{ background: "var(--color-paper)", width: "fit-content" }} data-capture-root>
      <Stage ref={ref} layout={layout} capture
        h={{ interactive: false, stop: -1, balloonOk: null, endActive: false, onCreate: noop, onApproveCast: noop, onEdit: noop, onApproveScript: noop,
          onBalloonPointerDown: noop, onBalloonKey: noop, onPlaceBalloon: noop, onReplay: noop }} />
    </div>
  );
}
