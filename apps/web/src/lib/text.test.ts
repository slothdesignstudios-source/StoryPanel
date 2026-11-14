import { describe, expect, it } from "vitest";
import { fitText, innerBox, wordCount, wrap } from "./text";

describe("balloon text fitting", () => {
  it("wraps greedily and never drops words", () => {
    const lines = wrap("one two three four five six", "x", 20, 60);
    expect(lines.join(" ")).toBe("one two three four five six");
    expect(lines.length).toBeGreaterThan(1);
  });
  it("shrinks the font until the text fits, and reports when it cannot", () => {
    const ok = fitText("Hi there", "x", 200, 60, 22);
    expect(ok.fits).toBe(true);
    expect(ok.size).toBe(22);
    const tight = fitText("a much longer line of dialogue than this little box can hold", "x", 60, 30, 22);
    expect(tight.size).toBeLessThan(22);
    expect(tight.fits).toBe(false);
  });
  it("uses the ellipse inset for speech and padding for captions", () => {
    innerBox("speech", 100, 100).forEach((v) => expect(v).toBeCloseTo(66));
    innerBox("thought", 100, 100).forEach((v) => expect(v).toBeCloseTo(58));
    expect(innerBox("caption", 100, 100)).toEqual([80, 80]);
  });
  it("counts words the way the 25-word rule does", () => {
    expect(wordCount("  one   two\nthree ")).toBe(3);
    expect(wordCount("")).toBe(0);
  });
});
