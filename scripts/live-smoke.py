#!/usr/bin/env python3
"""Bounded smoke test: one 3-panel strip, end to end, through the running API and worker.

    live-smoke.py preflight                       free checks only; no paid calls
    live-smoke.py estimate                        itemised cost model (expected, minimum cap, worst-case holds)
    live-smoke.py run --simulated                 same flow on fake providers; records the call profile
    live-smoke.py run --approve-usd X --run-id ID the paid run (live providers only)

The flow: create a comic from a fixed 3-panel prompt (text generation + moderation), approve the
cast (character reference sheets + identity embeddings), approve the script (reference-conditioned
panel generation, safety and identity QA, vector lettering), one instruction edit on panel 1,
then PDF, PNG and CBZ exports (export-time upscaling), downloaded and validated. Usage and cost
are read back per stage from `provider_attempts`: reserved estimate, calculated cost, unresolved
potential charges and provider-confirmed charges, kept separate.

Spending safety (paid run):
- refuses unless SP_PROVIDER_MODE=live and --approve-usd >= the smallest cap with which a clean run
  can finish (see `estimate`); the approved amount is an application admission limit, not a
  provider-enforced ceiling (docs/smoke-test-cost.md lists what can be billed beyond it);
- the approved amount is persisted as a lifetime SpendCap on the smoke user (same serialised
  reservation as every ceiling; survives restarts, re-use the same --run-id). Every paid call
  reserves its estimate before dispatch, so the run stops (spend_limit_reached) instead of
  reserving past it. Text reservations hold the full output cap plus an input bound; the report
  shows any call whose settled cost exceeded its reservation.

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
PANELS_REQUESTED = 3
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
# Hosts the live run talks to, and how. `secret` = the provider key travels in this header (set as a
# network secret in a cloud environment); hosts without one must still be reachable.
HOSTS = [
    ("api.anthropic.com", "x-api-key", "text, moderation, vision QA; Models API"),
    ("queue.fal.run", "Authorization: Key", "image generation, edit, upscale jobs"),
    ("rest.fal.ai", "Authorization: Key", "short-lived upload token for reference images"),
    ("api.fal.ai", "Authorization: Key", "fal pricing API (free; checks the price table)"),
    ("v3.fal.media", None, "reference uploads (CDN token) and result downloads"),
    ("api.voyageai.com", "Authorization: Bearer", "identity embeddings"),
]
FAL_UNITS = {"per_image": ("image", "images"), "per_compute_second": ("second", "seconds", "compute second",
                                                                     "compute seconds")}  # fmt: skip


async def preflight() -> int:
    """Free checks only: nothing here is billed. Credential-free checks always run."""
    from storypanel.config import Settings, get_settings
    from storypanel.providers import pricing

    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

    print("preflight: configuration and contracts (no paid calls)")
    # 1. Network: an unauthenticated request per host. Any HTTP answer means reachable; a refused
    #    proxy tunnel or a failed DNS lookup means the environment's network does not allow it.
    async with httpx.AsyncClient(timeout=15) as h:
        for host, _, why in HOSTS:
            try:
                r = await h.get(f"https://{host}/")
                check(f"network: {host} reachable", True, f"HTTP {r.status_code} unauthenticated ({why})")
            except Exception as exc:
                check(f"network: {host} reachable", False, f"{type(exc).__name__}: blocked or unresolved ({why})")
    # 2. The price table itself (no keys needed).
    env = {**_env_file(ROOT / "config/live-smoke.env"), **{k: v for k, v in os.environ.items() if k.startswith("SP_")}}
    try:
        table = pricing.parse_table(json.loads((ROOT / env["SP_PRICING_FILE"]).read_text()))
        models = [(p, env.get(f"SP_{k.upper()}")) for p, k in (
            ("anthropic", "text_model_story"), ("anthropic", "text_model_light"), ("anthropic", "vision_model"),
            ("fal", "image_model_generate"), ("fal", "image_model_edit"), ("fal", "upscale_model"),
            ("voyage", "embedding_model"))]  # fmt: skip
        missing = [f"{p}/{m}" for p, m in models if m and (p, m) not in table]
        check("price table covers every configured model", not missing, ", ".join(missing))
    except Exception as exc:
        table = {}
        check("price table parses", False, _safe_err(exc))
    # 3. Object storage and the API the run will drive.
    await _storage_and_api(check)
    # 4. Settings as the live run will load them (needs the keys).
    try:
        s: Settings = get_settings()
    except Exception as exc:  # the startup validator lists every problem
        # messages only: pydantic's str() also echoes the raw input, which may contain key values
        check("settings validate for live providers", False, _safe_err(exc))
        print("preflight: FAIL (checks that need the provider keys were skipped)")
        return 1
    check("provider mode is live", s.provider_mode == "live", s.provider_mode)
    check("not a deployed environment (smoke uses dev sign-in)", not s.is_deployed, s.env)
    for key in ("anthropic_api_key", "fal_key", "voyage_api_key"):
        check(f"SP_{key.upper()} present", getattr(s, key) is not None)
    problems = pricing.startup_problems(s)
    check("pricing table: every configured model priced, verified within "
          f"{s.pricing_max_age_days} days", not problems, "; ".join(problems)[:600])  # fmt: skip
    # 5. Anthropic Models API (free): the key works and the configured models exist.
    if s.anthropic_api_key is not None:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
            for mid in sorted({m for m in (s.text_model_story, s.text_model_light, s.vision_model) if m}):
                m = await client.models.retrieve(mid)
                check(f"Anthropic model {mid} exists", m.id == mid, f"max_input={getattr(m, 'max_input_tokens', '?')}")
        except Exception as exc:
            check("Anthropic Models API with the key", False, _safe_err(exc))
    # 6. fal pricing API (free, authenticated): official unit and price for every fal model we call.
    if s.fal_key is not None:
        fal_models = [m for m in (s.image_model_generate, s.image_model_edit, s.upscale_model) if m]
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.get("https://api.fal.ai/v1/models/pricing", params=[("endpoint_id", m) for m in fal_models],
                                headers={"Authorization": f"Key {s.fal_key.get_secret_value()}"})  # fmt: skip
            check("fal pricing API with the key", r.status_code == 200, f"HTTP {r.status_code}")
            official = {p["endpoint_id"]: p for p in (r.json().get("prices") or [])} if r.status_code == 200 else {}
            for m in fal_models:
                ours, theirs = table.get(("fal", m)), official.get(m)
                if theirs is None or ours is None:
                    check(f"fal price for {m}", False, "not returned by the pricing API" if theirs is None else "not in table")
                    continue
                unit_ok = str(theirs.get("unit", "")).lower() in FAL_UNITS.get(ours.unit, ())
                price_ok = Decimal(str(theirs.get("unit_price"))) == ours.usd and theirs.get("currency", "USD") == "USD"
                check(f"fal price for {m} matches the table", unit_ok and price_ok,
                      f"official {theirs.get('unit_price')} {theirs.get('currency')}/{theirs.get('unit')}; table {ours.usd} {ours.unit}")
        except Exception as exc:
            check("fal pricing API with the key", False, _safe_err(exc))
    # Voyage has no free authenticated endpoint: the key is first used by the paid run (free allowance).
    print("  [INFO] Voyage key validity cannot be checked without an embedding call; it is checked by the run")
    print("preflight:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _safe_err(exc: Exception) -> str:
    """Error text without echoed input: pydantic's str() repeats the raw settings, which may hold keys."""
    errs = getattr(exc, "errors", None)
    if callable(errs):
        return "; ".join(str(e.get("msg", "")) for e in errs())[:300]
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"


