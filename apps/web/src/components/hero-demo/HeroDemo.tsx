"use client";

/**
 * The landing-page hero demo: a 36-second walk through StoryPanel's workflow using development-fixture
 * artwork. Watch mode plays the timeline; Explore mode steps through it and lets the visitor approve the
 * cast and script and move a balloon. Both modes render `frame(t)` from ./timeline, written to the DOM
 * by `applyFrame` inside one requestAnimationFrame loop, so React does not re-render per frame.
 *
 * Nothing here calls the API: no generation, credits or approvals happen.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type KeyboardEvent as RKeyboardEvent, type PointerEvent as RPointerEvent } from "react";
import { Pause, Play, RotateCcw } from "lucide-react";
import { applyFrame, Stage } from "./stage";
import { BALLOON, balloonClear, DURATION, frame, SCENES, sceneAt, sceneOfStop, STAGE, STOPS, type Layout, type Overrides } from "./timeline";

const STEP_TEXT = [
  "Start with an idea and a format. Press “Create my cast”.",
  "Each character gets identity details used in every panel. Approve the cast.",
  "The Story Bible holds the setting, tone, continuity rules and the reveal.",
  "Edit panel 2's line before anything is drawn: press “Use the edited line”.",
  "The script is saved as version 2. Approve it to start drawing.",
  "The panels are illustrated in story order.",
  "Move the first balloon off the keeper's face: drag it, or focus it and use the arrow keys.",
  "Read the finished page, then choose an export format. Nothing downloads in this demo.",
  "Your idea. Your characters. Your comic.",
];

/** Small external stores for media queries, so the server render and hydration agree. */
function useMedia(query: string): boolean | null {
  return useSyncExternalStore(
    (cb) => { const m = window.matchMedia(query); m.addEventListener("change", cb); return () => m.removeEventListener("change", cb); },
    () => window.matchMedia(query).matches,
    () => null,
  );
}

type Mode = "watch" | "explore";

export function HeroDemo() {
  const narrow = useMedia("(max-width: 559px)");
  const reduced = useMedia("(prefers-reduced-motion: reduce)");
  if (narrow === null || reduced === null) return <Poster />;
  return <Demo layout={narrow ? "portrait" : "landscape"} reduced={reduced} />;
}

/** Shown before the script runs and if it never does: a still of the finished comic, with the controls'
 *  space reserved so nothing moves when the live demo replaces it. */
function Poster() {
  return (
    <div className="w-full">
      <picture className="block aspect-[360/640] w-full min-[560px]:aspect-[640/560]">
        <source media="(max-width: 559px)" srcSet="/hero-demo/poster-portrait.webp" width={720} height={1280} />
        <img src="/hero-demo/poster-landscape.webp" width={1280} height={1120} alt="" className="h-full w-full rounded-2xl border border-line object-cover" fetchPriority="high" />
      </picture>
      <div className="mt-3 space-y-3" aria-hidden>
        <div className="h-1 rounded-full bg-line" />
        <ol className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted">{SCENES.map((s) => <li key={s.id}>{s.label}</li>)}</ol>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="inline-flex rounded-lg border border-line bg-card p-0.5 text-[13px] font-semibold"><span className="rounded-md px-3 py-1.5">Watch</span><span className="rounded-md px-3 py-1.5 text-muted">Try it step by step</span></div>
        </div>
      </div>
    </div>
  );
}

