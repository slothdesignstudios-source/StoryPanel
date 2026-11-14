"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { API_URL, authHeader } from "./api";
import { pk } from "@/lib/session";

export type StreamState = "connecting" | "live" | "reconnecting";

/**
 * Subscribes to a project's server-sent events and keeps the cached project fresh.
 * Events are a signal to refetch authoritative state, so a missed or duplicated event can
 * never leave the screen wrong. Reconnects resume from the last sequence id seen.
 */
export function useProjectEvents(projectId: string, enabled: boolean, onEvent?: (type: string, data: Record<string, unknown>) => void) {
  const qc = useQueryClient();
  const [state, setState] = useState<StreamState>("connecting");
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    if (!enabled) return;
    const abort = new AbortController();
    let lastId = "";
    let attempt = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        void qc.invalidateQueries({ queryKey: pk("project", projectId) });
        // Background work (charges, holds, releases) changes the balance: keep the header,
        // the Credits page and cost estimates current without a reload.
        void qc.invalidateQueries({ queryKey: pk("me") });
        void qc.invalidateQueries({ queryKey: pk("credits") });
        void qc.invalidateQueries({ queryKey: pk("estimate") });
      }, 120);
    };

    async function run() {
      while (!abort.signal.aborted) {
        try {
          const res = await fetch(`${API_URL}/v1/projects/${projectId}/events`, {
            headers: { ...(await authHeader()), Accept: "text/event-stream", ...(lastId ? { "Last-Event-ID": lastId } : {}) },
            signal: abort.signal,
            cache: "no-store",
          });
          if (res.status === 401 || res.status === 404) return;
          if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
          setState("live");
          attempt = 0;
          if (lastId) refresh(); // catch up on anything that changed while disconnected
          const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += value;
            let cut: number;
            while ((cut = buffer.indexOf("\n\n")) >= 0) {
              const frame = buffer.slice(0, cut);
              buffer = buffer.slice(cut + 2);
              let type = "message";
              let data = "";
              for (const line of frame.split("\n")) {
                if (line.startsWith("id: ")) lastId = line.slice(4);
                else if (line.startsWith("event: ")) type = line.slice(7);
                else if (line.startsWith("data: ")) data += line.slice(6);
              }
              if (!data) continue;
              try {
                handler.current?.(type, JSON.parse(data) as Record<string, unknown>);
              } catch {
                /* a malformed frame only costs a refetch */
              }
              refresh();
            }
          }
        } catch {
          if (abort.signal.aborted) return;
        }
        setState("reconnecting");
        attempt += 1;
        const wait = Math.min(15_000, 500 * 2 ** attempt) * (0.5 + Math.random() / 2);
        await new Promise((r) => setTimeout(r, wait));
      }
    }
    void run();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [projectId, enabled, qc]);

  return state;
}
