#!/usr/bin/env python3
"""Read-only time and cost report for ONE comic, from the paid-request ledger (provider_attempts).

    scripts/live-env.sh services/backend/.venv/bin/python scripts/stage-report.py <comic-id> [--json out.json]

For each stage and operation: calls, attempts by status, latency (min / median / max / total) and
counted cost; then wall-clock time from the first to the last call against the summed call time
(how much ran in parallel), and the panel chains that had to run in order (panels in one location
use the previous panel as a reference). Nothing is changed: the queries run in a READ ONLY
transaction, and only this comic's rows are read. No prompts, text or images are printed.

On simulated providers the latencies are those of the fakes (milliseconds) and say nothing about
real performance; the call counts and the chain structure are still exact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))


async def report(pid: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    async with transaction() as db:
        await db.execute(text("set transaction read only"))
        project = (await db.execute(text(
            "select status, format, image_provider, created_at, completed_at from projects where id = :p"),
            {"p": pid})).mappings().one_or_none()  # fmt: skip
        if project is None:
            raise SystemExit(f"no comic {pid}")
        rows = (await db.execute(text("""
            select stage, operation, provider, model, status, latency_ms, cost_counted_usd, created_at, updated_at
            from provider_attempts where project_id = :p order by created_at"""), {"p": pid})).mappings().all()  # fmt: skip
        panels = (await db.execute(text("""
            select pg.index as page, pn.index as panel, pn.spec->>'location_id' as loc
            from panels pn join pages pg on pg.id = pn.page_id where pn.project_id = :p
            order by pg.index, pn.index"""), {"p": pid})).mappings().all()  # fmt: skip
        versions = (await db.execute(text("""
            select pn.id, count(v.id) as attempts from panels pn left join panel_versions v on v.panel_id = pn.id
            where pn.project_id = :p group by pn.id"""), {"p": pid})).mappings().all()  # fmt: skip
    stages: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = stages.setdefault(f"{r['stage']}|{r['operation']}", {"provider": r["provider"], "model": r["model"], "calls": 0,
                                                               "statuses": {}, "latency_ms": [], "cost_usd": Decimal(0)})  # fmt: skip
        s["calls"] += 1
        s["statuses"][r["status"]] = s["statuses"].get(r["status"], 0) + 1
        if r["latency_ms"] is not None:
            s["latency_ms"].append(int(r["latency_ms"]))
        s["cost_usd"] += r["cost_counted_usd"] or 0
    table = []
    for key, s in sorted(stages.items(), key=lambda kv: -sum(kv[1]["latency_ms"])):
        lat = s["latency_ms"]
        table.append({"stage": key, "provider": s["provider"], "model": s["model"], "calls": s["calls"],
                      "statuses": s["statuses"], "latency_ms": {
                          "min": min(lat) if lat else None, "median": int(statistics.median(lat)) if lat else None,
                          "max": max(lat) if lat else None, "total": sum(lat)},
                      "cost_counted_usd": str(s["cost_usd"])})  # fmt: skip
    chains: list[int] = []
    last = None
    for p in panels:
        if last is not None and p["page"] == last["page"] and p["loc"] == last["loc"]:
            chains[-1] += 1
        else:
            chains.append(1)
        last = p
    total_ms = sum(int(r["latency_ms"] or 0) for r in rows)
    wall_ms = int((rows[-1]["updated_at"] - rows[0]["created_at"]).total_seconds() * 1000) if rows else 0
    return {
        "comic": str(pid), "status": project["status"], "format": project["format"],
        "image_provider": project["image_provider"],
        "calls": len(rows), "summed_call_ms": total_ms, "wall_clock_ms": wall_ms,
        "parallelism": round(total_ms / wall_ms, 2) if wall_ms else None,
        "panel_chains": chains, "longest_sequential_chain": max(chains, default=0),
        "panel_attempts": sorted(int(v["attempts"]) for v in versions),
        "stages": table,
        "note": "wall clock includes time waiting for you at the approval gates",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("comic_id")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    out = asyncio.run(report(uuid.UUID(a.comic_id)))
    if a.json:
        a.json.write_text(json.dumps(out, indent=1, default=str))
    print(f"comic {out['comic']} ({out['status']}, {out['format']}, images: {out['image_provider']})")
    print(f"calls {out['calls']}; summed call time {out['summed_call_ms']} ms; wall clock {out['wall_clock_ms']} ms "
          f"(includes approval waits); parallelism {out['parallelism']}")
    print(f"panel chains (drawn in order within each): {out['panel_chains']}; attempts per panel: {out['panel_attempts']}")
    print(f"{'stage|operation':42} {'model':24} {'calls':>5} {'median ms':>9} {'total ms':>9} {'cost USD':>9}  statuses")
    for s in out["stages"]:
        print(f"{s['stage']:42} {str(s['model'])[:24]:24} {s['calls']:>5} {str(s['latency_ms']['median']):>9} "
              f"{s['latency_ms']['total']:>9} {s['cost_counted_usd']:>9}  {s['statuses']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
