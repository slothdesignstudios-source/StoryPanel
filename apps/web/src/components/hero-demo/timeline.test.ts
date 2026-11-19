import { describe, expect, it } from "vitest";
import { DEMO_ASSETS, allApproved } from "./manifest";
import { BALLOON, DURATION, LINES, NEW_LINE, OLD_LINE, SCENES, STOPS, balloonClear, coverPoint, frame, sceneAt, type Layout } from "./timeline";

const LAYOUTS: Layout[] = ["landscape", "portrait"];
const seen = (t: number, k: string, layout: Layout = "landscape") => (frame(t, layout)[k]?.o ?? 0) > 0.01;

describe("hero demo timeline", () => {
  it("is deterministic: the same time gives the same frame", () => {
    for (const layout of LAYOUTS) for (const t of [0, 3.3, 12.95, 17.9, 24.81, 36]) expect(frame(t, layout)).toEqual(frame(t, layout));
  });

  it("runs 30-36 seconds through the scenes in story order, ending on the finished comic", () => {
    expect(DURATION).toBeGreaterThanOrEqual(30);
    expect(DURATION).toBeLessThanOrEqual(36);
    expect(SCENES.map((s) => s.id)).toEqual(["idea", "cast", "bible", "script", "draw", "review", "read", "end"]);
    for (let i = 1; i < SCENES.length; i++) expect(SCENES[i]!.start).toBe(SCENES[i - 1]!.end);
    expect(sceneAt(DURATION)).toBe(SCENES.length - 1);
    for (let i = 1; i < STOPS.length; i++) expect(STOPS[i]!.t).toBeGreaterThan(STOPS[i - 1]!.t);
    // the final state is stable: nothing changes after the end card settles
    const still = (t: number) => { const { progress: _p, ...rest } = frame(t, "landscape"); return rest; };
    expect(still(34)).toEqual(still(DURATION));
  });

  it("reveals the panels in story order", () => {
    const start = (i: number) => { for (let t = 15; t < 26; t += 0.05) if (seen(t, `art${i}`)) return t; return Infinity; };
    expect(start(0)).toBeLessThan(start(1));
    expect(start(1)).toBeLessThan(start(2));
    // no art is shown before the script is approved
    expect(start(0)).toBeGreaterThan(STOPS.find((s) => s.gate === "approveScript")!.t);
  });

  it("shows the edited line replacing the old one, and only then 'Saved'", () => {
    expect(frame(12.5, "landscape").line1!.text).toBe(`“${OLD_LINE}”`);
    expect(frame(15, "landscape").line1!.text).toBe(`“${NEW_LINE}”`);
    expect(LINES[1]).toBe(NEW_LINE);
    expect(seen(13.5, "saved1")).toBe(false);
    expect(seen(15, "saved1")).toBe(true);
  });

  it("keeps the shell out of the story until panel 3 is drawn", () => {
    // the shell exists only in panel 3's artwork, which stays hidden until its reveal
    for (let t = 0; t < 20; t += 0.1) expect(seen(t, "art2")).toBe(false);
    expect(seen(36, "art2")).toBe(true);
  });

  it("accepts a balloon only when it is inside the panel and off the keeper's face", () => {
    for (const layout of LAYOUTS) {
      expect(balloonClear(BALLOON[0].start.fx, BALLOON[0].start.fy, layout)).toBe(false);
      expect(balloonClear(BALLOON[0].good.fx, BALLOON[0].good.fy, layout)).toBe(true);
      expect(balloonClear(-0.1, 0, layout)).toBe(false);
      expect(balloonClear(0.9, 0, layout)).toBe(false);
    }
  });

  it("holds a balloon the visitor placed, with its tail redrawn", () => {
    const a = frame(26, "landscape"), b = frame(26, "landscape", { balloon0: { fx: 0.5, fy: 0.05 } });
    expect(b.bal0!.x).toBeGreaterThan(a.bal0!.x!);
    expect(b.tail0!.d).not.toBe(a.tail0!.d);
  });

  it("maps artwork points through an object-fit: cover crop", () => {
    const r = { x: 10, y: 20, w: 200, h: 100 };
    expect(coverPoint(0.5, 0.5, r, { w: 400, h: 200 })).toEqual({ x: 110, y: 70 });
    // a taller image is cropped top and bottom
    const p = coverPoint(0, 0, r, { w: 100, h: 100 });
    expect(p.x).toBe(10);
    expect(p.y).toBe(20 - 50);
  });

  it("lists every asset, and labels the fixtures honestly", () => {
    for (const a of Object.values(DEMO_ASSETS)) {
      expect(a.src).toMatch(/^\/hero-demo\//);
      expect(a.width).toBeGreaterThan(0);
      expect(["development-fixture", "approved"]).toContain(a.status);
    }
    expect(allApproved()).toBe(false);
  });
});