async def _storage_and_api(check: Any) -> None:
    try:
        from storypanel.config import get_settings

        get_settings()
    except Exception:
        print("  [SKIP] object storage put/get/delete: needs valid live settings (the provider keys)")
    else:
        await _storage(check)
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"{API}/readyz")
            check("API ready", r.status_code == 200, r.text[:200])
            meta = (await h.get(f"{API}/v1/meta")).json()
            check("API runs live providers", meta.get("provider_mode") == "live", meta.get("provider_mode", "?"))
    except Exception as exc:
        check("API reachable", False, f"{API}: {type(exc).__name__}")


async def _storage(check: Any) -> None:
    try:
        from storypanel import storage

        key = f"smoke/preflight-{uuid.uuid4().hex}.txt"
        await storage.put_bytes(key, b"ok", "text/plain")
        got = await storage.get_bytes(key)
        res = await storage.delete_keys([key])
        check("object storage put/get/delete", got == b"ok" and res.ok, f"versioned={await storage.bucket_versioned()}")
    except Exception as exc:
        check("object storage put/get/delete", False, _safe_err(exc))


# ------------------------------------------------------------------ estimate
# Expected output tokens per text call: an ASSUMPTION for the expected-cost column only (reservations
# always hold the full max_tokens). Replaced by measured usage after the first live run.
EXPECTED_OUTPUT = {"intake": 600, "moderation": 250, "bible": 5000, "script": 7000, "image_safety": 250,
                   "vision_qa": 900}  # fmt: skip
