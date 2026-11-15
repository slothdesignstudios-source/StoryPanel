#!/usr/bin/env python3
"""Bounded smoke test: one 3-panel strip, end to end, through the running API and worker.

    live-smoke.py preflight                       free checks only; no paid calls
    live-smoke.py estimate                        itemised cost model (expected, minimum cap, worst-case holds)
    live-smoke.py estimate --image-provider gemini
    live-smoke.py run --simulated [--image-provider openai|gemini]
                                                  same flow on fake providers; records the call profile
    live-smoke.py run --approve-usd X --run-id ID [--image-provider openai|gemini]
                                                  the paid run (live providers only)
    live-smoke.py reconcile --run-id ID [--charges charges.csv]
                                                  after the run: settle open requests, import provider charges

The flow: create a comic from a fixed 3-panel prompt (Anthropic text generation + moderation) with
the chosen image provider (OpenAI by default, or Gemini; kept for the whole comic), approve the cast
(character reference sheets), approve the script (reference-conditioned panel generation, Anthropic
safety and layout checks, Gemini visual identity QA, vector lettering), a persistent visual prop
(continuity rule "The keeper carries a brass lantern", checked in panel QA), one script edit before
approval (three distinct camera shots and a changed action; re-checked against the Story Bible),
one instruction edit on panel 1, then PDF, PNG and CBZ exports (native images, local resampling, effective-DPI report),
downloaded and validated. fal and Voyage are not called. Usage and cost are read back per stage
from `provider_attempts`: reserved estimate, calculated cost, unresolved potential charges and
provider-confirmed charges, kept separate.

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

PROMPT = ("A lighthouse keeper, who always carries a brass lantern, and a cheeky seagull argue over the "
          "last sandwich on a windy pier, as a 3 panel strip")  # fmt: skip
PANELS_REQUESTED = 3
EDIT = "Make the seagull hold the sandwich in its beak."
# Persistent visual prop: a must-have rule on the keeper, checked on the sheet and in every panel's QA.
PROP_RULE = {"text": "The keeper carries a brass lantern", "applies_to": "character", "strength": "must",
             "visual": True, "source": "user"}  # fmt: skip
SHOTS = ["wide", "medium", "close_up"]  # the script edit makes the three camera shots distinct
SCRIPT_EDIT_ACTION = " The keeper raises the brass lantern."  # appended to the last panel's action
RULE_FIELDS = ("id", "text", "applies_to", "target", "strength", "visual", "source", "world_rule_index")
PROFILE = ROOT / "docs/evidence/smoke-call-profile.json"
API = os.environ.get("SP_SMOKE_API", "http://127.0.0.1:8000")
IMAGE_PROVIDERS = ("openai", "gemini")
# FakeSyncImageProvider's reported usage (providers/fake.py): prompt bytes // 4 text tokens, 1000 per input image
FAKE_BYTES_PER_TEXT_TOKEN, FAKE_TOKENS_PER_INPUT_IMAGE = 4, 1000

# simulated model -> the live setting that replaces it (text roles run on Anthropic)
LIVE_MODEL_FOR = {"text_story": "text_model_story", "text_light": "text_model_light", "text_vision": "vision_model"}
IMAGE_MODEL_SETTING = {"openai": "openai_image_model", "gemini": "gemini_image_model"}


def _env_file(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ------------------------------------------------------------------ preflight (free)
# Hosts the live run talks to, and how. The provider key travels in the named header (set it as a
# network secret in a cloud environment, or in the untracked .env locally).
HOSTS = [
    ("api.anthropic.com", "x-api-key", "story, script, moderation, vision checks; Models API"),
    ("api.openai.com", "Authorization: Bearer", "OpenAI images (generate, edit); Models API"),
    ("generativelanguage.googleapis.com", "x-goog-api-key", "Gemini identity QA and Gemini images; models.get"),
]
# Only when the retained fal / Voyage adapters are explicitly enabled (off by default).
FAL_HOSTS = [("queue.fal.run", "Authorization: Key", "fal image jobs"),
             ("rest.fal.ai", "Authorization: Key", "fal upload token"),
             ("api.fal.ai", "Authorization: Key", "fal pricing API"),
             ("v3.fal.media", None, "fal uploads and downloads")]  # fmt: skip
VOYAGE_HOSTS = [("api.voyageai.com", "Authorization: Bearer", "identity embeddings")]
FAL_UNITS = {"per_image": ("image", "images"), "per_compute_second": ("second", "seconds", "compute second",
                                                                     "compute seconds")}  # fmt: skip


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


async def preflight() -> int:
    """Free checks only: nothing here is billed (model lookups and the network probe are free).
    Credential-free checks always run."""
    from storypanel.config import Settings, get_settings
    from storypanel.providers import pricing

    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

    print("preflight: configuration and contracts (no paid calls)")
    env = {**_env_file(ROOT / "config/live-smoke.env"), **{k: v for k, v in os.environ.items() if k.startswith("SP_")}}
    fal_on, voyage_on = _truthy(env.get("SP_FAL_ENABLED")), _truthy(env.get("SP_VOYAGE_ENABLED"))
    print(f"  [INFO] fal {'ENABLED' if fal_on else 'disabled (not called, no key needed)'}; "
          f"Voyage {'ENABLED' if voyage_on else 'disabled (not called, no key needed)'}")
    # 1. Network: an unauthenticated request per host. Any HTTP answer means reachable; a refused
    #    proxy tunnel or a failed DNS lookup means the environment's network does not allow it.
    hosts = HOSTS + (FAL_HOSTS if fal_on else []) + (VOYAGE_HOSTS if voyage_on else [])
    async with httpx.AsyncClient(timeout=15) as h:
        for host, _, why in hosts:
            try:
                r = await h.get(f"https://{host}/")
                check(f"network: {host} reachable", True, f"HTTP {r.status_code} unauthenticated ({why})")
            except Exception as exc:
                check(f"network: {host} reachable", False, f"{type(exc).__name__}: blocked or unresolved ({why})")
    # 2. The price table itself (no keys needed).
    try:
        raw = json.loads((ROOT / env["SP_PRICING_FILE"]).read_text())
        table = pricing.parse_table(raw)
        models = [("anthropic", env.get("SP_TEXT_MODEL_STORY")), ("anthropic", env.get("SP_TEXT_MODEL_LIGHT")),
                  ("anthropic", env.get("SP_VISION_MODEL")), ("openai", env.get("SP_OPENAI_IMAGE_MODEL")),
                  ("gemini", env.get("SP_GEMINI_IMAGE_MODEL")), ("gemini", env.get("SP_GEMINI_QA_MODEL"))]  # fmt: skip
        if fal_on:
            models += [("fal", env.get("SP_IMAGE_MODEL_GENERATE")), ("fal", env.get("SP_IMAGE_MODEL_EDIT"))]
        if voyage_on:
            models += [("voyage", env.get("SP_EMBEDDING_MODEL"))]
        missing = [f"{p}/{m}" for p, m in models if m and (p, m) not in table]
        check("price table covers every configured model", not missing, ", ".join(missing))
        used = {(p, m) for p, m in models if m}
        unverified = sorted(f"{e['provider']}/{e['model']}" for e in raw["entries"]
                            if "UNVERIFIED" in str(e.get("verification", "")) and (e["provider"], e["model"]) in used)
        if unverified:
            print(f"  [WARN] prices not confirmed from the official page: {', '.join(unverified)} "
                  "(check them on the provider's pricing page before a paid run)")
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
    check("fal and Voyage disabled", not s.fal_enabled and not s.voyage_enabled,
          f"SP_FAL_ENABLED={s.fal_enabled} SP_VOYAGE_ENABLED={s.voyage_enabled}")
    check("exports resample locally", s.export_upscale == "local", s.export_upscale)
    check("identity QA is Gemini visual", s.identity_qa == "gemini_visual", s.identity_qa)
    needed = {"anthropic_api_key"}
    needed |= {"openai_api_key"} if "openai" in s.image_providers else set()
    needed |= {"gemini_api_key"} if ("gemini" in s.image_providers or s.identity_qa == "gemini_visual") else set()
    for key in sorted(needed):
        check(f"SP_{key.upper()} present", getattr(s, key) is not None)
    problems = pricing.startup_problems(s)
    check("pricing table: every configured model priced, verified within "
          f"{s.pricing_max_age_days} days", not problems, "; ".join(problems)[:600])  # fmt: skip
    # 5. Free model lookups: each key works and each configured model exists.
    if s.anthropic_api_key is not None:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
            for mid in sorted({m for m in (s.text_model_story, s.text_model_light, s.vision_model) if m}):
                m = await client.models.retrieve(mid)
                check(f"Anthropic model {mid} exists", m.id == mid, f"max_input={getattr(m, 'max_input_tokens', '?')}")
        except Exception as exc:
            check("Anthropic Models API with the key", False, _safe_err(exc))
    if s.openai_api_key is not None and s.openai_image_model:
        try:
            import openai

            oc = openai.AsyncOpenAI(api_key=s.openai_api_key.get_secret_value(), max_retries=0, timeout=20)
            om = await oc.models.retrieve(s.openai_image_model)
            check(f"OpenAI model {s.openai_image_model} available to this key", om.id == s.openai_image_model)
        except Exception as exc:
            check("OpenAI Models API with the key", False, _safe_err(exc))
    if s.gemini_api_key is not None:
        try:
            from google import genai
            from google.genai import types

            gc = genai.Client(api_key=s.gemini_api_key.get_secret_value(),
                              http_options=types.HttpOptions(timeout=20_000))  # fmt: skip
            for mid in sorted({m for m in (s.gemini_image_model, s.gemini_qa_model) if m}):
                gm = await gc.aio.models.get(model=mid)
                check(f"Gemini model {mid} available to this key", (gm.name or "").endswith(mid),
                      f"input_limit={gm.input_token_limit}")  # fmt: skip
        except Exception as exc:
            check("Gemini models.get with the key", False, _safe_err(exc))
    # 6. fal pricing API (free, authenticated): only when fal is explicitly enabled.
    if s.fal_enabled and s.fal_key is not None:
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
    print("  [INFO] OpenAI and Gemini have no free price API: their table entries are checked by hand")
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
        await _database(check)
    try:
        async with httpx.AsyncClient(timeout=10) as h:
            r = await h.get(f"{API}/readyz")
            check("API ready", r.status_code == 200, r.text[:200])
            meta = (await h.get(f"{API}/v1/meta")).json()
            check("API runs live providers", meta.get("provider_mode") == "live", meta.get("provider_mode", "?"))
    except Exception as exc:
        check("API reachable", False, f"{API}: {type(exc).__name__}")


async def _database(check: Any) -> None:
    """The database must be UTF-8: SQL_ASCII refuses the JSON escapes the app writes for non-ASCII text,
    so a comic with an accent in it would fail part-way. Converting is a copy, not a reset: see
    docs/upgrade-verification.md."""
    try:
        from sqlalchemy import text

        from storypanel.db.session import transaction

        async with transaction() as db:
            enc = (await db.execute(text("show server_encoding"))).scalar_one()
            rev = (await db.execute(text("select version_num from alembic_version"))).scalar_one_or_none()
        check("database encoding is UTF8", enc == "UTF8", f"{enc}: see docs/upgrade-verification.md (copy to UTF-8)")
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(ROOT / "services/backend/alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "services/backend/migrations"))
        head = ScriptDirectory.from_config(cfg).get_current_head()
        check("database migrated to head", rev == head, f"at {rev}, head {head}; run: cd services/backend && uv run alembic upgrade head")
    except Exception as exc:
        check("database reachable", False, _safe_err(exc))


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
EXPECTED_OUTPUT = {"intake": 600, "moderation": 250, "bible": 5000, "script_consistency": 1200, "script": 7000,
                   "image_safety": 250, "vision_qa": 900, "identity_visual": 700, "sheet_check": 600}  # fmt: skip
LIVE_IMAGE_TOKENS_EXPECTED = 1369  # Claude: a 1K (1024x1024) image is ceil(1024/28)^2 tokens (official vision rule)
GEMINI_IMAGE_TOKENS_EXPECTED = 1120  # ASSUMPTION (unverified): Gemini 3 default media resolution per image
# Live image prompts carry the Story Bible, style and panel spec: longer than the simulated ones.
LIVE_IMAGE_PROMPT_BYTES = 4000  # ASSUMPTION; the app reserves the real prompt's bytes as tokens
TEXT_OUT_CAP = {"anthropic": 16000, "gemini": 4096}  # AnthropicTextProvider / GeminiVisionProvider.max_tokens
CAP_USD = {"openai": Decimal("8.00"), "gemini": Decimal("4.00")}  # suggested approvals, checked by cap_scenarios


def _expected_out(stage: str) -> int:
    return next((v for k, v in EXPECTED_OUTPUT.items() if stage.startswith(k)), 600)


def cost_model(image_provider: str = "openai") -> dict[str, Any]:
    """Every potentially billable call of the smoke test for one image provider, from the code's limits.

    nominal  = the happy path once (no QA failure, no retry, no clarifying question);
    maximum  = every bounded loop and every Temporal activity attempt used, every failure
               counted as billed (SP_COUNT_FAILED_REQUESTS_AS_BILLED=true). Reservations are per call.
    No fal or Voyage call exists in this flow: no upscaling (local resampling), no embeddings.
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
    fake_table = pricing.parse_table(pricing.SIMULATED)
    prof = json.loads(PROFILE.read_text())
    sizes = prof["sizes"]
    TA, IA = TEXT["retry_policy"].maximum_attempts, IMAGE["retry_policy"].maximum_attempts
    QA = 1 + int(setting("max_panel_qa_retries"))
    SR = 1 + int(setting("max_script_repairs"))
    C, L, P, E, X = int(setting("max_characters")), MAX_LOCATIONS, PANELS_REQUESTED, 1, 3
    L_NOM = min(L, prof["calls"].get("location_ref|image_sync_generate", 1))
    REFS = int(setting("image_max_references")) + 1  # adapters' max_reference_images (refs + the base image)
    ID_IMAGES = 1 + 2 * C  # the panel + approved portrait and front view per character
    img_price = table[(image_provider, setting(IMAGE_MODEL_SETTING[image_provider]))]
    qa_price = table[("gemini", setting("gemini_qa_model"))]

    def text_row(stage: str, key: str, role: str, images: int, nominal: float, maximum: int, why: str,
                 provider: str = "anthropic") -> dict[str, Any]:  # fmt: skip
        pr = qa_price if provider == "gemini" else table[("anthropic", setting(LIVE_MODEL_FOR[role]))]
        sz = sizes[key]
        fk = fake_table[("fake", sz["model"])]
        out_cap = TEXT_OUT_CAP[provider]
        # recover the simulated input bound from the simulated reservation (same token formula)
        est = Decimal(sz["max_estimate"])
        in_bound = int((est * 1_000_000 - out_cap * fk.output_usd) / fk.usd)
        in_bound += images * pricing.IMAGE_TOKEN_CAP  # the app reserves every image at the per-image cap
        reserve = pricing.text_cost(pr, in_bound, out_cap)
        per_image = GEMINI_IMAGE_TOKENS_EXPECTED if provider == "gemini" else LIVE_IMAGE_TOKENS_EXPECTED
        exp_in = sz["max_input_tokens"] + images * per_image + 1000
        expected = pricing.text_cost(pr, exp_in, _expected_out(stage))
        return {"stage": stage, "model": pr.model, "unit": f"per MTok in ${pr.usd} / out ${pr.output_usd}",
                "quantity": f"<= {in_bound:,} in + <= {out_cap:,} out tokens", "reserve": reserve,
                "expected": expected, "nominal": nominal, "maximum": maximum, "why": why, "source": pr.source}

    def image_row(stage: str, key: str, inputs: int, nominal: float, maximum: int, why: str) -> dict[str, Any]:
        pr = img_price
        sz = sizes.get(key, {})
        prompt = max(LIVE_IMAGE_PROMPT_BYTES, int(sz.get("max_prompt_bytes", 0)))
        reserve = pricing.image_reservation(pr, prompt_bytes=prompt, input_images=inputs)
        exp_inputs = min(inputs, int(sz.get("max_input_images", inputs)))
        u = pricing.ImageUsage(input_text_tokens=prompt // 4 + pricing.TEXT_OVERHEAD_TOKENS,
                               input_image_tokens=exp_inputs * pr.assumption("reserve_input_tokens_per_image"),
                               output_text_tokens=pr.assumption("reserve_output_text_tokens"),
                               output_image_tokens=pr.assumption("reserve_output_image_tokens"))  # fmt: skip
        expected = pricing.multimodal_cost(pr, u)
        assert expected is not None
        return {"stage": stage, "model": pr.model,
                "unit": f"per MTok text in ${pr.usd} / image in ${pr.image_input_usd} / text out ${pr.output_usd} "
                        f"/ image out ${pr.image_output_usd}",
                "quantity": f"<= {prompt:,} prompt B + {inputs} input image(s) x "
                            f"{pr.assumption('reserve_input_tokens_per_image'):,} + "
                            f"{pr.assumption('reserve_output_image_tokens'):,} image out tokens",
                "reserve": reserve, "expected": expected, "nominal": nominal, "maximum": maximum, "why": why,
                "source": pr.source}  # fmt: skip

    S, G, ED = "character_sheet|image_sync_generate", "character_sheet|image_sync_edit", "panel_generate|image_sync_edit"
    rows = [
        text_row("intake", "intake|text_light", "text_light", 0, 1, 2 * TA,
                 "brief; x2 if a clarifying question is answered; x TEXT attempts"),
        text_row("moderation.prompt", "moderation.prompt|text_light", "text_light", 0, 1, 2 * TA, "same activity as intake"),
        text_row("bible", "bible|text_story", "text_story", 0, 1, TA, "Story Bible + cast"),
        text_row("moderation.bible", "moderation.bible|text_light", "text_light", 0, 1, TA, "same activity as the bible"),
        image_row("character_sheet portrait", S, 0, C, C * IA, f"{C} characters x IMAGE attempts"),
        image_row("character_sheet turnaround+expressions", G, 1, 2 * C, 2 * C * IA, "2 per character, portrait as reference"),
        text_row("image_safety (sheets, locations)", "image_safety|text_vision", "text_vision", 1, 3 * C + L_NOM,
                 (3 * C + L) * IA, f"every reference image; locations nominal {L_NOM}, max {L}"),
        image_row("location_ref", "location_ref|image_sync_generate", 0, L_NOM, L * IA,
                  f"locations: nominal {L_NOM}, code cap {L}"),
        text_row("sheet_check", "sheet_check|text_vision", "text_vision", 2, C, C * TA,
                 "reference-sheet review: portrait + turnaround per character (flags only, no redraw)"),
        text_row("script", "script|text_story", "text_story", 0, 1, SR * TA, f"draft + {SR - 1} repairs, x TEXT attempts"),
        text_row("moderation.script", "moderation.script|text_light", "text_light", 0, 1, TA, "same activity as the script"),
        text_row("script_consistency", "script_consistency|text_light", "text_light", 0, 2, 2 * TA,
                 "Story Bible check of the script, again after the test's one script edit; x TEXT attempts"),
        image_row("panel_generate", ED, REFS, P, P * QA * IA,
                  f"{P} panels x {QA} QA rounds x IMAGE attempts; up to {REFS} reference images"),
        text_row("image_safety (panels)", "image_safety|text_vision", "text_vision", 1, P, P * QA * IA, "safety per candidate"),
        text_row("vision_qa", "vision_qa|text_vision", "text_vision", 4, P, P * QA * IA,
                 "layout/content check: panel + up to 3 references"),
        text_row("identity_visual (Gemini)", "identity_visual|text_vision", "", ID_IMAGES, P, P * QA * IA,
                 f"panel + portrait and front view of up to {C} characters", provider="gemini"),
        text_row("moderation.panel_edit", "moderation.panel_edit|text_light", "text_light", 0, E, E * QA * IA,
                 "edit instruction, each edit attempt"),
        image_row("panel edit", ED, REFS, E, E * QA * IA, "instruction edit x QA rounds x IMAGE attempts"),
        text_row("image_safety (edit)", "image_safety|text_vision", "text_vision", 1, E, E * QA * IA, ""),
        text_row("vision_qa (edit)", "vision_qa|text_vision", "text_vision", 4, E, E * QA * IA, ""),
        text_row("identity_visual (edit)", "identity_visual|text_vision", "", ID_IMAGES, E, E * QA * IA, "",
                 provider="gemini"),
    ]
    for r in rows:
        r["expected_total"] = r["expected"] * Decimal(str(r["nominal"]))
        r["nominal_reserve"] = r["reserve"] * Decimal(str(r["nominal"]))
        r["max_reserve"] = r["reserve"] * r["maximum"]
    tot = {k: sum((r[k] for r in rows), Decimal(0)) for k in ("expected_total", "nominal_reserve", "max_reserve")}
    # Every call settles from reported usage; exports make no paid call (local resampling).
    tot["counted_at_end"] = tot["expected_total"]
    # headroom: the largest single reservations that can be open at once (sheets + locations in parallel)
    tot["headroom"] = sum(sorted((r["reserve"] for r in rows), reverse=True)[:4], Decimal(0))
    tot["min_cap_nominal"] = tot["counted_at_end"] + tot["headroom"]
    return {"rows": rows, "totals": tot, "image_provider": image_provider, "image_model": img_price.model,
            "limits": {"TEXT attempts": TA, "IMAGE attempts": IA, "QA rounds": QA, "script drafts": SR,
                       "characters": C, "locations": L, "panels": P, "edits": E, "exports": X,
                       "reference images": REFS, "refusal fallback": setting("text_refusal_fallback")},
            "profile": prof["recorded_at"]}  # fmt: skip


