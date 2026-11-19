/**
 * Hero demo timeline. Everything the demo shows is a pure function of playback time:
 * `frame(t, layout, overrides)` returns the state of every element, so watch mode, explore mode and video
 * capture render identical pictures for the same inputs. No timers, no randomness, no network.
 */
import anchors from "./anchors.json";

export type Layout = "landscape" | "portrait";
export type Rect = { x: number; y: number; w: number; h: number };
export type El = { x?: number; y?: number; w?: number; h?: number; o?: number; s?: number; text?: string; reveal?: number; d?: string };
export type Frame = Record<string, El>;
export type Overrides = { balloon0?: { fx: number; fy: number } | null };

export const DURATION = 36;
export const STAGE: Record<Layout, { w: number; h: number }> = { landscape: { w: 640, h: 560 }, portrait: { w: 360, h: 640 } };

export const IDEA = "A lighthouse keeper spots a shivering gull in the rain, builds it a tiny shelter, and finds a shiny shell left as thanks.";
export const IDEA_SHORT = "A lighthouse keeper, a shivering gull, and a thank-you";
export const OLD_LINE = "Here you go.";
export const NEW_LINE = "A room with a sea view.";
export const LINES = ["You look cold, little friend.", NEW_LINE, "A thank-you? For me?"];

export const SCENES = [
  { id: "idea", label: "Idea", start: 0, end: 4 },
  { id: "cast", label: "Cast", start: 4, end: 8 },
  { id: "bible", label: "Story Bible", start: 8, end: 12 },
  { id: "script", label: "Script", start: 12, end: 16 },
  { id: "draw", label: "Panels", start: 16, end: 22 },
  { id: "review", label: "Review & edit", start: 22, end: 26 },
  { id: "read", label: "Read & export", start: 26, end: 32 },
  { id: "end", label: "Your comic", start: 32, end: 36 },
] as const;
export type SceneId = (typeof SCENES)[number]["id"];
export const sceneAt = (t: number) => SCENES.findIndex((s) => t < s.end) === -1 ? SCENES.length - 1 : SCENES.findIndex((s) => t < s.end);

/** Explore mode stops. `gate` names the action that moves on from a stop; Next is disabled until it happens. */
export const STOPS: { scene: SceneId; t: number; gate?: "create" | "approveCast" | "edit" | "approveScript" | "balloon" }[] = [
  { scene: "idea", t: 3.0, gate: "create" },
  { scene: "cast", t: 6.9, gate: "approveCast" },
  { scene: "bible", t: 10.6 },
  { scene: "script", t: 12.8, gate: "edit" },
  { scene: "script", t: 15.0, gate: "approveScript" },
  { scene: "draw", t: 21.6 },
  { scene: "review", t: 24.6, gate: "balloon" },
  { scene: "read", t: 30.0 },
  { scene: "end", t: 35.95 },
];

// ---------------------------------------------------------------- easing and keyframes
const clamp = (v: number, a = 0, b = 1) => Math.min(b, Math.max(a, v));
export const ease = (p: number) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
export const seg = (t: number, a: number, b: number) => ease(clamp((t - a) / (b - a)));
type Key = [number, El];
function track(t: number, keys: Key[]): El {
  // carry properties forward so every keyframe is complete
  const full: Key[] = [];
  let acc: El = {};
  for (const [kt, st] of keys) { acc = { ...acc, ...st }; full.push([kt, acc]); }
  if (t <= full[0]![0]) return full[0]![1];
  for (let i = 1; i < full.length; i++) {
    const [t1, b] = full[i]!; const [t0, a] = full[i - 1]!;
    if (t <= t1) {
      const p = ease(clamp((t - t0) / (t1 - t0)));
      const out: El = {};
      for (const k of Object.keys(b) as (keyof El)[]) {
        const av = a[k], bv = b[k];
        if (typeof av === "number" && typeof bv === "number") (out as Record<string, number>)[k] = av + (bv - av) * p;
        else (out as Record<string, unknown>)[k] = p < 0.5 && av !== undefined ? av : bv;
      }
      return out;
    }
  }
  return full[full.length - 1]![1];
}
const R = (r: Rect, more: El = {}): El => ({ x: r.x, y: r.y, w: r.w, h: r.h, ...more });