LIVE_IMAGE_TOKENS_EXPECTED = 1369  # a 1K (1024x1024) image: ceil(1024/28)^2, official vision rule
UPSCALE_SECONDS_EXPECTED = 10  # ASSUMPTION: no published ESRGAN run time; the reservation holds the full budget
EMBED_PIXELS = {"character_sheet": 5 * 1_100_000, "identity_embedding": 3 * 400_000}  # live upper sizes


def _expected_out(stage: str) -> int:
    return next((v for k, v in EXPECTED_OUTPUT.items() if stage.startswith(k)), 600)


def cost_model() -> dict[str, Any]:
    """Every potentially billable call of the smoke test, from the code's own limits.

    nominal  = the happy path once (no QA failure, no retry, no clarifying question);
    maximum  = every bounded loop and every Temporal activity attempt used, every failure
               counted as billed (SP_COUNT_FAILED_REQUESTS_AS_BILLED=true). Reservations are per call.
    """
    from storypanel.config import Settings
    from storypanel.pipeline.story import MAX_LOCATIONS
    from storypanel.providers import pricing
    from storypanel.workflows.definitions import IMAGE, TEXT

    env = {**_env_file(ROOT / "config/live-smoke.env"), **{k: v for k, v in os.environ.items() if k.startswith("SP_")}}
    base = Settings(provider_mode="fake", _env_file=None)  # type: ignore[call-arg]

    def setting(name: str) -> Any:
        return env.get(f"SP_{name.upper()}") or getattr(base, name)

    table = pricing.parse_table(json.loads((ROOT / env["SP_PRICING_FILE"]).read_text()))
    prof = json.loads(PROFILE.read_text())
    sizes = prof["sizes"]
    TA, IA = TEXT["retry_policy"].maximum_attempts, IMAGE["retry_policy"].maximum_attempts
    QA = 1 + int(setting("max_panel_qa_retries"))
    SR = 1 + int(setting("max_script_repairs"))
    C, L, P, E, X = int(setting("max_characters")), MAX_LOCATIONS, PANELS_REQUESTED, 1, 3
    L_NOM = min(L, prof["calls"].get("location_ref|image_generate", 1))
    out_cap = 16000  # AnthropicTextProvider.max_tokens: provider-enforced output ceiling, thinking included
    fake_in = {"text_story": Decimal(2), "text_light": Decimal("0.1"), "text_vision": Decimal("0.1")}
    fake_out = {"text_story": Decimal(10), "text_light": Decimal("0.5"), "text_vision": Decimal("0.5")}

    def price(op: str) -> pricing.Price:
        return table[(_provider(op), setting(LIVE_MODEL_FOR[op]))]

    def text_row(stage: str, op: str, images: int, nominal: float, maximum: int, why: str) -> dict[str, Any]:
        pr = price(op)
        sz = sizes[f"{stage.split(' ')[0]}|{op}"]
        # recover the simulated input bound from the simulated reservation (same token formula)
        est = Decimal(sz["max_estimate"])
        in_bound = int((est * 1_000_000 - out_cap * fake_out[op]) / fake_in[op])
        in_bound += images * pricing.IMAGE_TOKEN_CAP  # live images at the documented per-image cap
        reserve = pricing.text_cost(pr, in_bound, out_cap)
        exp_in = sz["max_input_tokens"] + images * LIVE_IMAGE_TOKENS_EXPECTED + 1000
        expected = pricing.text_cost(pr, exp_in, _expected_out(stage))
        return {"stage": stage, "model": pr.model, "unit": f"per MTok in ${pr.usd} / out ${pr.output_usd}",
                "quantity": f"<= {in_bound:,} in + <= {out_cap:,} out tokens", "reserve": reserve,
                "expected": expected, "nominal": nominal, "maximum": maximum, "why": why, "source": pr.source}

    def image_row(stage: str, op: str, nominal: float, maximum: int, why: str) -> dict[str, Any]:
        pr = price(op)
        cost = pricing.image_cost(pr)
        return {"stage": stage, "model": pr.model, "unit": f"per image ${pr.usd} (1K, 1 image)", "quantity": "1 image",
                "reserve": cost, "expected": cost, "nominal": nominal, "maximum": maximum, "why": why,
                "source": pr.source}

    def embed_row(stage: str, nominal: float, maximum: int, why: str) -> dict[str, Any]:
        pr = price("embed")
        cost = pricing.image_cost(pr, pixels=EMBED_PIXELS[stage])
        return {"stage": stage, "model": pr.model, "unit": f"per 1B pixels ${pr.usd}",
                "quantity": f"<= {EMBED_PIXELS[stage]:,} px", "reserve": cost, "expected": cost,
                "nominal": nominal, "maximum": maximum, "why": why, "source": pr.source}

    up = price("upscale")
    rows = [
        text_row("intake", "text_light", 0, 1, 2 * TA, "brief; x2 if a clarifying question is answered; x TEXT attempts"),
        text_row("moderation.prompt", "text_light", 0, 1, 2 * TA, "same activity as intake"),
        text_row("bible", "text_story", 0, 1, TA, "Story Bible + cast"),
        text_row("moderation.bible", "text_light", 0, 1, TA, "same activity as the bible"),
        image_row("character_sheet portrait", "image_generate", C, C * IA, f"{C} characters x IMAGE attempts"),
        image_row("character_sheet turnaround+expressions", "image_edit", 2 * C, 2 * C * IA, "2 per character"),
        text_row("image_safety (sheets, locations)", "text_vision", 1, 3 * C + L_NOM, (3 * C + L) * IA,
                 f"every reference image; locations nominal {L_NOM}, max {L}"),
        embed_row("character_sheet", C, C * IA, "portrait + 4 verified crops per character"),
        image_row("location_ref", "image_generate", L_NOM, L * IA, f"locations: nominal {L_NOM}, code cap {L}"),
        text_row("script", "text_story", 0, 1, SR * TA, f"draft + {SR - 1} repairs, x TEXT attempts"),
        text_row("moderation.script", "text_light", 0, 1, TA, "same activity as the script"),
        image_row("panel_generate", "image_edit", P, P * QA * IA, f"{P} panels x {QA} QA rounds x IMAGE attempts"),
        text_row("image_safety (panels)", "text_vision", 1, P, P * QA * IA, "QA safety per candidate"),
        text_row("vision_qa", "text_vision", 4, P, P * QA * IA, "panel + up to 3 character references"),
        embed_row("identity_embedding", P, P * QA * IA, "character crops per candidate"),
        text_row("moderation.panel_edit", "text_light", 0, E, E * QA * IA, "edit instruction, each edit attempt"),
        image_row("panel edit", "image_edit", E, E * QA * IA, "instruction edit x QA rounds x IMAGE attempts"),
        text_row("image_safety (edit)", "text_vision", 1, E, E * QA * IA, ""),
        text_row("vision_qa (edit)", "text_vision", 4, E, E * QA * IA, ""),
        embed_row("identity_embedding", E, E * QA * IA, "edit candidates"),
        {"stage": "export_upscale", "model": up.model, "unit": f"per compute second ${up.usd}",
         "quantity": f"<= {WAIT_SECONDS_UPSCALE:.0f} s reserved, then cancelled", "reserve": up.usd * Decimal(str(WAIT_SECONDS_UPSCALE)),
         "expected": up.usd * UPSCALE_SECONDS_EXPECTED, "nominal": P, "maximum": P * X * IA,
         "why": f"one per final panel version, shared by the {X} exports; max {X} exports x IMAGE attempts",
         "source": up.source},
    ]
    for r in rows:
        r["expected_total"] = r["expected"] * Decimal(str(r["nominal"]))
        r["nominal_reserve"] = r["reserve"] * Decimal(str(r["nominal"]))
        r["max_reserve"] = r["reserve"] * r["maximum"]
    tot = {k: sum((r[k] for r in rows), Decimal(0)) for k in ("expected_total", "nominal_reserve", "max_reserve")}
    # ESRGAN stays at its reservation until a provider charge is imported, so the counted total at the end
    # of a clean run is expected text/images + the full upscale reservations.
    tot["counted_at_end"] = tot["expected_total"] - rows[-1]["expected_total"] + rows[-1]["nominal_reserve"]
    # headroom: the largest single reservations that can be open at once (sheets + locations in parallel)
    tot["headroom"] = sum(sorted((r["reserve"] for r in rows), reverse=True)[:4], Decimal(0))
    tot["min_cap_nominal"] = tot["counted_at_end"] + tot["headroom"]
    return {"rows": rows, "totals": tot, "limits": {"TEXT attempts": TA, "IMAGE attempts": IA, "QA rounds": QA,
            "script drafts": SR, "characters": C, "locations": L, "panels": P, "edits": E, "exports": X,
            "refusal fallback": setting("text_refusal_fallback")}, "profile": prof["recorded_at"]}  # fmt: skip


