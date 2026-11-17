#!/usr/bin/env python3
"""Matched A/B comparison of comic quality between two code versions (evals/quality/).

    # cost estimate only (no calls):
    services/backend/.venv/bin/python scripts/quality-ab.py plan [--cases a,b,c]
    # one arm, against the stack currently running that arm's code:
    scripts/live-env.sh services/backend/.venv/bin/python scripts/quality-ab.py run --run-id R --arm old \
        --approve-usd X [--cases a,b,c] [--simulated]
    # blind review sheets once both arms have run:
    services/backend/.venv/bin/python scripts/quality-ab.py blind --run-id R --arms old,new

`run` creates one comic per case with the case's fixed prompt and image provider, approves the cast
and the script exactly as generated (confirming every flagged item, the same way for both arms; no
human edits), waits for the pages, exports PDF and PNG, and writes per case: the run record (status,
timings, repairs, panels needing attention, lettering flags, story warnings, cost by stage), the
exports and a full dossier (scripts/comic-dossier.py). Every case is recorded, failures included;
nothing is retried to get a better result.

Paid calls: `run` without --simulated refuses unless SP_PROVIDER_MODE=live and --approve-usd covers
the pessimistic estimate for the chosen cases. The approved amount is stored as a lifetime spending
cap on a dedicated evaluation user, so the app stops starting new provider calls at that amount.
Calls already sent can still overrun it by what docs/smoke-test-cost.md section 4 describes.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import random
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))
CASES = json.loads((ROOT / "evals/quality/quality-v2.json").read_text())
API = os.environ.get("SP_QUALITY_API", "http://127.0.0.1:8000")
RUNS = ROOT / "evals/quality/runs"

# Cost model (USD), from docs/live-test-proposal.md (OpenAI gpt-image-2.5-sunburst, prices verified
# 2026-11-15; token counts per image are UNDOCUMENTED assumptions): 12 images cost 1.65 expected and
# 3.76 pessimistic. Text and checks per panel from the same table (27 Anthropic + 4 Gemini calls for
# 3 panels, 0.20 USD), rounded up.
IMAGE_EXPECTED, IMAGE_PESSIMISTIC = Decimal("0.138"), Decimal("0.314")
TEXT_PER_COMIC, TEXT_PER_PANEL = Decimal("0.14"), Decimal("0.03")
# Images per case: 3 sheet images per character, 1 per location, 1 per panel, plus repairs
# (1 expected; pessimistic: one per panel, and a second one for a third of the panels, since the
# retry cap is two and a second repair of the same problem is no longer made).
SHAPE = {  # characters, locations, panels: assumptions for the estimate, not limits
    "prop-lighthouse": (2, 2, 3), "twist-cat-fishbowl": (2, 2, 4), "action-apprentice": (1, 2, 5),
    "emotion-garden": (1, 2, 5), "dialogue-roadtrip": (2, 2, 5), "page-dragon-mail": (2, 3, 5),
    "strip-robot-noodles": (2, 2, 4), "reveal-train-ticket": (2, 2, 6), "reveal-tulips-garden": (1, 1, 5),
}


def estimate(case_ids: list[str]) -> dict[str, Any]:
    rows, exp_total, pes_total = [], Decimal(0), Decimal(0)
    for cid in case_ids:
        chars, locs, panels = SHAPE[cid]
        base = 3 * chars + locs + panels
        exp_images, pes_images = base + 1, base + panels + (panels + 2) // 3
        text = TEXT_PER_COMIC + TEXT_PER_PANEL * panels
        exp = exp_images * IMAGE_EXPECTED + text
        pes = pes_images * IMAGE_PESSIMISTIC + text
        rows.append({"case": cid, "images_expected": exp_images, "images_pessimistic": pes_images,
                     "expected_usd": round(float(exp), 2), "pessimistic_usd": round(float(pes), 2)})  # fmt: skip
        exp_total += exp
        pes_total += pes
    return {"per_arm": rows, "per_arm_expected_usd": round(float(exp_total), 2),
            "per_arm_pessimistic_usd": round(float(pes_total), 2),
            "two_arms_expected_usd": round(float(2 * exp_total), 2),
            "two_arms_pessimistic_usd": round(float(2 * pes_total), 2)}  # fmt: skip


def _dossier_module() -> Any:
    spec = importlib.util.spec_from_file_location("comic_dossier", ROOT / "scripts/comic-dossier.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def ledger(pid: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    async with transaction() as db:
        await db.execute(text("set transaction read only"))
        rows = (await db.execute(text("""
            select stage, count(*) as calls, coalesce(sum(cost_counted_usd), 0) as usd,
                   coalesce(sum(latency_ms), 0) as ms, sum(case when status in ('unresolved','timeout') then 1 else 0 end) as unresolved
            from provider_attempts where project_id = :p group by stage order by stage"""), {"p": pid})).mappings().all()  # fmt: skip
    return {"by_stage": {r["stage"]: {"calls": r["calls"], "usd": str(r["usd"]), "latency_ms": r["ms"],
                                      "unresolved": r["unresolved"]} for r in rows},
            "total_usd": str(sum((Decimal(str(r["usd"])) for r in rows), Decimal(0)))}  # fmt: skip


async def run_case(h: Any, H: dict[str, str], case: dict[str, Any], out: Path, run_id: str, arm: str) -> dict[str, Any]:
    started = time.monotonic()
    timings: dict[str, float] = {}
    rec: dict[str, Any] = {"case": case["id"], "arm": arm, "prompt": case["prompt"]}

    async def until(pid: str, ok: Any, what: str, limit: float = 2400) -> dict[str, Any]:
        while time.monotonic() - started < limit:
            p = (await h.get(f"{API}/v1/projects/{pid}", headers=H)).json()
            if ok(p):
                timings[what] = round(time.monotonic() - started, 1)
                return p  # type: ignore[no-any-return]
            if p["status"] in ("failed", "blocked", "cancelled", "expired"):
                raise RuntimeError(f"{what}: comic ended {p['status']}: {p.get('failure')}")
            await asyncio.sleep(2)
        raise RuntimeError(f"{what}: timed out")

    p = (await h.post(f"{API}/v1/projects", headers={**H, "Idempotency-Key": f"q-{run_id}-{arm}-{case['id']}"},
                      json={"prompt": case["prompt"], "image_provider": CASES["image_provider"]})).json()  # fmt: skip
    pid = p.get("id")
    rec["project_id"] = pid
    if not pid or p.get("status") in ("completed", "partially_completed", "failed", "blocked", "cancelled", "expired"):
        rec["error"] = f"not started or already finished ({p.get('status')}); never re-run for a better result"
        return rec
    try:
        p = await until(pid, lambda p: p["status"] in ("awaiting_cast_approval", "awaiting_questions"), "cast")
        if p["status"] == "awaiting_questions":
            await h.post(f"{API}/v1/projects/{pid}/answers", headers=H, json={"answers": {}})
            p = await until(pid, lambda p: p["status"] == "awaiting_cast_approval", "cast")
        p = await until(pid, lambda p: all((c.get("sheet_check") or {}).get("status") != "pending" for c in p["characters"]),
                        "sheet_review")  # fmt: skip
        acks = [c["review"] for c in p["characters"] if c.get("review")]
        rec["cast_flags"] = [{"character": a.get("character"), "issue": a["issue"]} for a in acks]
        r = await h.post(f"{API}/v1/projects/{pid}/characters/approve", headers=H,
                         json={"cast_version": p["cast_version"], "acknowledged": [a["key"] for a in acks]})  # fmt: skip
        if r.status_code != 200:
            raise RuntimeError(f"cast approval refused: {r.status_code} {r.text[:200]}")
        p = await until(pid, lambda p: p["status"] == "awaiting_script_approval"
                        and ((p.get("script") or {}).get("consistency") or {}).get("status") != "pending", "script")  # fmt: skip
        script = p["script"]
        rec["script_issues"] = [{"code": i["code"], "severity": i.get("severity", "error"), "message": i["message"]}
                                for i in script["issues"]]  # fmt: skip
        if any(i.get("severity", "error") == "error" for i in script["issues"]):
            raise RuntimeError("script has blocking errors after the automatic repairs; stopped before any panel image")
        r = await h.post(f"{API}/v1/projects/{pid}/script/approve", headers=H, json={
            "script_version": p["script_version"], "acknowledged": [x["key"] for x in script["review_required"]]})  # fmt: skip
        if r.status_code != 200:
            raise RuntimeError(f"script approval refused: {r.status_code} {r.text[:200]}")
        p = await until(pid, lambda p: p["status"] in ("completed", "partially_completed")
                        and all(pg["preview_url"] for pg in p["pages"]), "pages")  # fmt: skip
        panels = [pn for pg in p["pages"] for pn in pg["panels"]]
        rec["panel_status"] = [pn["status"] for pn in panels]
        rec["status"] = p["status"]
        for fmt in ("pdf", "png"):
            e = (await h.post(f"{API}/v1/projects/{pid}/exports", headers={**H, "Idempotency-Key": f"q-{run_id}-{arm}-{case['id']}-{fmt}"},
                              json={"format": fmt})).json()  # fmt: skip
            q = await until(pid, lambda q, i=e["id"]: any(x["id"] == i and x["status"] in ("ready", "failed")
                                                           for x in q["exports"]), f"export_{fmt}")  # fmt: skip
            ex = next(x for x in q["exports"] if x["id"] == e["id"])
            if ex["status"] == "ready":
                data = (await h.get(ex["download_url"])).content
                (out / f"comic.{'zip' if fmt == 'png' and data[:4] != b'\x89PNG' else fmt}").write_bytes(data)
                rec.setdefault("print_quality", {})[fmt] = ex.get("print_quality")
    except Exception as exc:  # noqa: BLE001 - every case is recorded, including failures
        rec["error"] = str(exc)[:400]
    rec["timings_s"] = timings
    if pid:
        rec["ledger"] = await ledger(uuid.UUID(pid))
        try:
            d = await _dossier_module().dossier(uuid.UUID(pid), out / "dossier")
            (out / "dossier" / "dossier.json").write_text(json.dumps(d, indent=1, default=str))
            versions = [v for pg in d["pages"] for pn in pg["panels"] for v in pn["versions"]]
            rec["panel_attempts"] = len(versions)
            rec["repairs"] = sum(1 for v in versions if v["attempt"] > 0)
            rec["repairs_stopped_as_repeat"] = sum(1 for v in versions if v.get("repeated_after_repair"))
            rec["needs_attention"] = sum(1 for pg in d["pages"] for pn in pg["panels"] if pn["status"] == "needs_attention")
        except Exception as exc:  # noqa: BLE001
            rec["dossier_error"] = str(exc)[:300]
        rec["lettering_flags"] = await lettering_flags(uuid.UUID(pid))
    return rec


async def lettering_flags(pid: uuid.UUID) -> dict[str, int]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    async with transaction() as db:
        rows = (await db.execute(text("select flags from balloons where project_id = :p"), {"p": pid})).scalars().all()
    out: dict[str, int] = {}
    for flags in rows:
        for f in flags or []:
            out[f] = out.get(f, 0) + 1
    return out


async def run(args: argparse.Namespace) -> int:
    import httpx
    from sqlalchemy import text

    from storypanel.config import get_settings
    from storypanel.db.models import SpendCap
    from storypanel.db.session import transaction

    ids = args.cases.split(",") if args.cases else [c["id"] for c in CASES["cases"]]
    cases = [c for c in CASES["cases"] if c["id"] in ids]
    if not args.simulated:
        if get_settings().provider_mode != "live":
            print("refusing: SP_PROVIDER_MODE is not live (use --simulated for a free harness check)", file=sys.stderr)
            return 2
        need = Decimal(str(estimate(ids)["per_arm_pessimistic_usd"]))
        if args.approve_usd is None or args.approve_usd < need:
            print(f"refusing: --approve-usd {args.approve_usd} is below USD {need}, the pessimistic estimate for "
                  f"{len(ids)} case(s) in one arm", file=sys.stderr)  # fmt: skip
            return 2
    elif get_settings().provider_mode == "live":
        print("refusing: --simulated against a live stack would make paid calls", file=sys.stderr)
        return 2
    out_root = RUNS / args.run_id / args.arm
    if out_root.exists():
        print(f"refusing: {out_root} exists; every arm runs once per run id", file=sys.stderr)
        return 2
    out_root.mkdir(parents=True)
    email = f"quality-{args.run_id}-{args.arm}@example.com"
    results = []
    async with httpx.AsyncClient(timeout=60) as h:
        tok = (await h.post(f"{API}/v1/dev/login", json={"email": email})).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        uid = uuid.UUID((await h.get(f"{API}/v1/me", headers=H)).json()["id"])
        async with transaction() as db:
            if not args.simulated:
                db.add(SpendCap(user_id=uid, cap_usd=args.approve_usd, reason=f"quality A/B {args.run_id} {args.arm}"))
            await db.execute(text("update users set credit_balance = greatest(credit_balance, 1000) where id = :u"),
                             {"u": uid})  # fmt: skip
        for case in cases:
            out = out_root / case["id"]
            out.mkdir()
            rec = await run_case(h, H, case, out, args.run_id, args.arm)
            rec["mode"] = "SIMULATED" if args.simulated else "LIVE"
            (out / "run.json").write_text(json.dumps(rec, indent=1, default=str))
            results.append(rec)
            print(f"{case['id']}: {rec.get('status') or 'error'}  repairs={rec.get('repairs')}  "
                  f"usd={(rec.get('ledger') or {}).get('total_usd')}  {rec.get('error') or ''}")
    summary = {"run_id": args.run_id, "arm": args.arm, "mode": "SIMULATED" if args.simulated else "LIVE",
               "commit": args.commit, "cases": results,
               "total_usd": str(sum((Decimal((r.get("ledger") or {}).get("total_usd") or 0) for r in results), Decimal(0)))}  # fmt: skip
    (out_root / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(f"arm {args.arm}: {len(results)} case(s), USD {summary['total_usd']} counted -> {out_root}")
    return 0


def blind(args: argparse.Namespace) -> int:
    """Side-by-side page sheets per case with the arms in random order; the key is written apart."""
    from PIL import Image, ImageDraw

    a, b = args.arms.split(",")
    root = RUNS / args.run_id
    out = root / "blind"
    out.mkdir(exist_ok=True)
    rng = random.Random(args.seed if args.seed is not None else uuid.uuid4().int)
    key: dict[str, dict[str, str]] = {}
    rows = ["case,criterion,A,B,notes"]
    for case in CASES["cases"]:
        pages: dict[str, list[Path]] = {}
        for arm in (a, b):
            pages[arm] = sorted((root / arm / case["id"] / "dossier" / "pages").glob("page-*.png"))
        if not pages[a] and not pages[b]:
            continue
        order = [a, b]
        rng.shuffle(order)
        key[case["id"]] = {"A": order[0], "B": order[1]}
        imgs = []
        for label, arm in zip("AB", order, strict=True):
            col = [Image.open(p).convert("RGB") for p in pages[arm]] or [Image.new("RGB", (1000, 400), "white")]
            w = max(i.width for i in col)
            sheet = Image.new("RGB", (w, sum(i.height for i in col) + 60), "white")
            ImageDraw.Draw(sheet).text((10, 10), f"{case['id']}  arm {label}" + ("" if pages[arm] else "  (no pages: see run.json)"),
                                       fill=(0, 0, 0))  # fmt: skip
            y = 60
            for i in col:
                sheet.paste(i, (0, y))
                y += i.height
            imgs.append(sheet)
        both = Image.new("RGB", (sum(i.width for i in imgs) + 40, max(i.height for i in imgs)), "white")
        both.paste(imgs[0], (0, 0))
        both.paste(imgs[1], (imgs[0].width + 40, 0))
        both.save(out / f"{case['id']}.png")
        rows += [f"{case['id']},{c},,," for c in ("story_clarity", "payoff", "identity", "prop_continuity",
                                                    "composition", "lettering", "overall")]  # fmt: skip
    (out / "review-form.csv").write_text("\n".join(rows) + "\n")
    (root / "key.json").write_text(json.dumps(key, indent=1))
    print(f"{len(key)} review sheet(s) in {out}; the arm key is in {root / 'key.json'} (open it after scoring)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--cases", default="")
    r = sub.add_parser("run")
    r.add_argument("--run-id", required=True)
    r.add_argument("--arm", required=True)
    r.add_argument("--cases", default="")
    r.add_argument("--approve-usd", type=Decimal)
    r.add_argument("--simulated", action="store_true")
    r.add_argument("--commit", required=True, help="the full commit SHA the running stack was started from")
    b = sub.add_parser("blind")
    b.add_argument("--run-id", required=True)
    b.add_argument("--arms", required=True)
    b.add_argument("--seed", type=int)
    a = ap.parse_args()
    if a.cmd == "plan":
        ids = a.cases.split(",") if a.cases else [c["id"] for c in CASES["cases"]]
        print(json.dumps(estimate(ids), indent=1))
        return 0
    if a.cmd == "blind":
        return blind(a)
    return asyncio.run(run(a))


if __name__ == "__main__":
    sys.exit(main())