// ---------------------------------------------------------------- layout rects
type Lay = Record<string, Rect>;
const LAY: Record<Layout, Lay> = {
  landscape: {
    ws: { x: 40, y: 118, w: 560, h: 268 }, story: { x: 40, y: 24, w: 560, h: 56 },
    cast0: { x: 40, y: 104, w: 272, h: 336 }, cast1: { x: 328, y: 104, w: 272, h: 336 },
    av0: { x: 40, y: 104, w: 56, h: 56 }, av1: { x: 40, y: 172, w: 56, h: 56 },
    bible: { x: 112, y: 104, w: 488, h: 304 }, bar: { x: 40, y: 462, w: 560, h: 64 },
    sc0: { x: 112, y: 100, w: 488, h: 104 }, sc1: { x: 112, y: 212, w: 488, h: 104 }, sc2: { x: 112, y: 324, w: 488, h: 104 },
    p0: { x: 40, y: 36, w: 275, h: 190 }, p1: { x: 325, y: 36, w: 275, h: 190 }, p2: { x: 40, y: 236, w: 560, h: 230 },
    r0: { x: 32, y: 22, w: 283, h: 196 }, r1: { x: 325, y: 22, w: 283, h: 196 }, r2: { x: 32, y: 228, w: 576, h: 236 },
    e0: { x: 32, y: 22, w: 283, h: 196 }, e1: { x: 325, y: 22, w: 283, h: 196 }, e2: { x: 32, y: 228, w: 576, h: 236 },
    label: { x: 40, y: 486, w: 560, h: 40 }, exp: { x: 40, y: 486, w: 560, h: 52 }, end: { x: 32, y: 478, w: 576, h: 70 },
  },
  portrait: {
    ws: { x: 16, y: 120, w: 328, h: 340 }, story: { x: 16, y: 16, w: 328, h: 64 },
    cast0: { x: 16, y: 96, w: 160, h: 300 }, cast1: { x: 184, y: 96, w: 160, h: 300 },
    av0: { x: 16, y: 96, w: 44, h: 44 }, av1: { x: 16, y: 148, w: 44, h: 44 },
    bible: { x: 70, y: 96, w: 274, h: 360 }, bar: { x: 16, y: 560, w: 328, h: 64 },
    sc0: { x: 70, y: 92, w: 274, h: 146 }, sc1: { x: 70, y: 246, w: 274, h: 146 }, sc2: { x: 70, y: 400, w: 274, h: 146 },
    p0: { x: 16, y: 20, w: 328, h: 196 }, p1: { x: 16, y: 224, w: 328, h: 196 }, p2: { x: 16, y: 428, w: 328, h: 140 },
    r0: { x: 16, y: 20, w: 328, h: 196 }, r1: { x: 16, y: 224, w: 328, h: 196 }, r2: { x: 16, y: 428, w: 328, h: 140 },
    e0: { x: 16, y: 12, w: 328, h: 180 }, e1: { x: 16, y: 198, w: 328, h: 180 }, e2: { x: 16, y: 384, w: 328, h: 132 },
    label: { x: 16, y: 580, w: 328, h: 40 }, exp: { x: 16, y: 580, w: 328, h: 52 }, end: { x: 16, y: 526, w: 328, h: 104 },
  },
};
/** Fixed sizes for content inside cards whose box animates: anchoring that content top-left at a constant size
 *  keeps it still while the card resizes around it (no reflow, no layout shift). */
export const INNER = (layout: Layout) => {
  const L = LAY[layout];
  return { ws: L.ws!, story: L.story!, card: L.cast0!, sc: L.sc0!, castImgH: layout === "portrait" ? 150 : 210 };
};
export const ART = [{ w: 580, h: 400 }, { w: 580, h: 400 }, { w: 1120, h: 460 }] as const;
const CAST_ART = { w: 400, h: 480 };
type Pt = { x: number; y: number };