function Demo({ layout, reduced }: { layout: Layout; reduced: boolean }) {
  const { w: W, h: H } = STAGE[layout];
  const wrapRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const [t0] = useState(() => (reduced ? STOPS[STOPS.length - 1]!.t : 0));
  const tRef = useRef(t0);
  const ovRef = useRef<Overrides>({});
  const rafRef = useRef(0);
  const targetRef = useRef<number | null>(null); // Explore: play to this time, then stop
  const autoPausedRef = useRef(false);
  const visibleRef = useRef(false);
  const scaleRef = useRef(1);

  const [mode, setMode] = useState<Mode>(reduced ? "explore" : "watch");
  const [playing, setPlaying] = useState(false);
  const [finished, setFinished] = useState(false);
  const [scene, setScene] = useState(() => sceneAt(t0));
  const [stop, setStop] = useState(reduced ? STOPS.length - 1 : 0);
  const [moving, setMoving] = useState(false); // Explore: travelling between stops
  const [gates, setGates] = useState<Set<string>>(() => new Set(reduced ? STOPS.map((s) => s.gate ?? "") : []));
  const [balloonOk, setBalloonOk] = useState<boolean | null>(null);
  const sceneRef = useRef(scene);
  const modeRef = useRef(mode);
  useEffect(() => { modeRef.current = mode; }, [mode]);

  const paint = useCallback((t: number) => {
    const el = stageRef.current;
    if (!el) return;
    applyFrame(el, frame(t, layout, ovRef.current));
    if (barRef.current) barRef.current.style.transform = `scaleX(${Math.min(1, t / DURATION)})`;
    const sc = sceneAt(t);
    if (sc !== sceneRef.current) { sceneRef.current = sc; setScene(sc); }
  }, [layout]);

  // fit the fixed-size stage to its container
  useEffect(() => {
    const wrap = wrapRef.current, el = stageRef.current;
    if (!wrap || !el) return;
    const ro = new ResizeObserver(() => {
      scaleRef.current = wrap.clientWidth / W;
      el.style.transformOrigin = "0 0";
      el.style.transform = `scale(${scaleRef.current})`;
    });
    ro.observe(wrap);
    paint(tRef.current);
    return () => ro.disconnect();
  }, [W, paint]);

  const stopLoop = () => { cancelAnimationFrame(rafRef.current); rafRef.current = 0; };
  const loop = useCallback(() => {
    stopLoop();
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000); // a long gap (background tab) never skips ahead
      last = now;
      let t = tRef.current + dt;
      const target = targetRef.current;
      if (target !== null && t >= target) {
        t = target; targetRef.current = null;
        tRef.current = t; paint(t); rafRef.current = 0;
        setMoving(false); setPlaying(false);
        return;
      }
      if (t >= DURATION) {
        tRef.current = DURATION; paint(DURATION); rafRef.current = 0;
        setPlaying(false); setFinished(true);
        return;
      }
      tRef.current = t; paint(t);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [paint]);
  useEffect(() => () => stopLoop(), []);

  // ---------------------------------------------------------------- Watch
  const play = useCallback(() => {
    if (tRef.current >= DURATION) { tRef.current = 0; ovRef.current = {}; setFinished(false); }
    autoPausedRef.current = false;
    targetRef.current = null;
    setPlaying(true);
    loop();
  }, [loop]);
  const pause = useCallback(() => { stopLoop(); setPlaying(false); }, []);
  const replay = useCallback(() => {
    stopLoop();
    tRef.current = 0; ovRef.current = {};
    setFinished(false); setBalloonOk(null); setGates(new Set()); setStop(0); setMoving(false);
    targetRef.current = null; autoPausedRef.current = false;
    paint(0);
    if (reduced) { modeRef.current = "explore"; setMode("explore"); return; }
    modeRef.current = "watch"; setMode("watch"); setPlaying(true); loop();
  }, [loop, paint, reduced]);

  // autoplay once the hero is on screen; pause when it scrolls away or the tab is hidden
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const sync = () => {
      const show = visibleRef.current && document.visibilityState === "visible";
      if (modeRef.current !== "watch" || tRef.current >= DURATION) return;
      if (!show && rafRef.current) { autoPausedRef.current = true; stopLoop(); setPlaying(false); }
      else if (show && autoPausedRef.current) { autoPausedRef.current = false; setPlaying(true); loop(); }
    };
    const io = new IntersectionObserver(([e]) => { visibleRef.current = !!e?.isIntersecting && e.intersectionRatio >= 0.35; sync(); }, { threshold: [0, 0.35, 0.6] });
    io.observe(wrap);
    document.addEventListener("visibilitychange", sync);
    if (!reduced) autoPausedRef.current = true; // "paused only because it isn't visible yet": the first sync starts it
    return () => { io.disconnect(); document.removeEventListener("visibilitychange", sync); };
  }, [loop, reduced]);

  // ---------------------------------------------------------------- Explore
  const goTo = useCallback((i: number, animate: boolean) => {
    const t = STOPS[i]!.t;
    setStop(i); setFinished(false);
    if (STOPS[i]!.gate === "balloon" && !ovRef.current.balloon0) { ovRef.current = { balloon0: { ...BALLOON[0].start } }; setBalloonOk(null); }
    if (!animate || reduced || t <= tRef.current) {
      stopLoop(); targetRef.current = null; setMoving(false); setPlaying(false);
      tRef.current = t; paint(t);
      return;
    }
    targetRef.current = t; setMoving(true); setPlaying(true); loop();
  }, [loop, paint, reduced]);

  const toExplore = useCallback(() => {
    if (modeRef.current === "explore") return;
    stopLoop(); setPlaying(false); autoPausedRef.current = false;
    const t = tRef.current;
    let i = 0;
    STOPS.forEach((s, k) => { if (s.t <= t + 0.01) i = k; });
    setGates(new Set(STOPS.slice(0, i).map((s) => s.gate ?? "")));
    if (t >= STOPS[6]!.t) setBalloonOk(true);
    modeRef.current = "explore"; setMode("explore");
    goTo(i, false);
  }, [goTo]);
  const toWatch = useCallback(() => {
    if (modeRef.current === "watch") return;
    targetRef.current = null; setMoving(false);
    modeRef.current = "watch"; setMode("watch");
    if (tRef.current >= DURATION - 0.1) { tRef.current = DURATION; paint(DURATION); setFinished(true); setPlaying(false); return; }
    ovRef.current = balloonOk ? ovRef.current : {};
    paint(tRef.current);
    if (!reduced) play();
  }, [balloonOk, paint, play, reduced]);

  const pass = useCallback((gate: string, next: number) => {
    setGates((g) => new Set(g).add(gate));
    goTo(next, true);
  }, [goTo]);

  const gate = STOPS[stop]!.gate;
  const canNext = mode === "explore" && stop < STOPS.length - 1 && (!gate || gates.has(gate));
  const next = () => {
    if (moving) { goTo(stop, false); return; } // finish the current move at once
    if (canNext) goTo(stop + 1, true);
  };
  const back = () => { if (stop > 0) goTo(stop - 1, false); };

  // balloon: drag or keys; the position is held in overrides and validated by balloonClear
  const moveBalloon = useCallback((fx: number, fy: number, commit: boolean) => {
    const b = BALLOON[0];
    fx = Math.min(1 - b.fw, Math.max(0, fx)); fy = Math.min(1 - b.fh, Math.max(0, fy));
    ovRef.current = { balloon0: { fx, fy } };
    paint(tRef.current);
    if (commit) {
      const ok = balloonClear(fx, fy, layout);
      setBalloonOk(ok);
      if (ok) setGates((g) => new Set(g).add("balloon"));
    }
  }, [layout, paint]);
  const onBalloonPointerDown = (e: RPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    const panel = frame(tRef.current, layout).sc0!;
    const start = { ...(ovRef.current.balloon0 ?? BALLOON[0].start) };
    const x0 = e.clientX, y0 = e.clientY, s = scaleRef.current;
    const pos = (ev: PointerEvent) => ({ fx: start.fx + (ev.clientX - x0) / s / panel.w!, fy: start.fy + (ev.clientY - y0) / s / panel.h! });
    const move = (ev: PointerEvent) => { const p = pos(ev); moveBalloon(p.fx, p.fy, false); };
    const up = (ev: PointerEvent) => {
      const p = pos(ev); moveBalloon(p.fx, p.fy, true);
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up);
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  };
  const onBalloonKey = (e: RKeyboardEvent<HTMLDivElement>) => {
    const d = { ArrowLeft: [-0.03, 0], ArrowRight: [0.03, 0], ArrowUp: [0, -0.03], ArrowDown: [0, 0.03] }[e.key];
    if (!d) return;
    e.preventDefault();
    const cur = ovRef.current.balloon0 ?? BALLOON[0].start;
    moveBalloon(cur.fx + d[0]!, cur.fy + d[1]!, true);
  };

  const explore = mode === "explore";
  const endActive = (mode === "watch" && finished) || (explore && stop === STOPS.length - 1 && !moving);
  const handlers = {
    interactive: explore && !moving, stop, balloonOk, endActive,
    onCreate: () => pass("create", 1),
    onApproveCast: () => pass("approveCast", 2),
    onEdit: () => pass("edit", 4),
    onApproveScript: () => pass("approveScript", 5),
    onBalloonPointerDown, onBalloonKey,
    onPlaceBalloon: () => moveBalloon(BALLOON[0].good.fx, BALLOON[0].good.fy, true),
    onReplay: replay,
  };
  const stageInert = !explore && !finished;

  return (
    <div className="w-full">
      <div ref={wrapRef} className="relative w-full overflow-hidden rounded-2xl" style={{ aspectRatio: `${W} / ${H}` }}>
        <div className="absolute left-0 top-0 origin-top-left" aria-hidden={stageInert || undefined} inert={stageInert || undefined}>
          <Stage ref={stageRef} layout={layout} h={handlers} />
        </div>
        <span className="absolute right-2 top-2 rounded-md bg-card/90 px-2 py-0.5 text-[11px] font-semibold text-muted shadow-raised">Sample artwork · demo</span>
      </div>

      {/* controls: outside the stage, always readable */}
      <div className="mt-3 space-y-3">
        <div className="h-1 overflow-hidden rounded-full bg-line" aria-hidden>
          <div ref={barRef} className="h-full origin-left bg-accent" style={{ transform: `scaleX(${t0 / DURATION})` }} />
        </div>
        <ol className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted" aria-label="Demo scenes">
          {SCENES.map((s, i) => (
            <li key={s.id} aria-current={i === scene ? "step" : undefined} className={i === scene ? "text-ink underline decoration-accent decoration-2 underline-offset-4" : ""}>{s.label}</li>
          ))}
        </ol>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="inline-flex rounded-lg border border-line bg-card p-0.5 text-[13px] font-semibold" role="group" aria-label="Demo mode">
            <button type="button" aria-pressed={!explore} onClick={toWatch} className={`rounded-md px-3 py-1.5 ${!explore ? "bg-ink text-paper" : "text-muted hover:text-ink"}`}>Watch</button>
            <button type="button" aria-pressed={explore} onClick={toExplore} className={`rounded-md px-3 py-1.5 ${explore ? "bg-ink text-paper" : "text-muted hover:text-ink"}`}>Try it step by step</button>
          </div>
          {!explore ? (
            <div className="flex items-center gap-2">
              {finished ? (
                <button type="button" onClick={replay} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-[13px] font-semibold"><RotateCcw className="h-4 w-4" aria-hidden />Replay</button>
              ) : (
                <button type="button" onClick={playing ? pause : play} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-[13px] font-semibold">
                  {playing ? <><Pause className="h-4 w-4" aria-hidden />Pause</> : <><Play className="h-4 w-4" aria-hidden />Play</>}
                </button>
              )}
              {!finished && <button type="button" onClick={replay} className="inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[13px] font-semibold text-muted hover:text-ink"><RotateCcw className="h-4 w-4" aria-hidden />Restart</button>}
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <button type="button" onClick={back} disabled={stop === 0} className="rounded-lg border border-line bg-card px-3 py-1.5 text-[13px] font-semibold disabled:opacity-50">Back</button>
              <span className="min-w-[4.5rem] text-center text-[13px] text-muted">Step {stop + 1} of {STOPS.length}</span>
              <button type="button" onClick={next} disabled={!moving && !canNext} className="rounded-lg border border-line bg-card px-3 py-1.5 text-[13px] font-semibold disabled:opacity-50">{moving ? "Skip ahead" : "Next"}</button>
            </div>
          )}
        </div>
        {explore && (
          <p className="text-[14px] text-ink" aria-live="polite">
            <span className="font-semibold">{SCENES[sceneOfStop(stop)]!.label}:</span> {STEP_TEXT[stop]}
            {gate && !gates.has(gate) && !moving && <span className="text-muted"> (Next unlocks after this step.)</span>}
          </p>
        )}
      </div>
    </div>
  );
}
