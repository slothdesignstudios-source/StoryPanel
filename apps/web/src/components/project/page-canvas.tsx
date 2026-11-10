"use client";

import type Konva from "konva";
import { useEffect, useMemo, useRef, useState } from "react";
import { Ellipse, Group, Image as KImage, Layer, Line, Rect, Stage, Text, Transformer } from "react-konva";
import { LINE_HEIGHT, fitText, innerBox } from "@/lib/text";
import type { Balloon, Geometry, Page, Panel } from "@/lib/types";

const INK = "#111111";
const COMIC = '"Comic Neue"';
const SFX = '"Bangers"';

function useImage(url: string | null | undefined) {
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    if (!url) return setImg(null);
    const el = new window.Image();
    el.onload = () => setImg(el);
    el.onerror = () => setImg(null);
    el.src = url;
    return () => { el.onload = null; el.onerror = null; };
  }, [url]);
  return img;
}

function PanelArt({ panel, selected, onSelect }: { panel: Panel; selected: boolean; onSelect?: () => void }) {
  const img = useImage(panel.image_url);
  const [x, y, w, h] = panel.rect as [number, number, number, number];
  // "cover" crop, matching the server-side compositor
  const crop = useMemo(() => {
    if (!img) return undefined;
    const scale = Math.max(w / img.width, h / img.height);
    const cw = w / scale, ch = h / scale;
    return { x: (img.width - cw) / 2, y: (img.height - ch) / 2, width: cw, height: ch };
  }, [img, w, h]);
  const label = panel.status === "blocked" ? "Blocked" : panel.status === "failed" ? "Couldn't draw" : panel.busy ? "Drawing…" : "";
  return (
    <Group onClick={onSelect} onTap={onSelect}>
      {img ? <KImage image={img} x={x} y={y} width={w} height={h} crop={crop} /> : <Rect x={x} y={y} width={w} height={h} fill="#e8e8e8" />}
      {!img && label && <Text x={x} y={y + h / 2 - 12} width={w} align="center" text={label} fontSize={22} fontFamily="Inter Variable, sans-serif" fill="#666" />}
      <Rect x={x} y={y} width={w} height={h} stroke={selected ? "#17726d" : INK} strokeWidth={selected ? 7 : 3} listening={false} />
    </Group>
  );
}

function tailPoints(g: Geometry): number[] | null {
  if (!g.tail) return null;
  const cx = g.x + g.w / 2, cy = g.y + g.h / 2;
  const dx = g.tail.x - cx, dy = g.tail.y - cy;
  const dist = Math.hypot(dx, dy);
  if (dist < 1) return null;
  const nx = -dy / dist, ny = dx / dist;
  const half = Math.min(g.w, g.h) * 0.12;
  const bx = cx + (dx / dist) * Math.min(g.w, g.h) * 0.3, by = cy + (dy / dist) * Math.min(g.w, g.h) * 0.3;
  return [bx + nx * half, by + ny * half, g.tail.x, g.tail.y, bx - nx * half, by - ny * half];
}

