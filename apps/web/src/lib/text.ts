/** Text fitting for balloons: the same greedy algorithm the server-side letterer uses. */
export const LINE_HEIGHT = 1.16;
// Same values as the server (lettering.py), checked by tests/test_balloon_text_bounds.py.
export const ELLIPSE_INSET = 0.66;
export const THOUGHT_INSET = 0.58;
export const BOX_PAD = 10;
/** Smallest readable lettering in page units (lettering.READABLE_MIN): text is never set smaller. */
export const READABLE_MIN = 15;

let ctx: CanvasRenderingContext2D | null = null;
function measure(text: string, font: string, size: number): number {
  if (typeof document === "undefined") return text.length * size * 0.5;
  ctx ??= document.createElement("canvas").getContext("2d");
  if (!ctx) return text.length * size * 0.5;
  ctx.font = `${size}px ${font}`;
  return ctx.measureText(text).width;
}

export function wrap(text: string, font: string, size: number, maxWidth: number): string[] {
  const lines: string[] = [];
  let current = "";
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const trial = current ? `${current} ${word}` : word;
    if (current && measure(trial, font, size) > maxWidth) {
      lines.push(current);
      current = word;
    } else current = trial;
  }
  if (current) lines.push(current);
  return lines.length ? lines : [""];
}

export function innerBox(type: string, w: number, h: number): [number, number] {
  const inset = type === "thought" ? THOUGHT_INSET : ELLIPSE_INSET;
  return type === "speech" || type === "thought" ? [w * inset, h * inset] : [Math.max(1, w - 2 * BOX_PAD), Math.max(1, h - 2 * BOX_PAD)];
}

export function fitText(text: string, font: string, boxW: number, boxH: number, maxSize: number, minSize = READABLE_MIN) {
  let size = Math.max(maxSize, minSize);
  for (;;) {
    const lines = wrap(text, font, size, boxW);
    const width = Math.max(...lines.map((l) => measure(l, font, size)));
    const height = lines.length * size * LINE_HEIGHT;
    if ((width <= boxW + 0.5 && height <= boxH + 0.5) || size <= minSize) return { size, lines, fits: width <= boxW + 0.5 && height <= boxH + 0.5 };
    size = Math.max(minSize, size - 1);
  }
}

export const wordCount = (text: string) => text.trim().split(/\s+/).filter(Boolean).length;
