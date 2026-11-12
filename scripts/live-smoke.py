#!/usr/bin/env python3
"""Bounded smoke test: one 3-panel strip, end to end, through the running API and worker.

    live-smoke.py preflight                       free checks only; no paid calls
    live-smoke.py estimate                        upper-bound spend from the recorded call profile
    live-smoke.py run --simulated                 same flow on fake providers; records the call profile
    live-smoke.py run --approve-usd X --run-id ID the paid run (live providers only)

The flow: create a comic from a fixed 3-panel prompt (text generation + moderation), approve the
cast (character reference sheets + identity embeddings), approve the script (reference-conditioned
panel generation, safety and identity QA, vector lettering), one instruction edit on panel 1,
then PDF, PNG and CBZ exports (export-time upscaling), downloaded and validated. Usage and cost
are read back per stage from `provider_attempts`: reserved estimate, calculated cost, unresolved
potential charges and provider-confirmed charges, kept separate.

Spending safety (paid run):
- refuses unless SP_PROVIDER_MODE=live and --approve-usd >= the computed upper bound;
- the approved amount is persisted as a lifetime SpendCap on the smoke user (same serialised
  reservation as every ceiling; survives restarts, re-use the same --run-id). Every paid call
  reserves its estimate before dispatch, so the run stops (spend_limit_reached) instead of
  exceeding it. Text estimates are heuristics, so a call's settled cost can exceed its estimate;
  the report shows any such overrun.

Environment: the API at SP_SMOKE_API (default http://127.0.0.1:8000) with SP_AUTH_MODE=dev in a
non-deployed SP_ENV, and the same SP_DATABASE_URL as the API (to set the cap and read usage).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import uuid
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/backend/src"))

import httpx  # noqa: E402

PROMPT = ("A lighthouse keeper and a cheeky seagull argue over the last sandwich on a windy pier, "
          "as a 3 panel strip")  # fmt: skip
EDIT = "Make the seagull hold the sandwich in its beak."
PROFILE = ROOT / "docs/evidence/smoke-call-profile.json"
API = os.environ.get("SP_SMOKE_API", "http://127.0.0.1:8000")
WAIT_SECONDS_UPSCALE = 150.0  # metering.upscale_image default wait budget

# simulated model -> the live setting that replaces it
LIVE_MODEL_FOR = {"text_story": "text_model_story", "text_light": "text_model_light", "text_vision": "vision_model",
                  "image_generate": "image_model_generate", "image_edit": "image_model_edit",
                  "image_inpaint": "inpaint_model", "upscale": "upscale_model", "embed": "embedding_model"}  # fmt: skip
PROVIDER_FOR = {"text": "anthropic", "image": "fal", "upscale": "fal", "embed": "voyage"}


def _provider(op: str) -> str:
    return PROVIDER_FOR[op.split("_")[0]]


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ------------------------------------------------------------------ preflight (free)
async def preflight() -> int:
    from storypanel.config import get_settings
    from storypanel.providers import pricing

    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

    print("preflight: configuration and contracts (no paid calls)")
    try:
        s = get_settings()
    except Exception as exc:  # the startup validator lists every problem
        check("settings validate for live providers", False, str(exc)[:600])
        return 1
    check("provider mode is live", s.provider_mode == "live", s.provider_mode)
    check("not a deployed environment (smoke uses dev sign-in)", not s.is_deployed, s.env)
    for key in ("anthropic_api_key", "fal_key", "voyage_api_key"):
        check(f"SP_{key.upper()} present", getattr(s, key) is not None)
    problems = pricing.startup_problems(s)
    check("pricing table: every configured model priced, verified within "
          f"{s.pricing_max_age_days} days", not problems, "; ".join(problems)[:600])  # fmt: skip
    # Anthropic model capabilities via the Models API (free).
    if s.anthropic_api_key is not None:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
            for mid in sorted({m for m in (s.text_model_story, s.text_model_light, s.vision_model) if m}):
                m = await client.models.retrieve(mid)
                caps = getattr(m, "capabilities", None)
                check(f"Anthropic model {mid} exists", m.id == mid, f"max_input={getattr(m, 'max_input_tokens', '?')}")
                if caps is not None:
                    print(f"         capabilities: {json.dumps(caps if isinstance(caps, dict) else caps.to_dict())[:300]}")
        except Exception as exc:
            check("Anthropic Models API reachable with the key", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    # Object storage round trip (put/get/delete one tiny object).
    try:
        from storypanel import storage

        key = f"smoke/preflight-{uuid.uuid4().hex}.txt"
        await storage.put_bytes(key, b"ok", "text/plain")
        got = await storage.get_bytes(key)
        res = await storage.delete_keys([key])
        check("object storage put/get/delete", got == b"ok" and res.ok, f"versioned={await storage.bucket_versioned()}")
    except Exception as exc:
        check("object storage put/get/delete", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    # The API the run will drive.
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"{API}/readyz")
            check("API ready", r.status_code == 200, r.text[:200])
            meta = (await h.get(f"{API}/v1/meta")).json()
            check("API runs live providers", meta.get("provider_mode") == "live", meta.get("provider_mode", "?"))
    except Exception as exc:
        check("API reachable", False, f"{API}: {type(exc).__name__}")
    print("preflight:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ------------------------------------------------------------------ estimate
def estimate(verbose: bool = True) -> Decimal:
    """Upper bound from the recorded call profile, priced with the live table:
    - text: 50k input tokens + the full output budget per call (more than any prompt here uses);
    - images: the price for the configured 1K output; per-megapixel units at 1.1 MP;
    - upscaling billed per compute second: rate x the full wait budget;
    - every QA-repeatable panel stage x (1 + max_panel_qa_retries), the script stage x (1 +
      max_script_repairs), character stages scaled to max_characters."""
    from storypanel.config import get_settings
    from storypanel.providers import pricing

    # Works before any key exists: models and prices come from the environment / pricing file
    # (config/live-smoke.env), not from a validated live Settings object.
    base = get_settings()
    env = {**_env_file(ROOT / "config/live-smoke.env"), **{k: v for k, v in os.environ.items() if k.startswith("SP_")}}

    class _View:
        def __getattr__(self, name: str) -> Any:
            return env.get(f"SP_{name.upper()}") or getattr(base, name)

    s = _View()
    prof = json.loads(PROFILE.read_text())
    table = pricing.parse_table(json.loads((ROOT / env["SP_PRICING_FILE"]).read_text()))
    chars = max(1, prof["characters"])
    total = Decimal(0)
    rows = []
    for (stage, op), n in sorted(((tuple(k.split("|")), v) for k, v in prof["calls"].items())):
        live_model = getattr(s, LIVE_MODEL_FOR[op])
        if not live_model:
            continue
        price = table.get((_provider(op), live_model))
        if price is None:
            raise SystemExit(f"no price for {_provider(op)}/{live_model}; run preflight")
        if op.startswith("text"):
            unit = pricing.text_cost(price, 50_000, 16_000)
        elif price.unit == "per_compute_second":
            unit = pricing.image_cost(price, seconds=WAIT_SECONDS_UPSCALE)
        else:
            unit = pricing.image_cost(price, pixels=1_100_000 * (4 if op == "embed" else 1))
        mult = 1
        s_retries, s_repairs, s_chars = int(s.max_panel_qa_retries), int(s.max_script_repairs), int(s.max_characters)
        if stage in ("panel_generate", "image_safety", "vision_qa", "identity_embedding", "panel_edit"):
            mult = 1 + s_retries
        elif stage.startswith("script"):
            mult = 1 + s_repairs
        elif stage.startswith("character"):
            mult = max(1, -(-s_chars // chars))  # ceil(max_characters / simulated characters)
        cost = unit * n * mult
        total += cost
        rows.append((stage, op, live_model, n, mult, unit, cost))
    if verbose:
        print(f"upper-bound estimate for one 3-panel strip + 1 edit + PDF/PNG/CBZ "
              f"(profile recorded {prof['recorded_at']}, {prof['characters']} characters, {prof['panels']} panels)")  # fmt: skip
        print(f"  {'stage':<20} {'operation':<15} {'model':<42} calls x retry  unit$     total$")
        for stage, op, m, n, mult, unit, cost in rows:
            print(f"  {stage:<20} {op:<15} {m:<42} {n:>4} x {mult:<4} {unit:>8.4f} {cost:>9.4f}")
        print(f"  UPPER BOUND: USD {total:.2f}  (approve at least this; the cap stops the run at the approved amount)")
    return total


# ------------------------------------------------------------------ the run
async def run(simulated: bool, approve: Decimal | None, run_id: str) -> int:
    from sqlalchemy import select, text

    from storypanel.config import get_settings
    from storypanel.db.models import SpendCap, User
    from storypanel.db.session import transaction

    s = get_settings()
    if not simulated:
        if s.provider_mode != "live":
            print("refusing: SP_PROVIDER_MODE is not live", file=sys.stderr)
            return 2
        bound = estimate(verbose=False)
        if approve is None or approve < bound:
            print(f"refusing: --approve-usd {approve} is below the upper bound USD {bound:.2f}", file=sys.stderr)
            return 2
    email = f"smoke-{run_id}@example.com"
    started = time.monotonic()
    timings: dict[str, float] = {}
    out_dir = ROOT / "docs/evidence/smoke" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60) as h:
        tok = (await h.post(f"{API}/v1/dev/login", json={"email": email})).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        me = (await h.get(f"{API}/v1/me", headers=H)).json()
        uid = uuid.UUID(me["id"])
        if not simulated:
            assert approve is not None
            async with transaction() as db:
                cap = (await db.execute(select(SpendCap).where(SpendCap.user_id == uid))).scalar_one_or_none()
                if cap is None:
                    db.add(SpendCap(user_id=uid, cap_usd=approve, reason=f"live smoke {run_id}"))
                elif Decimal(cap.cap_usd) != approve:
                    print(f"refusing: run {run_id} already has cap {cap.cap_usd}", file=sys.stderr)
                    return 2
                await db.execute(text("update users set credit_balance = greatest(credit_balance, 500) where id = :u"),
                                 {"u": uid})  # fmt: skip

        async def until(pid: str, ok: Any, what: str, limit: float = 1800) -> dict[str, Any]:
            t0 = time.monotonic()
            while time.monotonic() - t0 < limit:
                p = (await h.get(f"{API}/v1/projects/{pid}", headers=H)).json()
                if ok(p):
                    timings[what] = round(time.monotonic() - t0, 1)
                    return p  # type: ignore[no-any-return]
                if p["status"] in ("failed", "blocked", "cancelled", "expired"):
                    raise RuntimeError(f"{what}: comic ended {p['status']}: {p.get('failure')}")
                await asyncio.sleep(2)
            raise RuntimeError(f"{what}: timed out")

        p = (await h.post(f"{API}/v1/projects", headers={**H, "Idempotency-Key": f"smoke-{run_id}"},
                          json={"prompt": PROMPT})).json()  # fmt: skip
        pid = p["id"]
        result: dict[str, Any] = {"run_id": run_id, "mode": "SIMULATED" if simulated else "LIVE", "project_id": pid,
                                  "prompt": PROMPT, "approved_usd": str(approve) if approve else None}  # fmt: skip
        try:
            p = await until(pid, lambda p: p["status"] in ("awaiting_cast_approval", "awaiting_questions"), "story+cast")
            if p["status"] == "awaiting_questions":
                await h.post(f"{API}/v1/projects/{pid}/answers", headers=H, json={"answers": {}})
                p = await until(pid, lambda p: p["status"] == "awaiting_cast_approval", "cast")
            result["characters"] = len(p.get("characters") or [])
            await h.post(f"{API}/v1/projects/{pid}/characters/approve", headers=H, json={"cast_version": p["cast_version"]})
            p = await until(pid, lambda p: p["status"] == "awaiting_script_approval", "script")
            await h.post(f"{API}/v1/projects/{pid}/script/approve", headers=H, json={"script_version": p["script_version"]})
            p = await until(pid, lambda p: p["status"] in ("completed", "partially_completed")
                            and all(pg["preview_url"] for pg in p["pages"]), "panels")  # fmt: skip
            panels = [pn for pg in p["pages"] for pn in pg["panels"]]
            result["panels"] = len(panels)
            result["panel_status"] = [pn["status"] for pn in panels]
            first = panels[0]
            before = first.get("current_version_id")
            r = await h.post(f"{API}/v1/panels/{first['id']}/edit", headers={**H, "Idempotency-Key": f"edit-{run_id}"},
                             json={"instruction": EDIT})  # fmt: skip
            result["edit_request"] = r.status_code
            await until(pid, lambda p: any(pn["id"] == first["id"] and pn.get("current_version_id") != before
                                           and not pn.get("busy") for pg in p["pages"] for pn in pg["panels"]), "edit")  # fmt: skip
            files: dict[str, Any] = {}
            for fmt in ("pdf", "png", "cbz"):
                e = (await h.post(f"{API}/v1/projects/{pid}/exports", headers={**H, "Idempotency-Key": f"{fmt}-{run_id}"},
                                  json={"format": fmt})).json()  # fmt: skip
                q = await until(pid, lambda q, i=e["id"]: any(x["id"] == i and x["status"] in ("ready", "failed")
                                                               for x in q["exports"]), f"export_{fmt}")  # fmt: skip
                ex = next(x for x in q["exports"] if x["id"] == e["id"])
                if ex["status"] != "ready":
                    files[fmt] = {"status": ex["status"], "error": ex.get("error")}
                    continue
                data = (await h.get(ex["download_url"])).content
                (out_dir / f"strip.{fmt if fmt != 'png' or data[:4] == b'\x89PNG' else 'zip'}").write_bytes(data)
                files[fmt] = _validate(fmt, data)
            result["exports"] = files
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
    result["timings_s"] = {**timings, "total": round(time.monotonic() - started, 1)}
    result["usage"] = await _usage(uuid.UUID(pid), uid)
    if simulated:
        _record_profile(result)
    path = out_dir / "result.json"
    path.write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: result.get(k) for k in ("mode", "panels", "panel_status", "exports", "timings_s", "error")},
                     indent=1, default=str))  # fmt: skip
    print(json.dumps(result["usage"]["totals"], indent=1))
    print(f"evidence: {path}")
    return 0 if "error" not in result and all(v.get("ok") for v in result.get("exports", {}).values()) else 1


def _validate(fmt: str, data: bytes) -> dict[str, Any]:
    if fmt == "pdf":
        from pypdf import PdfReader

        page = PdfReader(io.BytesIO(data)).pages[0]
        text = page.extract_text()
        fonts = sorted({str(f.get_object()["/BaseFont"]).split("+")[-1] for f in page["/Resources"]["/Font"].values()})
        return {"ok": bool(text.strip()) and any("ComicNeue" in f for f in fonts), "bytes": len(data),
                "selectable_text_chars": len(text), "fonts": fonts,
                "trim_box": [float(v) for v in page.trimbox], "bleed_box": [float(v) for v in page.bleedbox]}  # fmt: skip
    if fmt == "cbz":
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        return {"ok": "ComicInfo.xml" in names and any(n.endswith(".png") for n in names), "entries": names}
    from PIL import Image

    if data[:4] == b"\x89PNG":
        im = Image.open(io.BytesIO(data))
        return {"ok": im.width >= 1000, "size": im.size}
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    return {"ok": bool(names), "entries": names}


async def _usage(pid: uuid.UUID, uid: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    async with transaction() as db:
        rows = (await db.execute(text("""
            select stage, operation, model, status, cost_basis, cost_estimate_usd, cost_counted_usd,
                   cost_calculated_usd, cost_confirmed_usd, input_tokens, output_tokens, images, latency_ms
            from provider_attempts where user_id = :u order by created_at"""), {"u": uid})).mappings().all()  # fmt: skip
    by: dict[str, dict[str, Any]] = {}
    tot = {k: Decimal(0) for k in ("reserved_estimate", "calculated", "unresolved", "provider_confirmed", "counted")}
    overruns = []
    for r in rows:
        b = by.setdefault(f"{r['stage']}|{r['operation']}", {"calls": 0, "model": r["model"], "counted": Decimal(0),
                                                             "statuses": {}})  # fmt: skip
        b["calls"] += 1
        b["counted"] += r["cost_counted_usd"]
        b["statuses"][r["status"]] = b["statuses"].get(r["status"], 0) + 1
        tot["reserved_estimate"] += r["cost_estimate_usd"]
        tot["counted"] += r["cost_counted_usd"]
        if r["cost_basis"] == "calculated":
            tot["calculated"] += r["cost_calculated_usd"] or 0
            if (r["cost_calculated_usd"] or 0) > r["cost_estimate_usd"]:
                overruns.append({"stage": r["stage"], "estimate": r["cost_estimate_usd"], "calculated": r["cost_calculated_usd"]})
        elif r["cost_basis"] in ("unresolved", "reserved"):
            tot["unresolved"] += r["cost_counted_usd"]
        elif r["cost_basis"] == "provider_confirmed":
            tot["provider_confirmed"] += r["cost_confirmed_usd"] or 0
    return {"by_stage": by, "totals": {k: str(v) for k, v in tot.items()}, "estimate_overruns": overruns,
            "note": "calculated = usage x operator price table (not a provider charge); unresolved = potential charges "
                    "counted at their estimate (e.g. compute-time upscaling) until a provider charge is imported"}  # fmt: skip


def _record_profile(result: dict[str, Any]) -> None:
    calls = {k: v["calls"] for k, v in result["usage"]["by_stage"].items()}
    PROFILE.write_text(json.dumps({
        "recorded_at": datetime.now(UTC).date().isoformat(), "source": "live-smoke.py run --simulated",
        "characters": result.get("characters", 0), "panels": result.get("panels", 0), "calls": calls}, indent=1))
    print(f"call profile recorded: {PROFILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    sub.add_parser("estimate")
    r = sub.add_parser("run")
    r.add_argument("--simulated", action="store_true")
    r.add_argument("--approve-usd", type=Decimal)
    r.add_argument("--run-id", default=None)
    a = ap.parse_args()
    if a.cmd == "preflight":
        return asyncio.run(preflight())
    if a.cmd == "estimate":
        estimate()
        return 0
    return asyncio.run(run(a.simulated, a.approve_usd, a.run_id or uuid.uuid4().hex[:8]))


if __name__ == "__main__":
    sys.exit(main())
