"""QA and Continuity: safety, vision checklist and identity similarity for one image.

Two outcomes matter and they are kept apart on purpose:
- quality failures ("fail") may be retried and, once retries run out, shown with a
  "needs attention" badge;
- safety failures ("blocked") quarantine the image. No retry budget, score or fallback
  path can turn a blocked image into a visible one.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from storypanel.catalog import PROMPT_VERSION, render_prompt
from storypanel.config import get_settings
from storypanel.domain.schemas import (
    AgeRating,
    CharacterSpec,
    ContinuityRule,
    IdentityVisualReport,
    PanelSpec,
    SafetyVerdict,
    VisionQAReport,
)  # fmt: skip
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.pipeline import repair
from storypanel.providers import metering
from storypanel.providers.base import CallContext, ImageInput


@dataclass
class QAOutcome:
    status: str  # pass | fail (repairable) | review (manual review; no automatic repair) | blocked
    scores: dict[str, Any] = field(default_factory=dict)
    fix_notes: list[str] = field(default_factory=list)
    head_boxes: dict[str, dict[str, float]] = field(default_factory=dict)
    action_box: dict[str, float] | None = None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def crop_box(image: bytes, box: dict[str, float]) -> bytes:
    with Image.open(io.BytesIO(image)) as img:
        w, h = img.size
        left, top = max(0, int(box["x"] * w)), max(0, int(box["y"] * h))
        right = min(w, max(left + 8, int((box["x"] + box["w"]) * w)))
        bottom = min(h, max(top + 8, int((box["y"] + box["h"]) * h)))
        out = io.BytesIO()
        img.convert("RGB").crop((left, top, right, bottom)).save(out, format="PNG")
        return out.getvalue()


async def check_safety(image: bytes, audience: AgeRating, ctx: CallContext) -> SafetyVerdict:
    verdict = await metering.structured(
        role="vision",
        system=render_prompt("image_safety", audience=audience.value),
        user="Classify this image.",
        schema=SafetyVerdict,
        images=(ImageInput(image),),
        ctx=CallContext(
            "image_safety", ctx.user_id, ctx.project_id, ctx.target_id, ctx.attempt, PROMPT_VERSION
        ),  # fmt: skip
    )
    if {"minors", "sexual"} & set(verdict.categories):
        verdict = verdict.model_copy(update={"allowed": False})
    metrics.qa_results.labels("safety", "pass" if verdict.allowed else "blocked").inc()
    metrics.moderation_results.labels("image", "allowed" if verdict.allowed else "blocked").inc()
    return verdict


IDENTITY_SEVERITY = {"pass": 0, "manual_review": 1, "repair": 2}
REVIEW_ONLY = frozenset({"identity_uncertain", "identity_unverified", "identity_check_invalid",
                         "identity_flagged_manual_review", "rule_uncertain"})  # fmt: skip


def assess_identity(report: IdentityVisualReport, expected: list[str]) -> tuple[str, list[str]]:
    """The outcome the app acts on, derived from the per-character findings and never more lenient
    than the model's own `outcome`. A character missing from the report is unverified, never passed."""
    found = {f.id: f for f in report.characters}
    problems: list[str] = []
    derived = "pass"
    for cid in expected:
        f = found.get(cid)
        if f is None:
            problems.append(f"identity_unverified:{cid}")
            derived = max(derived, "manual_review", key=IDENTITY_SEVERITY.__getitem__)
        elif not f.present:
            problems.append(f"missing:{cid}")
            derived = "repair"
        elif f.matches_reference == "no":
            problems.append(f"identity_visual:{cid}")
            derived = "repair"
        elif f.matches_reference == "uncertain" or f.confidence == "low":
            problems.append(f"identity_uncertain:{cid}")
            derived = max(derived, "manual_review", key=IDENTITY_SEVERITY.__getitem__)
    outcome = max(derived, report.outcome, key=IDENTITY_SEVERITY.__getitem__)
    if outcome != "pass" and not problems:  # the model flagged something it did not itemise
        problems.append(f"identity_flagged_{outcome}")
    return outcome, problems


