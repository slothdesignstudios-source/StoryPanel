"""Evaluation harness (spec 8.2-8.4).

Runs golden prompts through the real pipeline stages and reports the section 8.3 metrics
against their targets.

    uv run python -m storypanel.evals.harness --subset fast            # simulated, offline
    uv run python -m storypanel.evals.harness --subset all --live --max-usd 25

Evidence classes are kept apart on purpose. A run with fake providers is labelled
SIMULATED: it proves the pipeline, the rules and the lettering engine, and says nothing
about model quality, cost or latency. Only a --live run (real providers, explicit budget)
produces numbers that can be compared with the targets for those metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from storypanel.catalog import PROMPT_VERSION, catalog_root, layout_version, styles
from storypanel.config import get_settings
from storypanel.db.models import Balloon, Panel, PanelVersion, Project, ProviderUsage, Script, User
from storypanel.db.session import dispose_engine, transaction
from storypanel.domain.schemas import ComicScript, StoryBible
from storypanel.pipeline import lettering, moderation
from storypanel.pipeline import stages_art as art
from storypanel.pipeline import stages_plan as plan
from storypanel.providers import metering
from storypanel.providers.base import CallContext
from storypanel.services import credits, editor, events, repo

EVALS = catalog_root().parent / "evals"
# Metrics that only real providers can measure.
LIVE_ONLY = {"identity_rating_avg", "identity_pass_rate", "script_adherence_first_attempt",
             "panel_defect_rate", "stray_text_rate", "story_quality", "time_to_first_page_p50_s",
             "time_to_first_page_p90_s", "full_comic_p50_s", "cost_per_page_usd"}  # fmt: skip


async def _panel(panel_id: str, retries: int) -> None:
    versions: list[str] = []
    notes: list[str] = []
    for attempt in range(retries + 1):
        made = await art.stage_generate_panel(uuid.UUID(panel_id), request_key="initial",
                                              attempt=attempt, fix_notes=notes)  # fmt: skip
        versions.append(made["version_id"])
        verdict = await art.stage_qa_panel(uuid.UUID(made["version_id"]), attempt)
        if verdict["status"] == "pass":
            break
        notes = verdict.get("fix_notes", [])
    await art.stage_finalize_panel(uuid.UUID(panel_id), version_ids=versions, kind="generate",
                                   request_key="initial", intent_seq=0)  # fmt: skip


async def run_case(case: dict[str, Any], user_id: uuid.UUID) -> dict[str, Any]:
    """Drive one prompt through the same stages, in the same order, as ComicWorkflow,
    auto-approving both gates."""
    s = get_settings()
    out: dict[str, Any] = {"id": case["id"], "tags": case["tags"], "policy": case["policy"]}
    started = time.monotonic()
    rules = moderation.rule_check(moderation.neutralise_style_references(case["prompt"])[0])
    async with transaction() as db:
        project = Project(id=uuid.uuid4(), owner_id=user_id, prompt=case["prompt"])
        db.add(project)
        await db.flush()
        await credits.reserve(db, user_id, 8, purpose="plan", project_id=project.id,
                              idempotency_key=f"eval-plan:{project.id}")  # fmt: skip
        pid = project.id
    try:
        if rules is not None:
            out.update(outcome="blocked", blocked_by="rules", categories=rules.categories)
            return out
        r = await plan.stage_intake(pid)
        if r["outcome"] == "questions":
            out["questions_asked"] = True
            r = await plan.stage_intake(pid, apply_answers=True)
        if r["outcome"] == "blocked":
            out.update(outcome="blocked", blocked_by="classifier")
            return out
        bible = await plan.stage_bible(pid)
        if bible["outcome"] == "blocked":
            out.update(outcome="blocked", blocked_by="classifier")
            return out
        await asyncio.gather(*[plan.stage_character_sheet(pid, c) for c in bible["characters"]],
                             *[plan.stage_location_ref(pid, loc) for loc in bible["locations"]])  # fmt: skip
        cast = await plan.stage_cast_ready(pid)
        async with transaction() as db:
            await plan.record_approval(db, pid, "cast", cast["cast_version"], user_id)
        await plan.stage_begin_scripting(pid)
        sc = await plan.stage_script(pid)
        if sc["outcome"] == "blocked":
            out.update(outcome="blocked", blocked_by="classifier")
            return out
        async with transaction() as db:
            row = (await db.execute(select(Script).where(Script.project_id == pid))).scalar_one()
            script = ComicScript.model_validate(row.content)
            status = (await db.execute(select(Project.status).where(Project.id == pid))).scalar_one()
            out["script_errors"] = sum(1 for i in row.issues if i.get("severity") == "error")
            n_panels = sum(len(p.panels) for p in script.pages)
            if out["script_errors"] == 0:
                await credits.reserve(db, user_id, n_panels, purpose="panels", project_id=pid,
                                      idempotency_key=f"eval-panels:{pid}")  # fmt: skip
                await plan.record_approval(db, pid, "script", sc["script_version"], user_id)
            brief: dict[str, Any] = (
                await db.execute(select(Project.brief).where(Project.id == pid))
            ).scalar_one() or {}
            out["script_repairs"] = (await db.execute(
                select(func.count()).select_from(ProviderUsage).where(
                    ProviderUsage.project_id == pid, ProviderUsage.stage == "script_repair",
                    ProviderUsage.status == "succeeded"))).scalar_one()  # fmt: skip
        out["brief"] = {k: brief[k] for k in ("format", "pages", "style_preset", "audience")}
        out["brief_matches"] = all(brief.get(k) == v for k, v in case["expect"].items())
        if out["script_errors"] or status != "awaiting_script_approval":
            out.update(outcome="script_invalid")
            return out
        approved = time.monotonic()
        pages = (await art.stage_plan_pages(pid, sc["script_version"]))["pages"]
        first_page: float | None = None

        async def chain(ids: list[str]) -> None:
            for panel_id in ids:
                await _panel(panel_id, s.max_panel_qa_retries)

        async def page(p: dict[str, Any]) -> None:
            nonlocal first_page
            await asyncio.gather(*[chain(c) for c in p["chains"]])
            await art.stage_letter_page(uuid.UUID(p["page_id"]))
            first_page = first_page or time.monotonic() - approved

        await asyncio.gather(*[page(p) for p in pages])
        final = await art.stage_finish(pid)
        generated = time.monotonic()
        # A finished comic includes its export (with export-time upscaling): run it exactly as
        # ExportWorkflow does and include its time and cost.
        export_status = "not_run"
        if final["status"] in ("completed", "partially_completed"):
            async with transaction() as db:
                proj = (
                    await db.execute(select(Project).where(Project.id == pid).with_for_update())
                ).scalar_one()
                export = await editor.create_export(db, proj, "pdf", f"eval:{pid}")
                export_id = export.id
            begin = await art.stage_export_begin(export_id)
            for n in range(int(begin["pages"])):
                await art.stage_export_page(export_id, n)
            export_status = (await art.stage_export_assemble(export_id)).get("status", "ready")
        out.update(outcome=final["status"], time_to_first_page_s=round(first_page or 0, 2),
                   time_to_complete_s=round(generated - approved, 2),
                   time_to_export_s=round(time.monotonic() - generated, 2), export_status=export_status,
                   time_to_finished_comic_s=round(time.monotonic() - approved, 2),
                   pages=len(pages), panels=n_panels)  # fmt: skip

        async with transaction() as db:
            panels = (await db.execute(select(Panel).where(Panel.project_id == pid))).scalars().all()
            versions = (await db.execute(
                select(PanelVersion).join(Panel, Panel.id == PanelVersion.panel_id)
                .where(Panel.project_id == pid))).scalars().all()  # fmt: skip
            balloons = (await db.execute(select(Balloon).where(Balloon.project_id == pid))).scalars().all()
            bible_doc = await repo.latest_bible(db, pid)
            script_json = row.content
        first = [v for v in versions if v.attempt == 0]
        ratings = [v.qa_scores["identity_rating_avg"] for v in versions
                   if v.qa_scores and v.qa_scores.get("identity_rating_avg")]  # fmt: skip
        current = {p.current_version_id for p in panels}
        final_versions = [v for v in versions if v.id in current]
        first_failed = {v.panel_id for v in first if v.qa_status != "pass"}
        out["panel_stats"] = {
            "first_attempt_pass": sum(v.qa_status == "pass" for v in first), "first_attempts": len(first),
            # defects found on the first attempt / of those, fixed by automatic retries or
            # repair / still defective in the delivered comic
            "first_attempt_defects": len(first_failed),
            "repaired_defects": sum(p.id in first_failed and p.status == "ok" for p in panels),
            "final_not_ok": sum(p.status != "ok" for p in panels), "panels": len(panels),
            "stray_text": sum(bool((v.qa_scores or {}).get("visible_text")) for v in final_versions),
            "identity_ratings": ratings,
            "identity_pass": sum(not any(x.startswith(("identity", "unlocalised", "no_reference"))
                                         for x in (v.qa_scores or {}).get("problems", []))
                                 for v in final_versions),
            "final_versions": len(final_versions),
            # nobody regenerates panels in an automated run; measured only with real users
            "user_regenerations": None,
        }  # fmt: skip
        out["lettering"] = {
            "balloons": len(balloons), "covers_face": sum("covers_face" in b.flags for b in balloons),
            "reading_order": sum("reading_order" in b.flags for b in balloons),
            "overflow": sum("overflow" in b.flags for b in balloons),
        }  # fmt: skip
        if s.provider_mode == "live" and bible_doc is not None:
            out["story_quality"] = await judge_story(bible_doc, script_json, str(user_id), str(pid))
        out["cost"] = await _costs(pid)
        out["cost_usd"] = out["cost"]["product_usd"]
        return out
    finally:
        out["wall_s"] = round(time.monotonic() - started, 2)
        async with transaction() as db:
            await credits.close_for_project(db, pid)


async def ensure_run(run_id: str, budget: Any, reason: str) -> tuple[uuid.UUID, Any]:
    """The run's dedicated user and lifetime spend cap, created once and reused after a
    restart. Everything already counted for the run (including failed and unresolved
    attempts) stays counted. A different budget for an existing run is refused rather than
    silently changing the ceiling."""
    from decimal import Decimal

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from storypanel.db.models import ProviderAttempt, SpendCap

    subject = f"eval-run:{run_id}"
    async with transaction() as db:
        await db.execute(pg_insert(User).values(id=uuid.uuid4(), auth_subject=subject, plan="pro")
                         .on_conflict_do_nothing(index_elements=["auth_subject"]))  # fmt: skip
        user = (
            await db.execute(select(User).where(User.auth_subject == subject).with_for_update())
        ).scalar_one()
        await credits.grant(
            db, user.id, 100_000, kind="grant", reason="eval run", dedupe_key=f"eval:{user.id}"
        )
        cap = (await db.execute(select(SpendCap).where(SpendCap.user_id == user.id))).scalar_one_or_none()
        if cap is None:
            db.add(SpendCap(user_id=user.id, cap_usd=Decimal(budget), reason=reason[:80]))
        elif Decimal(cap.cap_usd) != Decimal(budget):
            raise ValueError(f"run {run_id} already has a budget of {cap.cap_usd} USD (asked {budget})")
        spent = (await db.execute(select(func.coalesce(func.sum(ProviderAttempt.cost_counted_usd), 0)).where(
            ProviderAttempt.user_id == user.id))).scalar_one()  # fmt: skip
        return user.id, Decimal(spent)


async def _costs(project_id: uuid.UUID) -> dict[str, float]:
    """Every attempt counts, including failed, lost and unresolved ones. The evaluation's own
    judge calls are reported separately so they do not inflate the product's cost."""
    from storypanel.db.models import ProviderAttempt

    async with transaction() as db:
        rows = (await db.execute(select(ProviderAttempt.stage, ProviderAttempt.cost_counted_usd,
                                        ProviderAttempt.cost_basis).where(
            ProviderAttempt.project_id == project_id))).all()  # fmt: skip
    judge = sum(float(c) for st, c, _ in rows if st.startswith("eval_"))
    product = sum(float(c) for st, c, _ in rows if not st.startswith("eval_"))
    return {"product_usd": round(product, 6), "eval_overhead_usd": round(judge, 6),
            "unresolved_usd": round(sum(float(c) for _, c, b in rows if b in ("reserved", "unresolved")), 6),
            "export_usd": round(sum(float(c) for st, c, _ in rows if st.startswith("export")), 6),
            "attempts": len(rows)}  # fmt: skip