/** Where a point of the artwork lands in a frame that shows it with object-fit: cover. */
export function coverPoint(fx: number, fy: number, frame: Rect, art: { w: number; h: number }): Pt {
  const s = Math.max(frame.w / art.w, frame.h / art.h);
  const dw = art.w * s, dh = art.h * s;
  return { x: frame.x + (frame.w - dw) / 2 + fx * dw, y: frame.y + (frame.h - dh) / 2 + fy * dh };
}

// Balloons: positions as fractions of the panel (x, y of the top-left corner; width and height).
export const BALLOON = [
  { start: { fx: 0.16, fy: 0.22 }, good: { fx: 0.04, fy: 0.05 }, fw: 0.44, fh: 0.25 },
  { start: { fx: 0.4, fy: 0.05 }, good: { fx: 0.4, fy: 0.05 }, fw: 0.42, fh: 0.25 },
  { start: { fx: 0.26, fy: 0.06 }, good: { fx: 0.26, fy: 0.06 }, fw: 0.3, fh: 0.24 },
] as const;
/** The wide third panel is short in the portrait layout, so its balloon takes a larger share of it. */
export const balloonSpec = (i: number, layout: Layout) =>
  layout === "portrait" && i === 2 ? { start: { fx: 0.18, fy: 0.06 }, good: { fx: 0.18, fy: 0.06 }, fw: 0.44, fh: 0.36 } : BALLOON[i]!;
const HEAD = [anchors.panel1.keeperHead, anchors.panel2.keeperHead, anchors.panel3.keeperHead];
const GLASSES = [anchors.panel1.glasses, anchors.panel2.glasses, anchors.panel3.glasses];

/** True when balloon 0 at (fx, fy) stays inside its panel and off the keeper's face. */
export function balloonClear(fx: number, fy: number, layout: Layout): boolean {
  const L = LAY[layout]; const p = L.r0!;
  const b = BALLOON[0];
  if (fx < 0 || fy < 0 || fx + b.fw > 1 || fy + b.fh > 1) return false;
  const head = coverPoint(HEAD[0]!.x, HEAD[0]!.y, p, ART[0]);
  const r = 0.11 * p.h;
  const bx = p.x + fx * p.w, by = p.y + fy * p.h, bw = b.fw * p.w, bh = b.fh * p.h;
  const nx = clamp(head.x, bx, bx + bw), ny = clamp(head.y, by, by + bh);
  return Math.hypot(head.x - nx, head.y - ny) > r;
}

/** Tail from the balloon toward the speaker, stopping short of the head. */
function tailPath(b: Rect, head: Pt, headR: number): string {
  const bx = b.x + b.w / 2, by = b.y + b.h / 2;
  const dx = head.x - bx, dy = head.y - by, d = Math.hypot(dx, dy) || 1;
  const tip = { x: head.x - (dx / d) * headR * 1.25, y: head.y - (dy / d) * headR * 1.25 };
  const nx = -dy / d, ny = dx / d, half = Math.min(b.w, b.h) * 0.16;
  const base = { x: bx + (dx / d) * b.h * 0.25, y: by + (dy / d) * b.h * 0.25 };
  return `M ${(base.x + nx * half).toFixed(1)} ${(base.y + ny * half).toFixed(1)} L ${tip.x.toFixed(1)} ${tip.y.toFixed(1)} L ${(base.x - nx * half).toFixed(1)} ${(base.y - ny * half).toFixed(1)} Z`;
}

function typed(full: string, t: number, a: number, b: number) {
  const n = Math.round(clamp((t - a) / (b - a)) * full.length);
  return full.slice(0, n);
}