def cap_scenarios(cap: Decimal = Decimal("5.00")) -> list[dict[str, Any]]:
    """Counted spend before exports in progressively worse runs, and whether the exports still fit.

    Exports run last and reserve 3 x the ESRGAN budget; a run whose counted spend plus those
    reservations exceeds the cap stops at export time (the comic exists; the export fails). Settled
    text calls count their usage-based cost (expected sizes); failed calls count their full reservation
    (SP_COUNT_FAILED_REQUESTS_AS_BILLED=true)."""
    m = cost_model()
    r = {row["stage"]: row for row in m["rows"]}
    exp = lambda k: r[k]["expected"]  # noqa: E731
    res = lambda k: r[k]["reserve"]  # noqa: E731
    up = r["export_upscale"]["reserve"] * 3
    clean = m["totals"]["expected_total"] - r["export_upscale"]["expected_total"]
    # one extra QA round for a panel (or the edit): image + safety + vision QA + identity embedding
    qa_round = res("panel_generate") + exp("image_safety (panels)") + exp("vision_qa") + r["identity_embedding"]["reserve"]
    worst_qa = qa_round * 2 * 4  # 3 panels + the edit, each using both repair rounds
    script_repairs = exp("script") * 2  # two more drafts (each re-sends the script)
    question = exp("intake") + exp("moderation.prompt")  # a clarifying question re-runs intake
    # every paid call failing once at the provider and being re-sent: the failure counts its reservation
    calls = {k: row["nominal"] for k, row in r.items() if k != "export_upscale"}
    one_failure_each = sum((res(k) * Decimal(str(n)) for k, n in calls.items()), Decimal(0))
    rows = []
    for name, before in (("clean run", clean),
                         ("every panel and the edit use both repair rounds", clean + worst_qa),
                         ("... + 2 script repairs + a clarifying question", clean + worst_qa + script_repairs + question),
                         ("... + every paid call fails once and is re-sent", clean + worst_qa + script_repairs + question
                          + one_failure_each)):  # fmt: skip
        rows.append({"scenario": name, "counted_before_exports": before, "with_exports": before + up,
                     "fits": before + up <= cap})
    return rows