def cap_scenarios(image_provider: str = "openai", cap: Decimal | None = None) -> list[dict[str, Any]]:
    """Counted spend in progressively worse runs, and whether it stays inside the cap.

    Exports make no paid call any more, so the question is whether the generation and the edit fit.
    Settled calls count their usage-based cost (expected sizes); failed calls count their full
    reservation (SP_COUNT_FAILED_REQUESTS_AS_BILLED=true). The last scenario also keeps the
    largest reservations open (in flight) at the moment the cap is checked."""
    cap = cap if cap is not None else CAP_USD[image_provider]
    m = cost_model(image_provider)
    r = {row["stage"]: row for row in m["rows"]}
    exp = lambda k: r[k]["expected"]  # noqa: E731
    res = lambda k: r[k]["reserve"]  # noqa: E731
    clean = m["totals"]["expected_total"]
    # one extra QA round for a panel (or the edit): image + safety + layout check + identity check
    qa_round = res("panel_generate") + exp("image_safety (panels)") + exp("vision_qa") + exp("identity_visual (Gemini)")
    worst_qa = qa_round * 2 * 4  # 3 panels + the edit, each using both repair rounds
    script_repairs = exp("script") * 2  # two more drafts (each re-sends the script)
    question = exp("intake") + exp("moderation.prompt")  # a clarifying question re-runs intake
    # every paid call failing once at the provider and being re-sent: the failure counts its reservation
    one_failure_each = sum((row["reserve"] * Decimal(str(row["nominal"])) for row in m["rows"]), Decimal(0))
    rows = []
    for name, total in (("clean run", clean),
                        ("every panel and the edit use both repair rounds", clean + worst_qa),
                        ("... + 2 script repairs + a clarifying question", clean + worst_qa + script_repairs + question),
                        ("... + every paid call fails once and is re-sent", clean + worst_qa + script_repairs + question
                         + one_failure_each)):  # fmt: skip
        with_holds = total + m["totals"]["headroom"]
        rows.append({"scenario": name, "counted": total, "with_open_holds": with_holds, "fits": with_holds <= cap,
                     "cap": cap})  # fmt: skip
    return rows