// ---------------------------------------------------------------- the frame
export function frame(t: number, layout: Layout, ov: Overrides = {}): Frame {
  const L = LAY[layout];
  const f: Frame = {};
  const portrait = layout === "portrait";

  // Scene 1: the workspace, then it becomes the story card
  f.ws = track(t, [[0, R(L.ws!, { o: 0, y: L.ws!.y + 10 })], [0.6, R(L.ws!, { o: 1 })], [3.8, R(L.ws!, { o: 1 })], [4.6, R(L.story!, { o: 1 })], [15.9, R(L.story!, { o: 1 })], [16.5, R(L.story!, { o: 0 })]]);
  f.wsFull = track(t, [[3.7, { o: 1 }], [4.0, { o: 0 }]]);
  f.wsLine = track(t, [[4.1, { o: 0 }], [4.6, { o: 1 }]]);
  f.prompt = { text: typed(IDEA, t, 0.6, 2.8) };
  f.caret = { o: t > 0.5 && t < 3.0 ? 1 : 0 };
  f.chips = track(t, [[2.8, { o: 0, y: 6 }], [3.15, { o: 1, y: 0 }]]);
  f.create = track(t, [[3.35, { s: 1 }], [3.45, { s: 0.95 }], [3.6, { s: 1 }]]);

  // Scene 2: cast cards rise out of the story card; identity details; approve
  for (const i of [0, 1]) {
    const c = L[`cast${i}`]!, av = L[`av${i}`]!;
    f[`cast${i}`] = track(t, [[4.3 + i * 0.25, R(L.story!, { o: 0 })], [5.1 + i * 0.25, R(c, { o: 1 })], [8.0, R(c, { o: 1 })], [8.8, R(av, { o: 1 })], [15.9, R(av, { o: 1 })], [16.4, R(av, { o: 0 })]]);
    f[`castInfo${i}`] = track(t, [[7.9, { o: 1 }], [8.2, { o: 0 }]]);
    // the portrait inside the card: padding (x) and height (h); it fills the avatar once the card shrinks
    f[`castImg${i}`] = track(t, [[8.0, { x: 10, h: portrait ? 150 : 210 }], [8.8, { x: 0, h: av.h }]]);
  }
  const castImg = (i: number): Rect => { const c = L[`cast${i}`]!; return { x: c.x + 10, y: c.y + 10, w: c.w - 20, h: portrait ? 150 : 210 }; };
  const coat = coverPoint(anchors.castKeeper.coat.x, anchors.castKeeper.coat.y, castImg(0), CAST_ART);
  const glasses = coverPoint(anchors.castKeeper.glasses.x, anchors.castKeeper.glasses.y, castImg(0), CAST_ART);
  const rs = portrait ? 34 : 44;
  f.castRing0 = { x: coat.x - rs / 2, y: coat.y - rs / 2, w: rs, h: rs, ...track(t, [[5.5, { o: 0, s: 0.6 }], [5.9, { o: 1, s: 1 }], [6.6, { o: 1, s: 1 }], [7.0, { o: 0, s: 1 }]]) };
  f.castRing1 = { x: glasses.x - rs / 2, y: glasses.y - rs / 2, w: rs, h: rs, ...track(t, [[6.0, { o: 0, s: 0.6 }], [6.4, { o: 1, s: 1 }], [7.0, { o: 1, s: 1 }], [7.4, { o: 0, s: 1 }]]) };
  f.tagCoat = track(t, [[5.5, { o: 0 }], [5.8, { o: 1 }], [6.7, { o: 1 }], [7.0, { o: 0 }]]);
  f.tagGlasses = track(t, [[6.0, { o: 0 }], [6.3, { o: 1 }], [7.1, { o: 1 }], [7.4, { o: 0 }]]);

  // bottom action bar: approve cast, then approve and draw
  f.bar = track(t, [[5.0, R(L.bar!, { o: 0, y: L.bar!.y + 8 })], [5.4, R(L.bar!, { o: 1 })], [8.0, R(L.bar!, { o: 1 })], [8.4, R(L.bar!, { o: 0 })], [14.3, R(L.bar!, { o: 0, y: L.bar!.y + 8 })], [14.7, R(L.bar!, { o: 1 })], [16.0, R(L.bar!, { o: 1 })], [16.4, R(L.bar!, { o: 0 })]]);
  f.barCast = { o: t < 11 ? 1 : 0 };
  f.barScript = { o: t >= 11 ? 1 : 0 };
  f.approveCast = track(t, [[6.95, { s: 1 }], [7.05, { s: 0.95 }], [7.2, { s: 1 }]]);
  f.castIdle = track(t, [[7.15, { o: 1 }], [7.3, { o: 0 }]]);
  f.castDone = track(t, [[7.2, { o: 0 }], [7.45, { o: 1 }]]);
  f.approveScript = track(t, [[15.25, { s: 1 }], [15.35, { s: 0.95 }], [15.5, { s: 1 }]]);
  f.scriptIdle = track(t, [[15.45, { o: 1 }], [15.6, { o: 0 }]]);
  f.scriptDone = track(t, [[15.5, { o: 0 }], [15.75, { o: 1 }]]);

  // Scene 3: Story Bible, then its facts become the script cards
  f.bible = track(t, [[8.5, R(L.bible!, { o: 0, y: L.bible!.y + 10 })], [9.1, R(L.bible!, { o: 1 })], [11.0, R(L.bible!, { o: 1 })], [11.4, R(L.bible!, { o: 0 })]]);
  f.factHi2 = track(t, [[9.4, { o: 0 }], [9.8, { o: 1 }], [10.6, { o: 1 }], [11.0, { o: 0 }]]);
  f.factHi3 = track(t, [[10.0, { o: 0 }], [10.4, { o: 1 }], [10.9, { o: 1 }], [11.2, { o: 0 }]]);

  // Scenes 4-7: the same three elements are script cards, then comic panels
  for (const i of [0, 1, 2]) {
    const sc = L[`sc${i}`]!, p = L[`p${i}`]!, r = L[`r${i}`]!, e = L[`e${i}`]!;
    const b = L.bible!;
    const fromBible: Rect = { x: b.x, y: b.y + 40 + i * 70, w: b.w, h: 60 };
    f[`sc${i}`] = track(t, [[11.35 + i * 0.15, R(fromBible, { o: 0 })], [12.1 + i * 0.15, R(sc, { o: 1 })], [16.2, R(sc, { o: 1 })], [17.2, R(p, { o: 1 })], [26.0, R(p, { o: 1 })], [26.8, R(r, { o: 1 })], [32.0, R(r, { o: 1 })], [32.8, R(e, { o: 1 })]]);
    f[`scc${i}`] = track(t, [[16.2, { o: 1 }], [16.6, { o: 0 }]]);
    const a = 17.2 + i * 1.4;
    f[`art${i}`] = { reveal: seg(t, a, a + 1.2), o: t >= a ? 1 : 0 };
  }
  // the edited line in script card 2
  let line = OLD_LINE;
  if (t >= 12.9 && t < 13.5) line = OLD_LINE.slice(0, Math.round((1 - clamp((t - 12.9) / 0.5)) * OLD_LINE.length));
  else if (t >= 13.5) line = typed(NEW_LINE, t, 13.5, 14.5);
  f.line1 = { text: `“${line}”` };
  f.edit1 = track(t, [[12.6, { o: 0 }], [12.85, { o: 1 }], [14.7, { o: 1 }], [15.1, { o: 0 }]]);
  f.saved1 = track(t, [[14.55, { o: 0 }], [14.8, { o: 1 }], [15.9, { o: 1 }], [16.2, { o: 0 }]]);

  // Scene 5 label
  f.label = track(t, [[16.6, R(L.label!, { o: 0 })], [17.0, R(L.label!, { o: 1 })], [21.4, R(L.label!, { o: 1 })], [21.8, R(L.label!, { o: 0 })], [22.0, R(L.label!, { o: 0 })], [22.3, R(L.label!, { o: 1 })], [25.9, R(L.label!, { o: 1 })], [26.2, R(L.label!, { o: 0 })]]);
  f.labelDraw = { o: t < 21.9 ? 1 : 0 };
  f.labelReview = { o: t >= 21.9 && t < 23.7 ? 1 : 0 };
  f.labelShell = { o: t >= 23.7 && t < 24.6 ? 1 : 0 };
  f.labelEdit = { o: t >= 24.6 ? 1 : 0 };

  // Scene 6: review rings on the glasses, the shell timing, then the balloon edit
  const panelRect = (i: number): Rect => { const el = f[`sc${i}`]!; return { x: el.x!, y: el.y!, w: el.w!, h: el.h! }; };
  for (const i of [0, 1, 2]) {
    const pt = coverPoint(GLASSES[i]!.x, GLASSES[i]!.y, panelRect(i), ART[i]!);
    const d = i === 2 ? (portrait ? 46 : 64) : portrait ? 34 : 38;
    f[`ring${i}`] = { x: pt.x - d / 2, y: pt.y - d / 2, w: d, h: d, ...track(t, [[22.3 + i * 0.3, { o: 0, s: 0.6 }], [22.6 + i * 0.3, { o: 1, s: 1 }], [23.6, { o: 1, s: 1 }], [24.0, { o: 0, s: 1 }]]) };
  }
  const sh = coverPoint(anchors.panel3.shell.x, anchors.panel3.shell.y, panelRect(2), ART[2]);
  const sd = portrait ? 44 : 64;
  f.ringShell = { x: sh.x - sd / 2, y: sh.y - sd / 2, w: sd, h: sd, ...track(t, [[23.7, { o: 0, s: 0.6 }], [24.0, { o: 1, s: 1 }], [24.3, { o: 1, s: 1 }], [24.55, { o: 0, s: 1 }]]) };

  for (const i of [0, 1, 2]) {
    const p = panelRect(i), b = balloonSpec(i, layout);
    let pos = track(t, [[24.8, { x: b.start.fx, y: b.start.fy }], [25.6, { x: b.good.fx, y: b.good.fy }]]);
    if (i === 0 && ov.balloon0) pos = { x: ov.balloon0.fx, y: ov.balloon0.fy };
    const vis = track(t, [[24.0 + i * 0.12, { o: 0, s: 0.92 }], [24.4 + i * 0.12, { o: 1, s: 1 }]]);
    const rect: Rect = { x: p.x + pos.x! * p.w, y: p.y + pos.y! * p.h, w: b.fw * p.w, h: b.fh * p.h };
    f[`bal${i}`] = { ...rect, ...vis };
    const head = coverPoint(HEAD[i]!.x, HEAD[i]!.y, p, ART[i]!);
    const hr = (i === 2 ? 0.16 : 0.09) * p.h;
    f[`tail${i}`] = { d: tailPath(rect, head, hr), o: vis.o };
  }
  const watchSaved = !ov.balloon0 ? track(t, [[25.6, { o: 0 }], [25.9, { o: 1 }], [27.0, { o: 1 }], [27.3, { o: 0 }]]) : { o: balloonClear(ov.balloon0.fx, ov.balloon0.fy, layout) && t < 27 ? 1 : 0 };
  const b0 = f.bal0!;
  f.saved2 = { x: b0.x!, y: b0.y! + b0.h! + 4, o: watchSaved.o };

  // Scene 7: read and export; Scene 8: the end card
  f.exp = track(t, [[28.6, R(L.exp!, { o: 0, y: L.exp!.y + 8 })], [29.0, R(L.exp!, { o: 1 })], [32.0, R(L.exp!, { o: 1 })], [32.3, R(L.exp!, { o: 0 })]]);
  f.end = track(t, [[32.3, R(L.end!, { o: 0, y: L.end!.y + 10 })], [32.9, R(L.end!, { o: 1 })]]);
  f.progress = { s: clamp(t / DURATION) };
  return f;
}

export const sceneOfStop = (i: number) => SCENES.findIndex((s) => s.id === STOPS[i]!.scene);