async def judge_story(bible: StoryBible, script: dict[str, Any], user_id: str, project_id: str) -> float:
    """LLM judge for story quality (live runs only). Uses the rubric dimensions verbatim."""
    from pydantic import BaseModel

    class StoryScores(BaseModel):
        hook: int
        coherence: int
        pacing: int
        payoff: int
        voice: int

    rubric = json.loads((EVALS / "rubrics.json").read_text())["story_judge"]["dimensions"]
    scores = await metering.structured(
        role="story",
        system=("You are grading a short comic script against a rubric. Score each dimension from 1 "
                "(poor) to 5 (excellent). Be a demanding reader: 3 means competent but forgettable.\n"
                + "\n".join(f"- {k}: {v}" for k, v in rubric.items())),
        user=f"Story Bible:\n{bible.model_dump_json()}\n\nScript:\n{json.dumps(script)}",
        schema=StoryScores, ctx=CallContext("eval_story_judge", user_id, project_id),
    )  # fmt: skip
    return float(round(statistics.mean(scores.model_dump().values()), 2))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summarise(results: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """Quality metrics are computed over completed comics, and every report carries the
    denominators: how many cases errored, were blocked or did not finish are never hidden
    behind an average of the successes."""
    done = [r for r in results if "panel_stats" in r]
    ps = [r["panel_stats"] for r in done]

    def total(key: str) -> int:
        return sum(int(p[key]) for p in ps)

    ratings = [x for p in ps for x in p["identity_ratings"]]
    balloons = sum(r["lettering"]["balloons"] for r in done)
    should_block = [r for r in results if r["policy"] == "block"]
    allow = [r for r in results if r["policy"] == "allow"]
    four_page = [r["time_to_complete_s"] for r in done if r.get("pages") == 4]
    quality = [r["story_quality"] for r in done if "story_quality" in r]
    pages = sum(r.get("pages", 0) for r in done)
    metrics: dict[str, float | None] = {
        "identity_rating_avg": round(statistics.mean(ratings), 2) if ratings else None,
        "identity_pass_rate": total("identity_pass") / total("final_versions") if ps and total("final_versions") else None,
        "script_adherence_first_attempt": total("first_attempt_pass") / total("first_attempts") if ps else None,
        "panel_defect_rate": total("final_not_ok") / total("panels") if ps else None,
        "stray_text_rate": total("stray_text") / total("final_versions") if ps and total("final_versions") else None,
        "story_quality": round(statistics.mean(quality), 2) if quality else None,
        "balloon_face_overlap_rate": sum(r["lettering"]["covers_face"] for r in done) / balloons if balloons else None,
        "reading_order_errors": sum(r["lettering"]["reading_order"] for r in done) if done else None,
        "time_to_first_page_p50_s": _pct([r["time_to_first_page_s"] for r in done], 0.5),
        "time_to_first_page_p90_s": _pct([r["time_to_first_page_s"] for r in done], 0.9),
        "full_comic_p50_s": _pct(four_page, 0.5),
        "cost_per_page_usd": round(sum(r["cost_usd"] for r in done) / pages, 4) if pages else None,
        "first_attempt_defect_rate": total("first_attempt_defects") / total("panels") if ps and total("panels") else None,
        "repaired_defect_rate": (total("repaired_defects") / total("first_attempt_defects")
                                 if ps and total("first_attempt_defects") else None),
        "finished_comic_p50_s": _pct([r["time_to_finished_comic_s"] for r in done if r.get("pages") == 4], 0.5),
        "completion_rate": (len([r for r in allow if r.get("export_status") == "ready"]) / len(allow)) if allow else None,
        "policy_block_recall": (sum(r.get("outcome") == "blocked" for r in should_block) / len(should_block)
                                if should_block else None),
        "script_valid_after_repair": (sum(r.get("script_errors", 1) == 0 for r in allow if "script_errors" in r)
                                      / max(1, sum("script_errors" in r for r in allow))) if allow else None,
    }  # fmt: skip
    targets = json.loads((EVALS / "rubrics.json").read_text())["targets"]
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
           "==": lambda a, b: a == b}  # fmt: skip
    gates = {}
    for name, target in targets.items():
        value = metrics.get(name)
        if value is None:
            verdict = "not_measured"
        elif mode == "simulated" and name in LIVE_ONLY:
            verdict = "simulated_only"  # a fake provider cannot pass or fail a model-quality gate
        else:
            verdict = "pass" if ops[target["op"]](value, target["value"]) else "fail"
        gates[name] = {"value": value, "target": f"{target['op']} {target['value']}", "verdict": verdict}
    return {
        "metrics": metrics, "gates": gates,
        "counts": {"cases": len(results), "completed": len(done),
                   "allow_policy_cases": len(allow), "block_policy_cases": len(should_block),
                   "exported": sum(r.get("export_status") == "ready" for r in done),
                   "incomplete": [r["id"] for r in allow if "panel_stats" not in r and r.get("outcome") != "blocked"],
                   "false_blocks": sum(r.get("outcome") == "blocked" for r in allow),
                   "brief_mismatches": sum(r.get("brief_matches") is False for r in allow),
                   "errors": sum("error" in r for r in results)},
        "denominators": {"quality_metrics": "completed comics", "completion_rate": "allow-policy cases",
                         "policy_block_recall": "block-policy cases",
                         "first_attempt_defect_rate": "panels in completed comics",
                         "repaired_defect_rate": "first-attempt defects"},
        "cost": {"product_usd": round(sum(r["cost"]["product_usd"] for r in done), 4),
                 "eval_overhead_usd": round(sum(r["cost"]["eval_overhead_usd"] for r in done), 4),
                 "unresolved_usd": round(sum(r["cost"]["unresolved_usd"] for r in done), 4),
                 "note": "includes failed and unresolved attempts; cases that errored before completion are "
                         "counted in the run budget (spend cap) but not here"},
    }  # fmt: skip