def _provider_markdown(image_provider: str) -> list[str]:
    m = cost_model(image_provider)
    t = m["totals"]
    cap = CAP_USD[image_provider]
    lines = [f"### Images on {image_provider} (`{m['image_model']}`)", "",
             "| Stage | Model | Unit price | Per call (reserved) | Reservation / call (USD) | Calls: clean run | "
             "Calls: max | Expected, clean run (USD) | Held if every retry ran (USD) | Why the max |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---|"]  # fmt: skip
    for r in m["rows"]:
        lines.append(f"| {r['stage']} | `{r['model']}` | {r['unit']} | {r['quantity']} | {r['reserve']:.4f} | "
                     f"{r['nominal']} | {r['maximum']} | {r['expected_total']:.4f} | {r['max_reserve']:.4f} | {r['why']} |")
    lines += ["", f"- **Estimated cost of a clean run: USD {t['expected_total']:.2f}** (assumed output sizes; "
              "UNVERIFIED OpenAI/Gemini prices)",
              f"- **Smallest cap with which a clean run can finish: USD {t['min_cap_nominal']:.2f}** "
              f"(expected total + USD {t['headroom']:.2f} for reservations open at the same time)",
              f"- Reservations if every bounded retry, QA round and activity attempt were used: USD {t['max_reserve']:.2f} "
              "(the approved cap stops the run long before this)",
              "", f"**Does a USD {cap:.2f} cap hold the bad cases?** Counted spend by scenario (exports cost nothing):", "",
              f"| Scenario | Counted (USD) | + open reservations (USD) | Fits in USD {cap:.2f} |", "|---|---:|---:|---|"]
    for c in cap_scenarios(image_provider, cap):
        lines.append(f"| {c['scenario']} | {c['counted']:.2f} | {c['with_open_holds']:.2f} | "
                     f"{'yes' if c['fits'] else '**no: the cap stops the run first**'} |")
    return lines