def cost_markdown() -> str:
    """The itemised table for docs/smoke-test-cost.md (generated; do not edit by hand)."""
    m = cost_model()
    t = m["totals"]
    lines = [f"Generated by `live-smoke.py estimate --markdown` from the code limits {m['limits']}, the price table "
             f"in config/live-smoke.env and the simulated call profile of {m['profile']}.", "",
             "| Stage | Model | Unit price | Per call (reserved) | Reservation / call (USD) | Calls: clean run | "
             "Calls: max | Expected, clean run (USD) | Held if every retry ran (USD) | Why the max |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---|"]  # fmt: skip
    for r in m["rows"]:
        lines.append(f"| {r['stage']} | `{r['model']}` | {r['unit']} | {r['quantity']} | {r['reserve']:.4f} | "
                     f"{r['nominal']} | {r['maximum']} | {r['expected_total']:.4f} | {r['max_reserve']:.4f} | {r['why']} |")
    lines += ["", f"- **Estimated cost of a clean run: USD {t['expected_total']:.2f}** (assumed output sizes)",
              f"- Counted by the app at the end of a clean run: USD {t['counted_at_end']:.2f} "
              "(ESRGAN stays at its full reservation until a provider charge is imported)",
              f"- **Smallest cap with which a clean run can finish: USD {t['min_cap_nominal']:.2f}** "
              f"(counted total + USD {t['headroom']:.2f} for reservations open at the same time)",
              f"- Reservations if every bounded retry, QA round and activity attempt were used: USD {t['max_reserve']:.2f} "
              "(the approved cap stops the run long before this)"]
    lines += ["", "**Will a USD 5.00 cap stop the run before the exports finish?** Exports run last and reserve "
              "3 × USD 0.1665 for upscaling. Counted spend before exports, by scenario:", "",
              "| Scenario | Counted before exports (USD) | + export reservations (USD) | Fits in USD 5.00 |",
              "|---|---:|---:|---|"]
    for c in cap_scenarios():
        lines.append(f"| {c['scenario']} | {c['counted_before_exports']:.2f} | {c['with_exports']:.2f} | "
                     f"{'yes' if c['fits'] else '**no: the export would be refused**'} |")
    return "\n".join(lines) + "\n"