async def check_identity_visual(*, image: bytes, expected: list[str], cast: dict[str, CharacterSpec],
                                references: dict[str, list[bytes]], ctx: CallContext) -> tuple[str, list[str], dict[str, Any]]:  # fmt: skip
    """Gemini compares the panel with each expected character's approved reference images."""
    from storypanel.providers.registry import get_providers

    provider = get_providers().identity
    if provider is None:
        raise AppError(ErrorCode.INVALID_STATE, "Visual identity QA is not configured.", retryable=False)
    imgs = [ImageInput(image, label="Panel to check:")]
    for cid in expected:
        for n, ref in enumerate(references.get(cid, [])):
            imgs.append(ImageInput(ref, label=f"Reference {n + 1} for '{cid}' ({cast[cid].name}):"))
    lines = ["Expected characters:"] + [
        f"- id '{cid}' ({cast[cid].name}): {cast[cid].canonical_description()}" for cid in expected]  # fmt: skip
    model = provider.model_for("vision")
    try:
        report = await metering.structured(
            role="vision", system=render_prompt("identity_visual"), user="\n".join(lines),
            schema=IdentityVisualReport, images=tuple(imgs), provider=provider,
            ctx=CallContext("identity_visual", ctx.user_id, ctx.project_id, ctx.target_id, ctx.attempt,
                            PROMPT_VERSION, inputs={"character_ids": expected,
                                                    "reference_images": len(imgs) - 1}))  # fmt: skip
    except AppError as exc:
        if exc.code is not ErrorCode.PROVIDER_BAD_OUTPUT:
            raise  # transient: the activity retries; never treated as a pass
        metrics.identity_visual.labels("invalid_response").inc()
        return "manual_review", ["identity_check_invalid"], {"method": "gemini_visual", "model": model,
                                                             "error": "invalid_response"}  # fmt: skip
    outcome, problems = assess_identity(report, expected)
    metrics.identity_visual.labels(outcome).inc()
    return outcome, problems, {"method": "gemini_visual", "model": model, "outcome": outcome,
                               "report": report.model_dump()}  # fmt: skip