def cost_markdown() -> str:
    """The itemised tables for docs/smoke-test-cost.md (generated; do not edit by hand)."""
    m = cost_model("openai")
    lines = [f"Generated by `live-smoke.py estimate --markdown` from the code limits {m['limits']}, the price table "
             f"in config/live-smoke.env and the simulated call profile of {m['profile']}. fal and Voyage: no calls.", ""]
    for provider in IMAGE_PROVIDERS:
        lines += _provider_markdown(provider) + [""]
    return "\n".join(lines)


def estimate(image_provider: str = "openai", verbose: bool = True) -> Decimal:
    """Prints the itemised model; returns the smallest cap that lets a clean run finish."""
    m = cost_model(image_provider)
    t = m["totals"]
    if verbose:
        print(f"images: {image_provider} ({m['image_model']}); limits from code/config: {m['limits']}")
        print(f"  {'stage':40} {'model':24} {'reserve':>9} {'nom':>4} {'max':>4} {'expected':>9} {'max held':>9}")
        for r in m["rows"]:
            print(f"  {r['stage']:40} {r['model']:24} {r['reserve']:9.4f} {r['nominal']:>4} {r['maximum']:>4} "
                  f"{r['expected_total']:9.4f} {r['max_reserve']:9.4f}")
        print(f"  ESTIMATED cost of a clean run (assumed output sizes):          USD {t['expected_total']:.2f}")
        print(f"  smallest cap that lets a clean run finish (+ in-flight holds):  USD {t['min_cap_nominal']:.2f}")
        print(f"  reservations if every retry/QA round/attempt were used:          USD {t['max_reserve']:.2f}")
        print("  OpenAI and Gemini prices are UNVERIFIED (search summaries of the official pages); check them first.")
        print("  The cap you approve is an APPLICATION admission limit, not a provider-enforced ceiling;")
        print("  see docs/smoke-test-cost.md for what can be billed beyond it.")
    return Decimal(t["min_cap_nominal"]).quantize(Decimal("0.01"))