def estimate(verbose: bool = True) -> Decimal:
    """Prints the itemised model; returns the smallest cap that lets a clean run finish."""
    m = cost_model()
    t = m["totals"]
    if verbose:
        print(f"limits from code/config: {m['limits']}")
        print(f"  {'stage':40} {'model':28} {'reserve':>9} {'nom':>4} {'max':>4} {'expected':>9} {'max held':>9}")
        for r in m["rows"]:
            print(f"  {r['stage']:40} {r['model']:28} {r['reserve']:9.4f} {r['nominal']:>4} {r['maximum']:>4} "
                  f"{r['expected_total']:9.4f} {r['max_reserve']:9.4f}")
        print(f"  ESTIMATED cost of a clean run (assumed output sizes):          USD {t['expected_total']:.2f}")
        print(f"  counted by the app at the end of a clean run (ESRGAN unresolved): USD {t['counted_at_end']:.2f}")
        print(f"  smallest cap that lets a clean run finish (+ in-flight holds):  USD {t['min_cap_nominal']:.2f}")
        print(f"  reservations if every retry/QA round/attempt were used:          USD {t['max_reserve']:.2f}")
        print("  The cap you approve is an APPLICATION admission limit, not a provider-enforced ceiling;")
        print("  see docs/smoke-test-cost.md for what can be billed beyond it.")
    return Decimal(t["min_cap_nominal"]).quantize(Decimal("0.01"))