function BalloonShape({ b, g, selected, editable, onSelect, onChange }: { b: Balloon; g: Geometry; selected: boolean; editable: boolean; onSelect?: () => void; onChange?: (g: Geometry, commit: boolean) => void }) {
  const ref = useRef<Konva.Group>(null);
  const tr = useRef<Konva.Transformer>(null);
  useEffect(() => {
    if (selected && editable && ref.current && tr.current) {
      tr.current.nodes([ref.current]);
      tr.current.getLayer()?.batchDraw();
    }
  }, [selected, editable]);
  const [iw, ih] = innerBox(b.type, g.w, g.h);
  const sfx = b.type === "sfx";
  const fit = sfx ? null : fitText(b.text, COMIC, iw, ih, g.font_size);
  const tail = b.type === "speech" || b.type === "thought" ? tailPoints(g) : null;
  const move = (node: Konva.Node, commit: boolean) => {
    const nx = node.x() - g.w / 2, ny = node.y() - g.h / 2;
    const dx = nx - g.x, dy = ny - g.y;
    onChange?.({ ...g, x: nx, y: ny, tail: g.tail ? { x: g.tail.x + dx, y: g.tail.y + dy } : g.tail }, commit);
  };
  return (
    <>
      {tail && b.type === "speech" && <Line points={tail} closed fill="#fff" stroke={INK} strokeWidth={2.6} listening={false} />}
      {tail && b.type === "thought" && [0.55, 0.78, 0.95].map((t, n) => {
        const sx = (tail[0]! + tail[4]!) / 2, sy = (tail[1]! + tail[5]!) / 2;
        return <Ellipse key={n} x={sx + (tail[2]! - sx) * t} y={sy + (tail[3]! - sy) * t} radiusX={9 - 3 * n} radiusY={9 - 3 * n} fill="#fff" stroke={INK} strokeWidth={2.6} listening={false} />;
      })}
      <Group
        ref={ref} x={g.x + g.w / 2} y={g.y + g.h / 2} offsetX={g.w / 2} offsetY={g.h / 2} rotation={sfx ? g.rotation ?? 0 : 0}
        draggable={editable} onClick={onSelect} onTap={onSelect} onDragStart={onSelect}
        onDragMove={(e) => move(e.target, false)} onDragEnd={(e) => move(e.target, true)}
        onTransformEnd={() => {
          const node = ref.current!;
          const w = Math.max(40, g.w * node.scaleX()), h = Math.max(30, g.h * node.scaleY());
          node.scaleX(1); node.scaleY(1);
          onChange?.({ ...g, w, h, x: node.x() - w / 2, y: node.y() - h / 2 }, true);
        }}
      >
        {b.type === "caption" && <Rect width={g.w} height={g.h} fill="#fff4c8" stroke={INK} strokeWidth={2.6} />}
        {(b.type === "speech" || b.type === "thought") && <Ellipse x={g.w / 2} y={g.h / 2} radiusX={g.w / 2} radiusY={g.h / 2} fill="#fff" stroke={INK} strokeWidth={2.6} dash={b.type === "thought" ? [10, 6] : undefined} />}
        {sfx ? (
          <Text width={g.w} height={g.h} align="center" verticalAlign="middle" text={b.text.toUpperCase()} fontFamily={SFX} fontSize={g.font_size} fill="#ffdd33" stroke={INK} strokeWidth={2.5} fillAfterStrokeEnabled />
        ) : (
          <Text x={(g.w - iw) / 2} y={g.h / 2 - (fit!.lines.length * fit!.size * LINE_HEIGHT) / 2} width={iw} align="center" text={fit!.lines.join("\n")} wrap="none" fontFamily={COMIC} fontSize={fit!.size} lineHeight={LINE_HEIGHT} fill={INK} />
        )}
        {selected && !editable && <Rect width={g.w} height={g.h} stroke="#17726d" strokeWidth={3} dash={[8, 5]} listening={false} />}
      </Group>
      {tail && b.type === "speech" && <Line points={[tail[0]! * 0.94 + (g.x + g.w / 2) * 0.06, tail[1]! * 0.94 + (g.y + g.h / 2) * 0.06, (tail[0]! + tail[4]! + tail[2]! * 4) / 6, (tail[1]! + tail[5]! + tail[3]! * 4) / 6, tail[4]! * 0.94 + (g.x + g.w / 2) * 0.06, tail[5]! * 0.94 + (g.y + g.h / 2) * 0.06]} closed fill="#fff" listening={false} />}
      {selected && editable && <Transformer ref={tr} rotateEnabled={false} keepRatio={false} flipEnabled={false} anchorSize={12} borderStroke="#17726d" anchorStroke="#17726d" boundBoxFunc={(o, n) => (n.width < 40 || n.height < 30 ? o : n)} />}
    </>
  );
}

export type CanvasProps = {
  page: Page;
  width: number;
  editable?: boolean;
  selectedPanel?: string | null;
  selectedBalloon?: string | null;
  drafts?: Record<string, Geometry>;
  onSelectPanel?: (id: string | null) => void;
  onSelectBalloon?: (id: string | null) => void;
  onBalloonChange?: (id: string, g: Geometry, commit: boolean) => void;
};

/** One comic page: panel art plus the vector lettering layer. Read-only unless `editable`. */
export default function PageCanvas({ page, width, editable = false, selectedPanel, selectedBalloon, drafts, onSelectPanel, onSelectBalloon, onBalloonChange }: CanvasProps) {
  const [pw, ph] = page.size as [number, number];
  const k = width / pw;
  const [fontsReady, setFontsReady] = useState(false);
  useEffect(() => {
    // Text must be measured with the real fonts, or balloons would re-wrap after they load.
    void Promise.all([document.fonts.load('22px "Comic Neue"'), document.fonts.load('48px "Bangers"')]).finally(() => setFontsReady(true));
  }, []);
  const balloons = page.panels.flatMap((p) => p.balloons);
  return (
    <Stage width={width} height={ph * k} scaleX={k} scaleY={k} aria-hidden onMouseDown={(e) => { if (e.target === e.target.getStage()) { onSelectBalloon?.(null); } }}>
      <Layer>
        <Rect width={pw} height={ph} fill="#fff" listening={false} />
        {page.panels.map((p) => <PanelArt key={p.id} panel={p} selected={selectedPanel === p.id && !selectedBalloon} onSelect={onSelectPanel ? () => { onSelectPanel(p.id); onSelectBalloon?.(null); } : undefined} />)}
      </Layer>
      <Layer key={fontsReady ? "fonts" : "fallback"}>
        {balloons.map((b) => (
          <BalloonShape key={b.id} b={b} g={drafts?.[b.id] ?? (b.geometry as Geometry)} selected={selectedBalloon === b.id} editable={editable}
            onSelect={onSelectBalloon ? () => { onSelectBalloon(b.id); onSelectPanel?.(b.panel_id); } : undefined}
            onChange={(g, commit) => onBalloonChange?.(b.id, g, commit)} />
        ))}
      </Layer>
    </Stage>
  );
}
