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
    PanelSpec,
    SafetyVerdict,
    VisionQAReport,
)  # fmt: skip
from storypanel.obs import metrics
from storypanel.pipeline import repair
from storypanel.providers import metering
from storypanel.providers.base import CallContext, ImageInput


@dataclass
class QAOutcome:
    status: str  # pass | fail | blocked
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


async def evaluate_panel(
    *, image: bytes, panel: PanelSpec, cast: dict[str, CharacterSpec], audience: AgeRating,
    reference_images: dict[str, bytes], reference_embeddings: dict[str, list[list[float]]],
    ctx: CallContext,
) -> QAOutcome:  # fmt: skip
    s = get_settings()
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
                ]
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

    # Identity embedding: crop each character and compare with their reference crops.
    similarities: dict[str, float] = {}
    crops = [(ch.id, crop_box(image, (observed[ch.id].body_box or observed[ch.id].head_box).model_dump()))  # type: ignore[union-attr]
             for ch in expected
             if ch.id in observed and observed[ch.id].present and ch.id in reference_embeddings
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
    metrics.qa_results.labels("vision", "pass" if passed else "fail").inc()
    metrics.qa_results.labels("stray_text", "fail" if report.visible_text else "pass").inc()
    notes = list(report.fix_notes)
    for p in problems:
        if p.startswith("identity_embedding:") and not notes:
            notes.append(f"Match {cast[p.split(':')[1]].name} to the reference image exactly.")
    scores = {
        "safety": safety.model_dump(), "problems": problems,
        "identity_rating_avg": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "identity_similarity": similarities, "visible_text": report.visible_text,
        "anatomy_defects": report.anatomy_defects, "action_matches": report.action_matches,
    }  # fmt: skip
    head_boxes = {o.id: o.head_box.model_dump() for o in report.characters if o.present and o.head_box}
    body_boxes = {o.id: o.body_box.model_dump() for o in report.characters if o.present and o.body_box}
    if not passed:
        scores["repair"] = repair.repair_target(problems, body_boxes)
    return QAOutcome("pass" if passed else "fail", scores, notes[:6], head_boxes,
                     report.action_box.model_dump() if report.action_box else None)  # fmt: skip