async def evaluate_panel(
    *, image: bytes, panel: PanelSpec, cast: dict[str, CharacterSpec], audience: AgeRating,
    reference_images: dict[str, bytes], reference_embeddings: dict[str, list[list[float]]],
    ctx: CallContext, identity_references: dict[str, list[bytes]] | None = None,
    required: list[ContinuityRule] | None = None, absent: list[str] | None = None,
) -> QAOutcome:  # fmt: skip
    s = get_settings()
    required = required or []
    safety = await check_safety(image, audience, ctx)
    if not safety.allowed:
        return QAOutcome("blocked", {"safety": safety.model_dump()})

    expected = [c for c in panel.characters if c.id in cast]
    images = [ImageInput(image, label="Panel to check:")]
    for ch in expected:
        if ch.id in reference_images:
            images.append(ImageInput(reference_images[ch.id], label=f"Reference for '{ch.id}':"))
    spec_lines = [f"Shot: {panel.shot}, {panel.angle}. Action: {panel.action}",
                  "Expected characters:"]  # fmt: skip
    for ch in expected:
        feats = ", ".join(cast[ch.id].visual.signature_features)
        spec_lines.append(f"- id '{ch.id}' ({cast[ch.id].name}), {ch.position}, {ch.pose}, "
                          f"{ch.expression}. Signature features: {feats}")  # fmt: skip
    if required:
        spec_lines.append("Required elements (report each by rule id):")
        spec_lines += [f"- {r.id}: {r.text}" for r in required]
    absent = [a for a in (absent or []) if a.strip()]
    if absent:
        spec_lines.append("Must NOT be visible yet, the story reveals them later (report each by number):")
        spec_lines += [f"- {n}: {a}" for n, a in enumerate(absent, start=1)]
    report = await metering.structured(
        role="vision",
        system=render_prompt("vision_qa"),
        user="\n".join(spec_lines),
        schema=VisionQAReport,
        images=tuple(images),
        ctx=CallContext(
            "vision_qa",
            ctx.user_id,
            ctx.project_id,
            ctx.target_id,
            ctx.attempt,
            PROMPT_VERSION,
            inputs={
                "characters": [
                    {
                        "id": c.id,
                        "position": c.position,
                        "signature_features": cast[c.id].visual.signature_features,
                    }
                    for c in expected
                ],
                "required_rules": [r.id for r in required],
                "absent": absent,
            },
        ),  # fmt: skip
    )

    observed = {o.id: o for o in report.characters}
    problems: list[str] = []
    if report.character_count != len(expected):
        problems.append("character_count")
    if not report.action_matches:
        problems.append("action")
    if report.visible_text:
        problems.append("stray_text")
    if report.anatomy_defects:
        problems.append("anatomy")
    # Required visual elements (must-have continuity rules). No answer for a requested rule is
    # not evidence that it is there: it is treated as uncertain, never as a pass.
    seen_rules = {e.rule_id: e.visible for e in report.required_elements}
    for r in required:
        verdict = seen_rules.get(r.id, "uncertain")
        if verdict == "no":
            problems.append(f"rule_missing:{r.id}")
        elif verdict == "uncertain":
            problems.append(f"rule_uncertain:{r.id}")
    # Something the story reveals later is in the picture: the reveal would be spoiled. Only a clear
    # "yes" counts; an unsure or missing answer is not evidence either way.
    seen_absent = {e.number: e.visible for e in report.absent_elements}
    for n, _ in enumerate(absent, start=1):
        if seen_absent.get(n) == "yes":
            problems.append(f"shown_too_early:{n}")
    ratings: list[int] = []
    for ch in expected:
        o = observed.get(ch.id)
        if o is None or not o.present:
            problems.append(f"missing:{ch.id}")
            continue
        ratings.append(max(1, min(5, o.identity_rating)))
        if o.missing_features:
            problems.append(f"features:{ch.id}")
        if o.identity_rating < 3:
            problems.append(f"identity_rating:{ch.id}")

    similarities: dict[str, float] = {}
    identity: dict[str, Any] = {"method": s.identity_qa}
    if s.identity_qa == "gemini_visual":
        refs = identity_references or {}
        present = [ch.id for ch in expected if observed.get(ch.id) is not None and observed[ch.id].present]
        no_ref = [cid for cid in present if not refs.get(cid)]
        problems += [f"no_reference:{cid}" for cid in no_ref]  # no evidence is a failure, not a pass
        to_check = [cid for cid in present if cid not in no_ref]
        if to_check:
            _, idp, identity = await check_identity_visual(image=image, expected=to_check, cast=cast,
                                                           references=refs, ctx=ctx)  # fmt: skip
            problems += [p for p in idp if p not in problems]
    # Embedding identity (Voyage): needs a location and references for every present character.
    # Without them there is no identity evidence, which is a failure, not a pass.
    for ch in expected if s.identity_qa == "voyage_embedding" else []:
        o = observed.get(ch.id)
        if o is None or not o.present:
            continue
        if not (o.body_box or o.head_box):
            problems.append(f"unlocalised:{ch.id}")
        elif not reference_embeddings.get(ch.id):
            problems.append(f"no_reference:{ch.id}")
    crops = [(ch.id, crop_box(image, (observed[ch.id].body_box or observed[ch.id].head_box).model_dump()))  # type: ignore[union-attr]
             for ch in expected
             if s.identity_qa == "voyage_embedding"
             and ch.id in observed and observed[ch.id].present and ch.id in reference_embeddings
             and (observed[ch.id].body_box or observed[ch.id].head_box)]  # fmt: skip
    if crops:
        vectors = await metering.embed_images(
            [c for _, c in crops],
            CallContext("identity_embedding", ctx.user_id, ctx.project_id, ctx.target_id),
        )
        for (cid, _), vec in zip(crops, vectors, strict=True):
            sim = max(cosine(vec, ref) for ref in reference_embeddings[cid])
            similarities[cid] = round(sim, 4)
            metrics.identity_score.observe(sim)
            if sim < s.identity_similarity_threshold:
                problems.append(f"identity_embedding:{cid}")

    passed = not problems
    # Problems a repaint cannot resolve (uncertain or unverifiable identity) go to manual review.
    blocking = [p for p in problems if p.split(":")[0] not in REVIEW_ONLY]
    status = "pass" if passed else "fail" if blocking else "review"
    metrics.qa_results.labels("vision", status).inc()
    metrics.qa_results.labels("stray_text", "fail" if report.visible_text else "pass").inc()
    notes = list(report.fix_notes)
    by_rule = {r.id: r for r in required}
    for p in problems:
        if p.startswith("rule_missing:") and p.split(":", 1)[1] in by_rule:
            notes.append(f"Draw this clearly: {by_rule[p.split(':', 1)[1]].text}")
    for p in problems:
        if p.startswith("shown_too_early:"):
            notes.append(
                f"Remove {absent[int(p.split(':')[1]) - 1]} from this panel: it must not be visible yet."
            )
    for p in problems:
        if p.startswith("identity_embedding:") and not notes:
            notes.append(f"Match {cast[p.split(':')[1]].name} to the reference image exactly.")
    for f in (identity.get("report") or {}).get("characters", []):
        for mm in f.get("mismatches", [])[:2]:
            if f["id"] in cast:
                notes.append(
                    f"{cast[f['id']].name}: {mm['aspect']} must be {mm['expected']} (not {mm['observed']})."
                )
    scores = {
        "safety": safety.model_dump(), "problems": problems, "status": status,
        "identity_rating_avg": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "visible_text": report.visible_text,
        "anatomy_defects": report.anatomy_defects, "action_matches": report.action_matches,
    }  # fmt: skip
    if s.identity_qa == "voyage_embedding":
        scores["identity_similarity"] = similarities  # embedding cosine similarity (Voyage)
    else:
        scores["identity_visual"] = identity  # Gemini categorical judgment; not a similarity score
    head_boxes = {o.id: o.head_box.model_dump() for o in report.characters if o.present and o.head_box}
    body_boxes = {o.id: o.body_box.model_dump() for o in report.characters if o.present and o.body_box}
    scores["body_boxes"] = body_boxes  # lettering keeps balloons off the characters where it can
    # where required story elements were seen, so lettering keeps them clear
    scores["prop_boxes"] = {e.rule_id: e.box.model_dump() for e in report.required_elements
                            if e.box is not None and e.visible == "yes" and e.rule_id in {r.id for r in required}}  # fmt: skip
    scores["aspects"] = aspects(problems)
    if blocking:
        scores["repair"] = repair.repair_target(blocking, body_boxes)
        notes = actionable_notes(blocking, notes, panel=panel, cast=cast, report=report)
    else:
        notes = []  # nothing will be repaired: the model's remarks are not instructions for anyone
    return QAOutcome(status, scores, notes[:6], head_boxes,
                     report.action_box.model_dump() if report.action_box else None)  # fmt: skip


