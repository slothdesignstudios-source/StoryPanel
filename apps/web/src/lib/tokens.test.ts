import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/** Reads the design tokens from globals.css and checks WCAG 2.2 contrast, so a colour change that
 *  breaks legibility fails here rather than in someone's browser. */
const css = readFileSync(join(__dirname, "../app/globals.css"), "utf8");
const token = (name: string): string => {
  const m = css.match(new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) throw new Error(`token ${name} not found`);
  return m[1]!;
};

function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}
const contrast = (a: string, b: string) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
};
/** A colour at `alpha` over `bg`, as the `/10`-style Tailwind tints render. */
const tint = (fg: string, bg: string, alpha: number) =>
  "#" + [1, 3, 5].map((i) => Math.round(parseInt(fg.slice(i, i + 2), 16) * alpha + parseInt(bg.slice(i, i + 2), 16) * (1 - alpha)).toString(16).padStart(2, "0")).join("");

describe("design tokens meet contrast requirements", () => {
  const surfaces = { paper: token("paper"), card: token("card") };

  it.each(["ink", "muted", "accent", "accent-strong", "ok", "warn", "danger"])("%s text is at least 4.5:1 on paper and card", (name) => {
    for (const bg of Object.values(surfaces)) expect(contrast(token(name), bg)).toBeGreaterThanOrEqual(4.5);
  });

  it("button text is at least 4.5:1 on the primary action colours", () => {
    expect(contrast(token("accent-ink"), token("accent"))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token("accent-ink"), token("accent-strong"))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(["ok", "warn", "danger", "accent"])("%s text is at least 4.5:1 on its own 10%% tint (badges, notices)", (name) => {
    expect(contrast(token(name), tint(token(name), token("card"), 0.1))).toBeGreaterThanOrEqual(4.5);
  });

  it("selected rows and chips keep text legible", () => {
    for (const name of ["ink", "muted", "accent"]) expect(contrast(token(name), token("accent-soft"))).toBeGreaterThanOrEqual(4.5);
  });

  it("form field borders and the focus ring are at least 3:1 (WCAG 1.4.11)", () => {
    for (const bg of Object.values(surfaces)) {
      expect(contrast(token("field"), bg)).toBeGreaterThanOrEqual(3);
      expect(contrast(token("accent"), bg)).toBeGreaterThanOrEqual(3);
    }
  });
});
