"use client";

/**
 * The demo stage: every element is rendered once and positioned from `frame(t)` by `applyFrame`, which
 * writes styles directly to the DOM. React re-renders only when mode or interaction state changes,
 * never per animation frame.
 */
import { forwardRef, type PointerEvent as RPointerEvent, type KeyboardEvent as RKeyboardEvent, type ReactNode } from "react";
import Link from "next/link";
import { DEMO_ASSETS } from "./manifest";
import { IDEA_SHORT, INNER, LINES, STAGE, type El, type Frame, type Layout } from "./timeline";

const RECT_KEYS = new Set(["ws", "cast0", "cast1", "bar", "bible", "sc0", "sc1", "sc2", "label", "exp", "end", "bal0", "bal1", "bal2", "castRing0", "castRing1", "ring0", "ring1", "ring2", "ringShell"]);

/** Write one frame to the DOM. Elements are found by `data-k`. */
export function applyFrame(root: HTMLElement, f: Frame) {
  const prompt = root.querySelector<HTMLElement>('[data-k="prompt"]');
  if (prompt) prompt.style.borderRightColor = (f.caret?.o ?? 0) > 0 ? "var(--color-accent)" : "transparent";
  for (const el of root.querySelectorAll<HTMLElement | SVGElement>("[data-k]")) {
    const k = el.getAttribute("data-k")!;
    const st: El | undefined = f[k];
    if (!st) continue;
    const s = (el as HTMLElement).style;
    if (st.o !== undefined) {
      s.opacity = String(Math.round(st.o * 1000) / 1000);
      s.visibility = st.o < 0.005 ? "hidden" : ""; // never force "visible": it would override a hidden parent
    }
    if (st.d !== undefined) el.setAttribute("d", st.d);
    if (st.text !== undefined && el.textContent !== st.text) el.textContent = st.text;
    if (st.reveal !== undefined) {
      const r = st.reveal * 130 - 15; // soft left-to-right mask
      const m = st.reveal >= 1 ? "none" : `linear-gradient(90deg, #000 ${r}%, transparent ${r + 15}%)`;
      s.setProperty("mask-image", m);
      s.setProperty("-webkit-mask-image", m);
    }
    if (k.startsWith("castImg")) {
      // inset by transform (not left/top) so the move is not a layout shift
      if (st.x !== undefined) { s.transform = `translate(${st.x}px, ${st.x}px)`; s.width = `calc(100% - ${2 * st.x}px)`; }
      if (st.h !== undefined) s.height = `${st.h}px`;
      continue;
    }
    if (RECT_KEYS.has(k) || k === "saved2") {
      if (st.w !== undefined) s.width = `${st.w}px`;
      if (st.h !== undefined) s.height = `${st.h}px`;
      if (st.x !== undefined || st.y !== undefined || st.s !== undefined) s.transform = `translate(${st.x ?? 0}px, ${st.y ?? 0}px) scale(${st.s ?? 1})`;
    } else if (st.s !== undefined || st.y !== undefined) {
      s.transform = `translateY(${st.y ?? 0}px) scale(${st.s ?? 1})`;
    }
  }
}

type Handlers = {
  interactive: boolean;
  stop: number;
  onCreate: () => void;
  onApproveCast: () => void;
  onEdit: () => void;
  onApproveScript: () => void;
  onBalloonPointerDown: (e: RPointerEvent<HTMLDivElement>) => void;
  onBalloonKey: (e: RKeyboardEvent<HTMLDivElement>) => void;
  onPlaceBalloon: () => void;
  onReplay: () => void;
  balloonOk: boolean | null;
  /** The end card's link and replay button can be used (finished in Watch, or the last Explore step). */
  endActive: boolean;
};

const Btn = ({ k, active, onClick, children, className = "" }: { k?: string; active: boolean; onClick: () => void; children: ReactNode; className?: string }) => (
  <button type="button" data-k={k} tabIndex={active ? 0 : -1} aria-disabled={!active || undefined} onClick={active ? onClick : undefined}
    className={`inline-flex items-center justify-center whitespace-nowrap rounded-lg bg-accent px-4 py-2 text-[14px] font-semibold text-white shadow-raised ${active ? "cursor-pointer ring-2 ring-accent/30 ring-offset-2" : "cursor-default"} ${className}`}>
    {children}
  </button>
);
const Check = () => <svg viewBox="0 0 16 16" className="h-4 w-4" aria-hidden><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" /></svg>;
const abs = "absolute left-0 top-0";