# ------------------------------------------------------------------ aspects and repair notes
# What each problem is about, so story, required elements, identity and composition are judged
# and reported separately (one bad aspect never hides or excuses another).
ASPECT = {
    "action": "story", "missing": "story",
    "rule_missing": "required_elements", "rule_uncertain": "required_elements",
    "identity_visual": "identity", "identity_rating": "identity", "features": "identity",
    "identity_embedding": "identity", "identity_uncertain": "identity", "identity_unverified": "identity",
    "identity_check_invalid": "identity", "identity_flagged_repair": "identity",
    "identity_flagged_manual_review": "identity", "no_reference": "identity", "unlocalised": "identity",
    "character_count": "composition", "anatomy": "composition", "stray_text": "composition",
    "shown_too_early": "story",
}  # fmt: skip
SEVERITY = {"ok": 0, "not_checked": 1, "uncertain": 2, "defect": 3}


def aspects(problems: list[str]) -> dict[str, str]:
    """Per aspect: ok | defect (a confirmed visual problem) | uncertain (the check couldn't tell) |
    not_checked (the check failed or had nothing to compare with). Lettering is judged on the page."""
    out = {"story": "ok", "required_elements": "ok", "identity": "ok", "composition": "ok"}
    for p in problems:
        code = p.split(":")[0]
        kind = {"visual_mismatch": "defect", "missing": "defect"}.get(QA_CATEGORY.get(code, "visual_mismatch"),
                                                                       QA_CATEGORY.get(code, "defect"))  # fmt: skip
        aspect = ASPECT.get(code, "composition")
        if SEVERITY[kind] > SEVERITY[out[aspect]]:
            out[aspect] = kind
    return out


def repeated_after_repair(current: list[str], previous: list[str]) -> bool:
    """True when every blocking problem now was already there before the repair aimed at it."""
    now = {p for p in current if p.split(":")[0] not in REVIEW_ONLY}
    before = {p for p in previous if p.split(":")[0] not in REVIEW_ONLY}
    return bool(now) and now <= before