# ------------------------------------------------------------------ the run
async def run(simulated: bool, approve: Decimal | None, run_id: str) -> int:
    from sqlalchemy import select, text

    from storypanel.config import get_settings
    from storypanel.db.models import SpendCap
    from storypanel.db.session import transaction

    s = get_settings()
    if not simulated:
        if s.provider_mode != "live":
            print("refusing: SP_PROVIDER_MODE is not live", file=sys.stderr)
            return 2
        need = estimate(verbose=False)
        if approve is None or approve < need:
            print(f"refusing: --approve-usd {approve} is below USD {need:.2f}, the smallest cap with which a clean "
                  "run can finish (not a guaranteed upper bound on provider charges)", file=sys.stderr)
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
            # Checked before approval, so a wrong count costs no panel images (simulated and live alike).
            scripted = [len(pg["panels"]) for pg in p["script"]["content"]["pages"]]
            result["scripted_panels"] = scripted
            if scripted != [PANELS_REQUESTED]:
                raise RuntimeError(f"script has {scripted} panels; asked for one page of {PANELS_REQUESTED}")
            await h.post(f"{API}/v1/projects/{pid}/script/approve", headers=H, json={"script_version": p["script_version"]})
            p = await until(pid, lambda p: p["status"] in ("completed", "partially_completed")
                            and all(pg["preview_url"] for pg in p["pages"]), "panels")  # fmt: skip
            panels = [pn for pg in p["pages"] for pn in pg["panels"]]
            result["panels"] = len(panels)
            result["panel_status"] = [pn["status"] for pn in panels]
            result["panels_requested"] = PANELS_REQUESTED
            if len(panels) != PANELS_REQUESTED:  # same assertion in simulated and live runs
                raise RuntimeError(f"asked for {PANELS_REQUESTED} panels, got {len(panels)}")
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
    result["timing_resolution_s"] = 2  # stage timings are polled every 2 s (wall clock to first observed state)
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
        embedded = all("+" in str(f.get_object()["/BaseFont"]) for f in page["/Resources"]["/Font"].values())
        return {"ok": bool(text.strip()) and any("ComicNeue" in f for f in fonts) and embedded, "bytes": len(data),
                "all_fonts_embedded": embedded,
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
        b["max_estimate"] = max(Decimal(b.get("max_estimate", 0)), r["cost_estimate_usd"])
        b["max_input_tokens"] = max(b.get("max_input_tokens", 0), r["input_tokens"] or 0)
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
    by = result["usage"]["by_stage"]
    calls = {k: v["calls"] for k, v in by.items()}
    # per stage|operation: the largest reservation made (simulated prices; text prices equal the live
    # ones per token, and the input bound is recovered from it) and the largest recorded input size
    sizes = {k: {"max_estimate": str(v["max_estimate"]), "max_input_tokens": v["max_input_tokens"]}
             for k, v in by.items()}  # fmt: skip
    PROFILE.write_text(json.dumps({
        "recorded_at": datetime.now(UTC).date().isoformat(), "source": "live-smoke.py run --simulated",
        "characters": result.get("characters", 0), "panels": result.get("panels", 0), "calls": calls,
        "sizes": sizes}, indent=1, default=str))
    print(f"call profile recorded: {PROFILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    e = sub.add_parser("estimate")
    e.add_argument("--markdown", action="store_true", help="print the itemised table as Markdown")
    r = sub.add_parser("run")
    r.add_argument("--simulated", action="store_true")
    r.add_argument("--approve-usd", type=Decimal)
    r.add_argument("--run-id", default=None)
    a = ap.parse_args()
    if a.cmd == "preflight":
        return asyncio.run(preflight())
    if a.cmd == "estimate":
        if a.markdown:
            print(cost_markdown(), end="")
        else:
            estimate()
        return 0
    return asyncio.run(run(a.simulated, a.approve_usd, a.run_id or uuid.uuid4().hex[:8]))


if __name__ == "__main__":
    sys.exit(main())