export const Stage = forwardRef<HTMLDivElement, { layout: Layout; h: Handlers; capture?: boolean }>(function Stage({ layout, h, capture }, ref) {
  const { w: W, h: H } = STAGE[layout];
  const P = layout === "portrait";
  const inner = INNER(layout);
  const at = (i: number) => h.interactive && h.stop === i;
  return (
    <div ref={ref} className="relative overflow-hidden rounded-2xl border border-line bg-paper text-ink" style={{ width: W, height: H }} data-capture={capture || undefined}>
      {/* 1. workspace → story card */}
      <div data-k="ws" className={`${abs} overflow-hidden rounded-xl border border-line bg-card shadow-raised`}>
        <div data-k="wsFull" className="absolute left-0 top-0 flex flex-col gap-3 p-5" style={{ width: inner.ws.w - 2, height: inner.ws.h - 2 }}>
          <p className="text-[12px] font-semibold uppercase tracking-wide text-accent">New comic</p>
          <p className="font-display text-[20px] font-semibold leading-tight">What&apos;s the story?</p>
          <div className={`rounded-lg border border-field bg-card px-3 py-2 text-[15px] leading-snug ${P ? "min-h-[120px]" : "min-h-[84px]"}`}>
            {/* the caret is the text's right border, so typing never moves another element (no layout shift) */}
            <span data-k="prompt" className="border-r-2 border-transparent pr-px" />
          </div>
          <div data-k="chips" className="flex flex-wrap gap-2 text-[13px]">
            <span className="rounded-full border border-line bg-paper px-3 py-1">3-panel strip</span>
            <span className="rounded-full border border-line bg-paper px-3 py-1">Warm illustrated style</span>
          </div>
          <div className="mt-auto"><Btn k="create" active={at(0)} onClick={h.onCreate}>Create my cast</Btn></div>
        </div>
        <div data-k="wsLine" className="absolute left-0 top-0 flex items-center gap-3 px-4" style={{ width: inner.story.w - 2, height: inner.story.h - 2 }}>
          <span className="rounded-md bg-accent-soft px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-accent">Story</span>
          <span className={`${P ? "text-[13px] leading-snug" : "truncate text-[14px]"} font-medium`}>{IDEA_SHORT}</span>
        </div>
      </div>

      {/* 2. cast cards → portraits in the script margin */}
      {(["keeper", "gull"] as const).map((who, i) => (
        <div key={who} data-k={`cast${i}`} className={`${abs} overflow-hidden rounded-xl border border-line bg-card shadow-raised`}>
          {/* eslint-disable-next-line @next/next/no-img-element -- static fixture, explicit size */}
          <img data-k={`castImg${i}`} src={DEMO_ASSETS[who].src} width={400} height={480} alt="" decoding="async" className="absolute left-0 top-0 rounded-lg object-cover" style={{ objectPosition: "50% 30%" }} />
          <div data-k={`castInfo${i}`} className={`absolute left-0 space-y-1.5 ${P ? "px-3" : "px-4"}`} style={{ top: inner.castImgH + 24, width: inner.card.w - 2 }}>
            <p className="font-display text-[16px] font-semibold">{who === "keeper" ? "Ida, the keeper" : "The gull"}</p>
            <div className="flex flex-wrap gap-1.5 text-[12px]">
              {who === "keeper" ? (
                <>
                  <span className="relative rounded-full border border-line px-2 py-0.5"><span data-k="tagCoat" className="absolute inset-0 rounded-full bg-accent-soft ring-1 ring-accent" />
                    <span className="relative">Yellow raincoat</span></span>
                  <span className="relative rounded-full border border-line px-2 py-0.5"><span data-k="tagGlasses" className="absolute inset-0 rounded-full bg-accent-soft ring-1 ring-accent" />
                    <span className="relative">Round glasses</span></span>
                  {!P && <span className="rounded-full border border-line px-2 py-0.5">Navy cap</span>}
                </>
              ) : (
                <>
                  <span className="rounded-full border border-line px-2 py-0.5">Grey wings</span>
                  <span className="rounded-full border border-line px-2 py-0.5">Orange beak</span>
                </>
              )}
            </div>
          </div>
        </div>
      ))}
      <div data-k="castRing0" className={`${abs} rounded-full border-[3px] border-accent`} aria-hidden />
      <div data-k="castRing1" className={`${abs} rounded-full border-[3px] border-accent`} aria-hidden />

      {/* 3. Story Bible */}
      <div data-k="bible" className={`${abs} overflow-hidden rounded-xl border border-line bg-card p-5 shadow-raised`}>
        <p className="font-display text-[18px] font-semibold">Story Bible</p>
        <dl className="mt-3 space-y-1 text-[14px]">
          {[["Setting", "A lighthouse at dusk"], ["Tone", "Warm and gently funny"], ["Continuity", "Same keeper and gull in every panel"], ["Reveal", "The shell appears only at the end"]].map(([k, v], i) => (
            <div key={k} className={`relative grid rounded-md px-2 py-1.5 ${P ? "grid-cols-1" : "grid-cols-[110px_1fr]"}`}>
              {i >= 2 && <span data-k={`factHi${i}`} className="absolute inset-0 rounded-md bg-accent-soft ring-1 ring-accent/40" />}
              <dt className="relative font-semibold text-muted">{k}</dt>
              <dd className="relative">{v}</dd>
            </div>
          ))}
        </dl>
      </div>

      {/* 4–7. script cards that become the comic panels */}
      {[0, 1, 2].map((i) => {
        const art = [DEMO_ASSETS.panel1, DEMO_ASSETS.panel2, DEMO_ASSETS.panel3][i]!;
        const head = [["1", "Wide", "A cold visitor."], ["2", "Medium", "A little kindness."], ["3", "Close-up", "An unexpected thank-you."]][i]!;
        return (
          <div key={i} data-k={`sc${i}`} className={`${abs} overflow-hidden rounded-[10px] border-2 border-ink bg-card`}>
            {/* eslint-disable-next-line @next/next/no-img-element -- static fixture, explicit size */}
            <img data-k={`art${i}`} src={art.src} width={art.width} height={art.height} alt="" decoding="async" loading={capture ? "eager" : "lazy"} className="absolute inset-0 h-full w-full object-cover" />
            <div data-k={`scc${i}`} className="absolute left-0 top-0 flex flex-col justify-center gap-1 bg-card px-4" style={{ width: inner.sc.w - 4, height: inner.sc.h - 4 }}>
              <p className="text-[12px] font-semibold uppercase tracking-wide text-accent">Panel {head[0]} · {head[1]}</p>
              <p className="font-display text-[16px] font-semibold leading-tight">{head[2]}</p>
              <div className="relative self-start">
                {i === 1 && <span data-k="edit1" className="absolute -inset-1 rounded-md ring-2 ring-accent" />}
                <p className="relative text-[14px] text-muted">{i === 1 ? <span data-k="line1" /> : `“${LINES[i]}”`}</p>
              </div>
              {i === 1 && (
                <div className="flex items-center gap-2">
                  <span data-k="saved1" className="inline-flex items-center gap-1 text-[12px] font-semibold text-ok"><Check />Saved as version 2</span>
                  <button type="button" tabIndex={at(3) ? 0 : -1} onClick={at(3) ? h.onEdit : undefined} className={`text-[12px] font-semibold text-accent underline underline-offset-2 ${at(3) ? "" : "hidden"}`}>Use the edited line</button>
                </div>
              )}
            </div>
          </div>
        );
      })}

      {/* lettering */}
      <svg className={`${abs} pointer-events-none`} width={W} height={H} aria-hidden>
        {[0, 1, 2].map((i) => <path key={i} data-k={`tail${i}`} fill="#fff" stroke="#18202b" strokeWidth="2" strokeLinejoin="round" />)}
      </svg>
      {[0, 1, 2].map((i) => {
        const drag = i === 0 && at(6);
        return (
          <div key={i} data-k={`bal${i}`} role={drag ? "button" : undefined} tabIndex={drag ? 0 : -1}
            aria-label={drag ? `Speech balloon: “${LINES[0]}”. Drag it, or use the arrow keys, to move it off the keeper's face.` : undefined}
            onPointerDown={drag ? h.onBalloonPointerDown : undefined} onKeyDown={drag ? h.onBalloonKey : undefined}
            className={`${abs} flex items-center justify-center rounded-[50%] border-2 border-ink bg-white px-3 text-center font-comic leading-tight text-ink text-[13px] ${drag ? "cursor-grab touch-none ring-2 ring-accent ring-offset-2" : ""}`}>
            {LINES[i]}
          </div>
        );
      })}
      <div data-k="saved2" className={`${abs} inline-flex items-center gap-1 rounded-full bg-card px-2 py-0.5 text-[12px] font-semibold text-ok shadow-raised`}><Check />Saved</div>

      {/* review rings */}
      {["ring0", "ring1", "ring2", "ringShell"].map((k) => <div key={k} data-k={k} className={`${abs} rounded-full border-[3px] border-accent bg-accent/5`} aria-hidden />)}

      {/* status label under the comic */}
      <div data-k="label" className={`${abs} flex items-center`}>
        <div className="relative h-8 w-full text-[14px] font-medium">
          <span data-k="labelDraw" className="absolute inset-0 flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-accent" aria-hidden />Illustrating your story, panel by panel</span>
          <span data-k="labelReview" className="absolute inset-0 flex items-center gap-2"><span className="rounded-md bg-accent-soft px-2 py-0.5 text-accent">Review</span>Character consistency: keeper&apos;s glasses</span>
          <span data-k="labelShell" className="absolute inset-0 flex items-center gap-2"><span className="rounded-md bg-accent-soft px-2 py-0.5 text-accent">Review</span>The shell appears only in panel 3</span>
          <span data-k="labelEdit" className="absolute inset-0 flex items-center gap-2"><span className="rounded-md bg-accent-soft px-2 py-0.5 text-accent">Edit</span>{h.interactive && h.stop === 6
            ? (h.balloonOk === false ? "Move it clear of the keeper's face" : h.balloonOk ? "Clear of the face. Saved." : "Drag the first balloon off the keeper's face")
            : "Drag a balloon; its tail follows the speaker"}
            {at(6) && !h.balloonOk && <button type="button" onClick={h.onPlaceBalloon} className="ml-1 text-[13px] font-semibold text-accent underline underline-offset-2">Place it for me</button>}
          </span>
        </div>
      </div>

      {/* approval bar */}
      <div data-k="bar" className={`${abs} overflow-hidden rounded-xl border border-line bg-card shadow-float`}>
        <div data-k="barCast" className="absolute inset-0">
          <div data-k="castIdle" className="absolute inset-0 flex items-center justify-between gap-3 px-4">
            <span className="text-[13px] text-muted">{P ? "Approve before drawing." : "Nothing is drawn until you approve."}</span>
            <Btn k="approveCast" active={at(1)} onClick={h.onApproveCast}>Approve cast</Btn>
          </div>
          <div data-k="castDone" className="absolute inset-0 flex items-center gap-2 px-4 text-[14px] font-semibold text-ok"><Check />Cast approved</div>
        </div>
        <div data-k="barScript" className="absolute inset-0">
          <div data-k="scriptIdle" className="absolute inset-0 flex items-center justify-between gap-3 px-4">
            <span className="text-[13px] text-muted">{P ? "Version 2" : "Script version 2 · 3 panels"}</span>
            <Btn k="approveScript" active={at(4)} onClick={h.onApproveScript}>Approve and draw</Btn>
          </div>
          <div data-k="scriptDone" className="absolute inset-0 flex items-center gap-2 px-4 text-[14px] font-semibold text-ok"><Check />Script approved</div>
        </div>
      </div>

      {/* 7. export */}
      <div data-k="exp" className={`${abs} flex items-center justify-between gap-3 rounded-xl border border-line bg-card px-4 shadow-raised`}>
        <span className="text-[13px] font-semibold">Export</span>
        <span className="flex gap-2 text-[13px] font-semibold">{["PDF", "PNG", "CBZ"].map((x) => <span key={x} className="rounded-md border border-line bg-paper px-2.5 py-1">{x}</span>)}</span>
      </div>

      {/* 8. end card */}
      <div data-k="end" className={`${abs} flex ${P ? "flex-col justify-center gap-3" : "items-center justify-between gap-4"} rounded-xl bg-ink px-5 text-paper`}>
        <p className="font-display text-[19px] font-semibold leading-tight">Your idea. Your characters. Your comic.</p>
        {!capture && (
          <div className="flex shrink-0 items-center gap-2">
            <Link href="/new" tabIndex={h.endActive ? 0 : -1} className="rounded-lg bg-paper px-4 py-2 text-[14px] font-semibold text-ink">Create your comic</Link>
            <button type="button" tabIndex={h.endActive ? 0 : -1} onClick={h.onReplay} className="rounded-lg px-3 py-2 text-[14px] font-semibold text-paper/90 underline underline-offset-2">Replay demo</button>
          </div>
        )}
      </div>
    </div>
  );
});