def actionable_notes(blocking: list[str], notes: list[str], *, panel: PanelSpec, cast: dict[str, CharacterSpec],
                     report: VisionQAReport) -> list[str]:  # fmt: skip
    """A repair is only worth paying for with concrete instructions. Every blocking problem gets a
    specific note when the reviewer gave none for it, written from the panel's own spec."""
    out = list(notes)
    text = " ".join(out).lower()
    expected = [c for c in panel.characters if c.id in cast]
    for p in blocking:
        code, _, subject = p.partition(":")
        name = cast[subject].name if subject in cast else subject
        if code == "missing" and name.lower() not in text:
            pos = next((c.position for c in expected if c.id == subject), "")
            out.append(
                f"Draw {name}{' on the ' + pos if pos and pos != 'center' else ''}, as in the reference image."
            )
        elif code == "character_count" and "character" not in text:
            names = ", ".join(cast[c.id].name for c in expected) or "no characters"
            out.append(
                f"Show exactly {len(expected)} character{'s' if len(expected) != 1 else ''} ({names}); no one else."
            )
        elif code == "action" and not out:
            out.append(f"Show this action clearly: {panel.action.strip().rstrip('.')}.")
        elif code == "stray_text" and "text" not in text and "letter" not in text:
            out.append("Remove every letter, word, sign with writing and speech bubble from the picture.")
        elif code == "anatomy" and not any(d.lower() in text for d in report.anatomy_defects):
            out.append("Fix these drawing defects: " + "; ".join(report.anatomy_defects[:3]) + ".")
        elif code == "features" and name.lower() not in text:
            feats = ", ".join(cast[subject].visual.signature_features) if subject in cast else ""
            out.append(f"{name}: draw the signature features clearly ({feats}).")
        elif code in ("identity_rating", "identity_embedding") and name.lower() not in text:
            out.append(f"Match {name} to the reference image: face, colours and outfit.")
        text = " ".join(out).lower()
    return out


# ------------------------------------------------------------------ user-facing reasons
# Every problem code a check can produce, by what it means for the user. "not_checked" is kept
# apart from defects: a check that errored or had nothing to compare against says nothing
# about the picture, and must never be presented as a confirmed problem.
QA_CATEGORY = {
    "identity_visual": "visual_mismatch", "identity_rating": "visual_mismatch", "features": "visual_mismatch",
    "identity_embedding": "visual_mismatch", "action": "visual_mismatch", "character_count": "visual_mismatch",
    "anatomy": "visual_mismatch", "stray_text": "visual_mismatch", "identity_flagged_repair": "visual_mismatch",
    "missing": "missing", "rule_missing": "missing", "shown_too_early": "visual_mismatch",
    "identity_uncertain": "uncertain", "rule_uncertain": "uncertain", "identity_flagged_manual_review": "uncertain",
    "identity_unverified": "not_checked", "identity_check_invalid": "not_checked", "no_reference": "not_checked",
    "unlocalised": "not_checked",
}  # fmt: skip


def qa_reasons(qa_status: str | None, problems: list[str], *, names: dict[str, str] | None = None,
               rules: dict[str, str] | None = None) -> tuple[str, list[dict[str, Any]]]:  # fmt: skip
    """(state, reasons) for one panel version. state: passed | issues | uncertain | not_checked."""
    names, rules = names or {}, rules or {}
    if qa_status == "pending":  # the checks never completed (provider errors after retries)
        return "not_checked", [{"category": "not_checked", "code": "qa_not_completed", "subject": None,
                                "label": "The quality checks didn't complete for this picture."}]  # fmt: skip
    reasons = []
    for p in problems:
        code, _, subject = p.partition(":")
        cat = QA_CATEGORY.get(code, "visual_mismatch")
        who = names.get(subject, subject)
        label = {
            "identity_visual": f"{who} looks different from the approved reference",
            "identity_rating": f"{who} doesn't closely match the approved reference",
            "identity_embedding": f"{who} doesn't closely match the approved reference",
            "features": f"{who} is missing a signature feature",
            "action": "The picture doesn't show the described action",
            "character_count": "The number of characters is different from the script",
            "anatomy": "There are drawing defects (hands, limbs or faces)",
            "stray_text": "There is unwanted text in the picture",
            "missing": f"{who} isn't in the picture",
            "rule_missing": f"Not visible: {rules.get(subject, subject)}",
            "shown_too_early": "Shows something the story reveals later",
            "identity_uncertain": f"Couldn't tell whether {who} matches the reference",
            "rule_uncertain": f"Couldn't tell whether this is visible: {rules.get(subject, subject)}",
            "identity_flagged_manual_review": "The identity check asked for a person to look",
            "identity_flagged_repair": "The identity check found a mismatch",
            "identity_unverified": f"{who} wasn't checked against the reference",
            "identity_check_invalid": "The identity check gave an unusable answer",
            "no_reference": f"There was no approved reference for {who} to compare with",
            "unlocalised": f"{who} couldn't be located in the picture to compare",
        }.get(code, p.replace("_", " "))
        reasons.append({"category": cat, "code": code, "subject": subject or None, "label": label})
    cats = {r["category"] for r in reasons}
    state = ("issues" if cats & {"visual_mismatch", "missing"} else "not_checked" if "not_checked" in cats
             else "uncertain" if "uncertain" in cats else "passed")  # fmt: skip
    return state, reasons
