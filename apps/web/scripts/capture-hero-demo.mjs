#!/usr/bin/env node
/**
 * Renders the hero demo to video, frame by frame, from the capture route (/demo/capture).
 * Time is set with window.__heroDemo.seek(t), so the output does not depend on machine speed and matches
 * what the web page shows for the same t. Requires Playwright (chromium) and ffmpeg with libx264/libwebp.
 *
 *   node scripts/capture-hero-demo.mjs --base http://localhost:3200 --out ./hero-demo-video [--fps 30] [--posters]
 *
 * Writes hero-demo-landscape.mp4 (1280x1120), hero-demo-portrait.mp4 (720x1280) and poster-*.webp/png (the
 * final frame). --posters also copies the posters into public/hero-demo/ for the page's no-JS fallback.
 * Use a production build (`next build && next start`): the dev server overlays its own indicator.
 */
import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const base = arg("base", "http://localhost:3000");
const out = resolve(arg("out", "hero-demo-video"));
const fps = Number(arg("fps", "30"));
const posters = process.argv.includes("--posters");
const only = arg("layout", "");
const here = dirname(fileURLToPath(import.meta.url));
mkdirSync(out, { recursive: true });

const LAYOUTS = { landscape: { width: 640, height: 560 }, portrait: { width: 360, height: 640 } };

function ffmpeg(args) {
  const p = spawn("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", ...args], { stdio: ["pipe", "inherit", "inherit"] });
  const done = new Promise((ok, bad) => p.on("close", (c) => (c === 0 ? ok() : bad(new Error(`ffmpeg exited ${c}`)))));
  return { stdin: p.stdin, done };
}

const browser = await chromium.launch();
try {
  for (const [layout, viewport] of Object.entries(LAYOUTS)) {
    if (only && only !== layout) continue;
    const page = await browser.newPage({ viewport, deviceScaleFactor: 2, reducedMotion: "no-preference" });
    const requests = [];
    page.on("request", (r) => requests.push(r.url()));
    await page.goto(`${base}/demo/capture?layout=${layout}`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.__heroDemo);
    await page.evaluate(() => window.__heroDemo.ready);
    const duration = await page.evaluate(() => window.__heroDemo.duration);
    const root = page.locator("[data-capture-root]");
    const file = join(out, `hero-demo-${layout}.mp4`);
    const enc = ffmpeg(["-f", "image2pipe", "-framerate", String(fps), "-c:v", "png", "-i", "-",
      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", file]);
    const frames = Math.round(duration * fps);
    for (let i = 0; i <= frames; i++) {
      await page.evaluate((t) => window.__heroDemo.seek(t), i / fps);
      const png = await root.screenshot({ animations: "disabled", caret: "hide" });
      if (!enc.stdin.write(png)) await new Promise((r) => enc.stdin.once("drain", r));
      if (i % (fps * 6) === 0) process.stdout.write(`${layout}: ${(i / fps).toFixed(0)}s / ${duration}s\n`);
    }
    enc.stdin.end();
    await enc.done;
    // poster: the finished comic (final frame)
    await page.evaluate((t) => window.__heroDemo.seek(t), duration);
    const png = join(out, `poster-${layout}.png`);
    await root.screenshot({ path: png });
    const webp = join(out, `poster-${layout}.webp`);
    const w = ffmpeg(["-i", png, "-c:v", "libwebp", "-quality", "82", webp]);
    w.stdin.end();
    await w.done;
    if (posters) copyFileSync(webp, join(here, "..", "public", "hero-demo", `poster-${layout}.webp`));
    const external = requests.filter((u) => !u.startsWith(base));
    if (external.length) throw new Error(`unexpected requests: ${external.join(", ")}`);
    console.log(`wrote ${file} (${frames + 1} frames) and ${webp}`);
    await page.close();
  }
} finally {
  await browser.close();
}