def bakeoff_decision(scores: dict[str, dict[str, float]]) -> list[tuple[str, float]]:
    """Weighted decision matrix (spec 8.4). `scores[model][criterion]` are 0-1, higher is better."""
    weights = json.loads((EVALS / "rubrics.json").read_text())["bakeoff"]["weights"]
    ranked = [(m, round(sum(weights[c] * s.get(c, 0.0) for c in weights), 4)) for m, s in scores.items()]
    return sorted(ranked, key=lambda x: -x[1])


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", choices=["fast", "all"], default="fast")
    ap.add_argument("--live", action="store_true", help="use real providers (costs money)")
    ap.add_argument("--max-usd", type=float, default=0.0, help="hard budget for a --live run")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument(
        "--run-id",
        default=None,
        help="persistent run identity; re-use it after a restart to keep the same budget",
    )
    ap.add_argument("--out", default=str(EVALS / "reports"))
    args = ap.parse_args(argv)
    s = get_settings()
    mode = "live" if s.provider_mode == "live" else "simulated"
    if args.live != (mode == "live"):
        print(f"refusing to run: --live={args.live} but SP_PROVIDER_MODE={s.provider_mode}", file=sys.stderr)
        return 2
    if args.live and args.max_usd <= 0:
        print("a --live run needs an explicit --max-usd budget", file=sys.stderr)
        return 2
    if not s.is_deployed:
        from storypanel import storage

        await storage.ensure_bucket()  # local runs may use a fresh S3 bucket
    golden = json.loads((EVALS / "golden" / "golden-v1.json").read_text())
    cases = [c for c in golden["cases"] if args.subset == "all" or c["id"] in golden["fast_subset"]]
    # The run's budget is persisted as a lifetime spend cap on this run's own user, enforced
    # by the same serialised reservation as every other ceiling: it does not reset at
    # midnight, survives restarts (re-run with the same --run-id), and no other account's
    # activity counts against it. Live: --max-usd. The global daily ceiling stays in force for
    # live runs. Simulated: prices are fictional, so the global ceiling is lifted for this
    # process; the per-comic ceiling stays.
    from decimal import Decimal

    budget = Decimal(str(args.max_usd)) if args.live else Decimal("100000")
    run_id = args.run_id or uuid.uuid4().hex
    try:
        user_id, spent = await ensure_run(run_id, budget, f"eval run {args.subset} {mode}")
    except ValueError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 2
    print(f"run {run_id}: budget {budget} USD, already counted {spent} USD", file=sys.stderr)
    if not args.live:
        object.__setattr__(s, "spend_limit_global_daily_usd", budget)
    sem = asyncio.Semaphore(args.concurrency)

    async def guarded(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                return await run_case(case, user_id)
            except Exception as exc:  # one bad case must not hide the others
                return {"id": case["id"], "tags": case["tags"], "policy": case["policy"],
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}"}  # fmt: skip

    started = time.monotonic()
    results = await asyncio.gather(*[guarded(c) for c in cases])
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,  # noqa: S607
                                cwd=EVALS, check=False).stdout.strip()  # fmt: skip
    except OSError:
        commit = ""
    report = {
        "evidence": "LIVE" if mode == "live" else "SIMULATED (fake providers: not evidence of model quality, cost or latency)",
        "mode": mode, "subset": args.subset, "at": datetime.now(UTC).isoformat(), "commit": commit,
        "versions": {"golden": golden["version"], "prompts": PROMPT_VERSION, "styles": styles().version,
                     "layouts": layout_version(), "lettering_font_range": [lettering.FONT_MIN, lettering.FONT_MAX]},
        "models": {"story": s.text_model_story, "light": s.text_model_light, "vision": s.vision_model,
                   "image": s.image_model_generate, "embedding": s.embedding_model},
        "duration_s": round(time.monotonic() - started, 1),
        **summarise(list(results), mode), "cases": results,
    }  # fmt: skip
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{mode}-{args.subset}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=1, default=str))
    print(f"{report['evidence']}\nreport: {path}\ncounts: {report['counts']}")
    for name, g in report["gates"].items():
        print(f"  {g['verdict']:<15} {name:<34} {g['value']!s:<8} target {g['target']}")
    await events.close_redis()
    await dispose_engine()
    failed = [n for n, g in report["gates"].items() if g["verdict"] == "fail"]
    return 1 if failed or report["counts"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