# ------------------------------------------------------------------ the run
async def run(simulated: bool, approve: Decimal | None, run_id: str, image_provider: str = "openai") -> int:
    from sqlalchemy import select, text

    from storypanel.config import get_settings
    from storypanel.db.models import SpendCap
    from storypanel.db.session import transaction

    s = get_settings()
    if not simulated:
        if s.provider_mode != "live":
            print("refusing: SP_PROVIDER_MODE is not live", file=sys.stderr)
            return 2
        if image_provider not in s.image_providers:
            print(f"refusing: image provider {image_provider} is not enabled (SP_IMAGE_PROVIDERS)", file=sys.stderr)
            return 2
        need = estimate(image_provider, verbose=False)
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
                          json={"prompt": PROMPT, "image_provider": image_provider})).json()  # fmt: skip
        if "id" not in p:
            print(f"refusing: comic not created: {p}", file=sys.stderr)
            return 2
        pid = p["id"]
        if p.get("status") in ("completed", "partially_completed", "failed", "blocked", "cancelled", "expired"):
            # the idempotency key returned this run id's earlier comic: never wait on it, never pay again
            print(f"refusing: run {run_id} already finished (comic {pid} is {p['status']}); use a new --run-id",
                  file=sys.stderr)  # fmt: skip
            return 2
        result: dict[str, Any] = {"run_id": run_id, "mode": "SIMULATED" if simulated else "LIVE", "project_id": pid,
                                  "prompt": PROMPT, "approved_usd": str(approve) if approve else None,
                                  "image_provider": p.get("image_provider"), "image_model": p.get("image_model")}  # fmt: skip
        try:
            if p.get("image_provider") != image_provider:  # persisted per comic; never switched mid-run
                raise RuntimeError(f"asked for {image_provider}, comic uses {p.get('image_provider')}")
            p = await until(pid, lambda p: p["status"] in ("awaiting_cast_approval", "awaiting_questions"), "story+cast")
            if p["status"] == "awaiting_questions":
                await h.post(f"{API}/v1/projects/{pid}/answers", headers=H, json={"answers": {}})
                p = await until(pid, lambda p: p["status"] == "awaiting_cast_approval", "cast")
            result["characters"] = len(p.get("characters") or [])
            p = await until(pid, lambda p: all((c.get("sheet_check") or {}).get("status") != "pending"
                                               for c in p["characters"]), "sheet review")  # fmt: skip
            # 1. Persistent visual prop: add the rule unless the Story Bible already has one (no paid call;
            #    the sheets are not redrawn, their review is then marked as made against older rules).
            keeper = next((c for c in p["characters"] if "keeper" in (c["name"] + json.dumps(c["spec"])).lower()),
                          p["characters"][0])  # fmt: skip
            rules = p["bible"]["continuity_rules"]
            prop = next((r for r in rules if "lantern" in r["text"].lower()), None)
            added = prop is None
            if prop is None:
                prop = {**PROP_RULE, "target": keeper["slug"]}
                r = await h.put(f"{API}/v1/projects/{pid}/continuity-rules", headers=H, json={
                    "cast_version": p["cast_version"],
                    "rules": [{k: x[k] for k in RULE_FIELDS if x.get(k) is not None} for x in rules] + [prop]})  # fmt: skip
                if r.status_code != 200:
                    raise RuntimeError(f"adding the prop rule failed: {r.status_code} {r.text[:300]}")
                p = r.json()
            result["prop_rule"] = {"text": prop["text"], "target": prop.get("target"), "added_by_test": added}
            cast_acks = [c["review"] for c in p["characters"] if c.get("review")]
            result["cast_confirmations"] = [{"kind": x["kind"], "character": x.get("character"), "issue": x["issue"]}
                                            for x in cast_acks]  # fmt: skip
            r = await h.post(f"{API}/v1/projects/{pid}/characters/approve", headers=H, json={
                "cast_version": p["cast_version"], "acknowledged": [x["key"] for x in cast_acks]})  # fmt: skip
            if r.status_code != 200:
                raise RuntimeError(f"cast approval refused: {r.status_code} {r.text[:300]}")
            checked = lambda p: (p["status"] == "awaiting_script_approval"  # noqa: E731
                                 and ((p.get("script") or {}).get("consistency") or {}).get("status") != "pending")
            p = await until(pid, checked, "script")
            # Checked before approval, so a wrong count costs no panel images (simulated and live alike).
            scripted = [len(pg["panels"]) for pg in p["script"]["content"]["pages"]]
            result["scripted_panels"] = scripted
            if scripted != [PANELS_REQUESTED]:
                raise RuntimeError(f"script has {scripted} panels; asked for one page of {PANELS_REQUESTED}")
            # 2. A script edit, as a person would make it: three distinct camera shots and one changed action.
            content = p["script"]["content"]
            result["generated_shots"] = [pn["shot"] for pn in content["pages"][0]["panels"]]
            for pn, shot in zip(content["pages"][0]["panels"], SHOTS, strict=True):
                pn["shot"] = shot
            content["pages"][0]["panels"][-1]["action"] += SCRIPT_EDIT_ACTION
            r = await h.patch(f"{API}/v1/projects/{pid}/script", headers=H,
                              json={"base_version": p["script_version"], "script": content})  # fmt: skip
            if r.status_code != 200:
                raise RuntimeError(f"script edit refused: {r.status_code} {r.text[:300]}")
            p = await until(pid, lambda q, v=r.json()["version"]: checked(q) and q["script_version"] == v, "script edit")
            script = p["script"]
            errors = [i for i in script["issues"] if i.get("severity", "error") == "error"]
            result["script_edit"] = {"version": script["version"], "shots": SHOTS,
                                     "consistency": (script.get("consistency") or {}).get("status"),
                                     "errors": [i["message"] for i in errors],
                                     "confirmations": [{"kind": x["kind"], "panel": x.get("panel"), "issue": x["issue"]}
                                                       for x in script["review_required"]]}  # fmt: skip
            if errors:  # stops before any panel image is paid for
                raise RuntimeError(f"edited script has blocking issues: {[i['message'] for i in errors]}")
            r = await h.post(f"{API}/v1/projects/{pid}/script/approve", headers=H, json={
                "script_version": p["script_version"], "acknowledged": [x["key"] for x in script["review_required"]]})  # fmt: skip
            if r.status_code != 200:
                raise RuntimeError(f"script approval refused: {r.status_code} {r.text[:300]}")
            p = await until(pid, lambda p: p["status"] in ("completed", "partially_completed")
                            and all(pg["preview_url"] for pg in p["pages"]), "panels")  # fmt: skip
            panels = [pn for pg in p["pages"] for pn in pg["panels"]]
            result["panels"] = len(panels)
            result["panel_status"] = [pn["status"] for pn in panels]
            result["panels_requested"] = PANELS_REQUESTED
            if len(panels) != PANELS_REQUESTED:  # same assertion in simulated and live runs
                raise RuntimeError(f"asked for {PANELS_REQUESTED} panels, got {len(panels)}")
            result["final_shots"] = [pn["spec"]["shot"] for pn in panels]
            if result["final_shots"] != SHOTS:
                raise RuntimeError(f"drawn panels use shots {result['final_shots']}, the edited script says {SHOTS}")
            # what panel QA said about each panel (the prop rule is a required element where the keeper is drawn)
            result["panel_qa"] = [{"panel": n + 1, "status": pn["status"], "qa_state": pn.get("qa_state"),
                                   "keeper_drawn": any(c["id"] == keeper["slug"] for c in pn["spec"]["characters"]),
                                   "reasons": [x["label"] for x in pn.get("qa_reasons", [])]}
                                  for n, pn in enumerate(panels)]  # fmt: skip
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
                result.setdefault("print_quality", {})[fmt] = ex.get("print_quality")
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
            select stage, operation, provider, model, status, cost_basis, cost_estimate_usd, cost_counted_usd,
                   cost_calculated_usd, cost_confirmed_usd, input_tokens, output_tokens, images, latency_ms,
                   usage_detail
            from provider_attempts where user_id = :u order by created_at"""), {"u": uid})).mappings().all()  # fmt: skip
    by: dict[str, dict[str, Any]] = {}
    tot = {k: Decimal(0) for k in ("reserved_estimate", "calculated", "unresolved", "provider_confirmed", "counted")}
    overruns = []
    per_provider: dict[str, Decimal] = {}
    for r in rows:
        b = by.setdefault(f"{r['stage']}|{r['operation']}", {"calls": 0, "model": r["model"], "provider": r["provider"],
                                                             "counted": Decimal(0), "statuses": {}})  # fmt: skip
        b["calls"] += 1
        per_provider[r["model"]] = per_provider.get(r["model"], Decimal(0)) + r["cost_counted_usd"]
        if r["usage_detail"]:
            d = r["usage_detail"]
            b["max_input_text_tokens"] = max(b.get("max_input_text_tokens", 0), d.get("input_text_tokens") or 0)
            b["max_input_image_tokens"] = max(b.get("max_input_image_tokens", 0), d.get("input_image_tokens") or 0)
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
            "counted_by_model": {k: str(v) for k, v in per_provider.items()},
            "note": "calculated = usage x operator price table (not a provider charge); unresolved = potential charges "
                    "counted at their estimate (e.g. compute-time upscaling) until a provider charge is imported"}  # fmt: skip


def _record_profile(result: dict[str, Any]) -> None:
    by = result["usage"]["by_stage"]
    calls = {k: v["calls"] for k, v in by.items()}
    # per stage|operation: the largest reservation made (simulated prices; text prices equal the live
    # ones per token, and the input bound is recovered from it) and the largest recorded input size
    sizes: dict[str, dict[str, Any]] = {}
    for k, v in by.items():
        sizes[k] = {"max_estimate": str(v["max_estimate"]), "max_input_tokens": v["max_input_tokens"], "model": v["model"]}
        if "max_input_text_tokens" in v:  # image calls: the fake reports len(prompt bytes)//4 and 1000 per input image
            sizes[k]["max_prompt_bytes"] = v["max_input_text_tokens"] * FAKE_BYTES_PER_TEXT_TOKEN
            sizes[k]["max_input_images"] = v["max_input_image_tokens"] // FAKE_TOKENS_PER_INPUT_IMAGE
    PROFILE.write_text(json.dumps({
        "recorded_at": datetime.now(UTC).date().isoformat(), "source": "live-smoke.py run --simulated",
        "image_provider": result.get("image_provider"),
        "characters": result.get("characters", 0), "panels": result.get("panels", 0), "calls": calls,
        "sizes": sizes}, indent=1, default=str))
    print(f"call profile recorded: {PROFILE}")


async def reconcile(run_id: str, charges_csv: Path | None) -> int:
    """After a live run: settle open requests with each provider, import provider-reported charges, and
    write the reconciled usage next to the run's evidence. Charges CSV columns: provider,request_id,usd,source
    (copy them from the provider dashboards; `source` says where, e.g. "OpenAI usage page 2026-11-15").
    A timed-out image call has no request id: put "attempt:<attempt_id>" (from still_unresolved) in the
    request_id column and the charge the dashboard shows for that time and model, or 0 if it shows none."""
    import csv
    from datetime import timedelta

    from sqlalchemy import text

    from storypanel.db.session import transaction
    from storypanel.providers import metering

    out_dir = ROOT / "docs/evidence/smoke" / run_id
    async with transaction() as db:
        uid = (await db.execute(text("select id from users where email = :e"),
                                {"e": f"smoke-{run_id}@example.com"})).scalar_one_or_none()  # fmt: skip
    if uid is None:
        print(f"no smoke user for run {run_id}", file=sys.stderr)
        return 2
    settled = await metering.reconcile_open_requests(older_than=timedelta(0))
    print(f"provider status checks: {settled}")
    imported = 0
    if charges_csv is not None:
        rows = [(r["provider"], r["request_id"], Decimal(r["usd"]), r["source"])
                for r in csv.DictReader(charges_csv.open())]  # fmt: skip
        imported = await metering.record_provider_charges(rows)
        print(f"provider-confirmed charges imported: {imported} attempt(s) from {len(rows)} row(s)")
    async with transaction() as db:
        open_rows = (await db.execute(text("""
            select id as attempt_id, provider, model, stage, status, provider_request_id, cost_counted_usd,
                   created_at, latency_ms
            from provider_attempts where user_id = :u and cost_basis in ('reserved', 'unresolved')
            order by created_at"""), {"u": uid})).mappings().all()  # fmt: skip
    unresolved = [{k: str(v) for k, v in r.items()} for r in open_rows]
    usage = await _usage(uuid.UUID("00000000-0000-0000-0000-000000000000"), uid)
    report = {"run_id": run_id, "mode": "LIVE", "reconciled_at": datetime.now(UTC).isoformat(),
              "provider_status_checks": settled, "charges_imported": imported, "usage": usage,
              "still_unresolved": unresolved,
              "note": "still_unresolved = potential charges counted at their reservation; look each one up in the "
                      "provider's usage dashboard (by request id, or by time and model when there is none) and "
                      "import it with --charges (request_id column: the id, or attempt:<attempt_id>)"}  # fmt: skip
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reconciled.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(usage["totals"], indent=1))
    print(f"{len(unresolved)} attempt(s) still unresolved; report: {out_dir / 'reconciled.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    e = sub.add_parser("estimate")
    e.add_argument("--markdown", action="store_true", help="print the itemised tables (both providers) as Markdown")
    e.add_argument("--image-provider", choices=IMAGE_PROVIDERS, default="openai")
    r = sub.add_parser("run")
    r.add_argument("--simulated", action="store_true")
    r.add_argument("--approve-usd", type=Decimal)
    r.add_argument("--run-id", default=None)
    r.add_argument("--image-provider", choices=IMAGE_PROVIDERS, default="openai",
                   help="image provider for this comic (kept for the whole comic)")
    rc = sub.add_parser("reconcile", help="after a live run: settle, import provider charges, report")
    rc.add_argument("--run-id", required=True)
    rc.add_argument("--charges", type=Path, default=None, help="CSV: provider,request_id,usd,source")
    a = ap.parse_args()
    if a.cmd == "preflight":
        return asyncio.run(preflight())
    if a.cmd == "estimate":
        if a.markdown:
            print(cost_markdown(), end="")
        else:
            estimate(a.image_provider)
        return 0
    if a.cmd == "reconcile":
        return asyncio.run(reconcile(a.run_id, a.charges))
    return asyncio.run(run(a.simulated, a.approve_usd, a.run_id or uuid.uuid4().hex[:8], a.image_provider))


if __name__ == "__main__":
    sys.exit(main())
