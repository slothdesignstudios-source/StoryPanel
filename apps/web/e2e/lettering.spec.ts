import { expect, test } from "@playwright/test";
import { PNG } from "pngjs";
import { writeFileSync } from "node:fs";
import { apiToken, finishedComic, signIn } from "./helpers";

/**
 * Visual regression: editor/reader lettering vs the server renderer used for exports.
 * Both draw the server's stored typesetting with the same font files. For each balloon we
 * binarise "ink" (dark pixels) in its box in both images and measure overlap (IoU).
 * A different line break moves whole words, which drops IoU sharply; the negative control
 * (pairing each balloon with a different balloon's server crop) shows what a mismatch scores.
 */
const WIDTH = 1000;
const OUT = process.env.E2E_SHOTS;

function decode(buf: Buffer) { return PNG.sync.read(buf); }
function ink(img: PNG, x0: number, y0: number, w: number, h: number): Uint8Array {
  const m = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = ((y0 + y) * img.width + (x0 + x)) * 4;
    m[y * w + x] = img.data[i]! + img.data[i + 1]! + img.data[i + 2]! < 3 * 110 ? 1 : 0;
  }
  return m;
}
function iou(a: Uint8Array, b: Uint8Array) {
  let inter = 0, union = 0;
  for (let i = 0; i < a.length; i++) { inter += a[i]! & b[i]!; union += a[i]! | b[i]!; }
  return union ? inter / union : 1;
}
/** Tolerate antialiasing: count a pixel as matched if the other image has ink within 1px. */
function dilate(m: Uint8Array, w: number, h: number) {
  const o = new Uint8Array(m.length);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let v = 0;
    for (let dy = -1; dy <= 1 && !v; dy++) for (let dx = -1; dx <= 1 && !v; dx++) {
      const yy = y + dy, xx = x + dx;
      if (yy >= 0 && yy < h && xx >= 0 && xx < w) v = m[yy * w + xx]!;
    }
    o[y * w + x] = v;
  }
  return o;
}
function overlapScore(a: Uint8Array, b: Uint8Array, w: number, h: number) {
  // symmetric: share of each image's ink that lands on (dilated) ink of the other
  const da = dilate(a, w, h), db = dilate(b, w, h);
  let ab = 0, na = 0, ba = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { na += a[i]!; nb += b[i]!; ab += a[i]! & db[i]!; ba += b[i]! & da[i]!; }
  return Math.min(na ? ab / na : 1, nb ? ba / nb : 1);
}

test("editor lettering matches the export renderer", async ({ page, request }) => {
  const email = `lettering-${Date.now()}@example.com`;
  const comic = await finishedComic(request, await apiToken(request, email),
    "Three friends named Ana, Bo and Cy argue about who gets the last dumpling. 2 pages, funny.");
  await signIn(page, email);
  await page.goto(`/projects/${comic.id}/read`);
  await expect(page.getByText(/Page 1 of/)).toBeVisible();
  // the lettering fonts must be the bundled files, not a system font or a fallback
  const faces = await page.evaluate(async () => {
    await Promise.all([document.fonts.load('22px "Comic Neue"'), document.fonts.load('48px "Bangers"')]);
    return [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family.replace(/"/g, ""));
  });
  expect(faces).toEqual(expect.arrayContaining(["Comic Neue", "Bangers"]));
  await page.waitForTimeout(500);

  const pg = comic.pages[0]!;
  const k = WIDTH / pg.size[0]!;
  const browserPng = await page.evaluate((w) => {
    const K = (window as unknown as { Konva: { stages: { width(): number; toDataURL(o: object): string }[] } }).Konva;
    const stage = K.stages[K.stages.length - 1]!;
    return stage.toDataURL({ pixelRatio: w / stage.width(), mimeType: "image/png" });
  }, WIDTH);
  const browser = decode(Buffer.from(browserPng.split(",")[1]!, "base64"));
  const server = decode(Buffer.from(await (await request.get(pg.preview_url!)).body()));
  expect(browser.width).toBe(server.width);

  const balloons = pg.panels.flatMap((p) => p.balloons);
  expect(balloons.length).toBeGreaterThan(2);
  const boxes = balloons.map((b) => {
    const g = b.geometry;
    const x0 = Math.max(0, Math.floor(g.x * k)), y0 = Math.max(0, Math.floor(g.y * k));
    return { b, x0, y0, w: Math.min(server.width - x0, Math.ceil(g.w * k)), h: Math.min(server.height - y0, Math.ceil(g.h * k)) };
  });
  const same: number[] = [], control: number[] = [];
  boxes.forEach((box, i) => {
    const { x0, y0, w, h } = box;
    same.push(overlapScore(ink(browser, x0, y0, w, h), ink(server, x0, y0, w, h), w, h));
    // negative control: this balloon's browser crop vs another balloon's server crop, same size
    const other = boxes[(i + 1) % boxes.length]!;
    const ow = Math.min(w, other.w), oh = Math.min(h, other.h);
    control.push(overlapScore(ink(browser, x0, y0, ow, oh), ink(server, other.x0, other.y0, ow, oh), ow, oh));
  });
  const report = boxes.map((b, i) => ({ text: b.b.text.slice(0, 40), match: +same[i]!.toFixed(3), control: +control[i]!.toFixed(3) }));
  console.log(JSON.stringify(report, null, 1));
  if (OUT) {
    writeFileSync(`${OUT}/lettering-browser.png`, PNG.sync.write(browser));
    writeFileSync(`${OUT}/lettering-server.png`, PNG.sync.write(server));
    writeFileSync(`${OUT}/lettering-report.json`, JSON.stringify(report, null, 1));
  }
  // Calibrated on this test: correct rendering scores >= 0.97; truncated line ends or a
  // covered letter scored 0.92 before they were fixed, so 0.95 is the regression line.
  for (const s of same) expect(s).toBeGreaterThan(0.95);
  expect(Math.max(...control)).toBeLessThan(0.6);
});
