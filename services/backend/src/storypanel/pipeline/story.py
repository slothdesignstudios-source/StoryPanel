"""Story Engine: intake, Story Bible and cast, beat sheet and script. Pure of persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from storypanel.catalog import PROMPT_VERSION, render_prompt, styles
from storypanel.config import get_settings
from storypanel.domain.schemas import (
    AgeRating,
    BibleAndCast,
    CharacterSpec,
    ComicBrief,
    ComicFormat,
    FormatSpec,
    IntakeResult,
    ScriptDraft,
    ScriptIssue,
    StoryBible,
    slugify,
)  # fmt: skip
from storypanel.domain.validation import ScriptLimits, has_errors, validate_script
from storypanel.providers import metering
from storypanel.providers.base import CallContext

MAX_LOCATIONS = 3  # reference images per comic (one generation each)


def script_limits(bible: StoryBible | None = None) -> ScriptLimits:
    """Product limits; a strip whose brief asked for an exact panel count is held to it."""
    s = get_settings()
    lim = ScriptLimits(max_pages=s.max_pages, max_characters_per_panel=s.max_characters_per_panel,
                       max_words_per_balloon=s.max_words_per_balloon,
                       max_balloons_per_panel=s.max_balloons_per_panel)  # fmt: skip
    if bible is not None and bible.format.type is ComicFormat.STRIP and bible.format.panels:
        lim = replace(lim, strip_min_panels=bible.format.panels, strip_max_panels=bible.format.panels)
    return lim


def normalise_brief(brief: ComicBrief, allowed_presets: list[str]) -> ComicBrief:
    """Clamp model output to product limits. The model proposes; code decides."""
    s = get_settings()
    cat = styles()
    preset = brief.style_preset if brief.style_preset in allowed_presets else (
        cat.default if cat.default in allowed_presets else allowed_presets[0])  # fmt: skip
    fmt = brief.format
    pages = 1 if fmt is ComicFormat.STRIP else max(1, min(brief.pages, s.max_pages))
    lim = script_limits()
    panels = (max(lim.strip_min_panels, min(brief.panels, lim.strip_max_panels))
              if fmt is ComicFormat.STRIP and brief.panels else None)  # fmt: skip
    return brief.model_copy(update={
        "style_preset": preset, "pages": pages, "panels": panels,
        "named_characters": brief.named_characters[: s.max_characters],
    })  # fmt: skip


async def run_intake(prompt: str, answers: dict[str, str] | None, *, allowed_presets: list[str],
                     ctx: CallContext) -> IntakeResult:  # fmt: skip
    s = get_settings()
    cat = styles()
    default_style = cat.default if cat.default in allowed_presets else allowed_presets[0]
    inputs = {"prompt": prompt, "answers": answers, "default_pages": s.default_pages,
              "max_pages": s.max_pages, "default_style": default_style,
              "style_ids": allowed_presets, "max_characters": s.max_characters}  # fmt: skip
    system = render_prompt(
        "intake", default_pages=s.default_pages, max_pages=s.max_pages, default_style=default_style,
        style_ids=", ".join(allowed_presets), max_characters=s.max_characters,
        answers=json.dumps(answers) if answers else "",
    )  # fmt: skip
    result = await metering.structured(
        role="light",
        system=system,
        user=f"<user_idea>\n{prompt}\n</user_idea>",
        schema=IntakeResult,
        ctx=CallContext(
            "intake", ctx.user_id, ctx.project_id, prompt_version=PROMPT_VERSION, inputs=inputs
        ),  # fmt: skip
    )
    brief = normalise_brief(result.brief, allowed_presets)
    # At most three questions, one per id, and none once answers were given.
    seen: set[str] = set()
    questions = []
    for q in [] if answers else result.questions:
        if q.id not in seen and len(q.options) >= 2:
            seen.add(q.id)
            questions.append(q)
    return IntakeResult(brief=brief, questions=questions[:3])


def _dedupe_slugs(specs: list[CharacterSpec]) -> list[CharacterSpec]:
    out: list[CharacterSpec] = []
    used: set[str] = set()
    for spec in specs:
        slug = slugify(spec.id or spec.name)
        base, n = slug, 2
        while slug in used:
            slug = f"{base[:37]}_{n}"
            n += 1
        used.add(slug)
        out.append(spec.model_copy(update={"id": slug}))
    return out


async def write_bible_and_cast(brief: ComicBrief, ctx: CallContext) -> BibleAndCast:
    s = get_settings()
    result = await metering.structured(
        role="story",
        system=render_prompt("bible", max_characters=s.max_characters),
        user="ComicBrief:\n" + brief.model_dump_json(indent=1),
        schema=BibleAndCast,
        ctx=CallContext(
            "bible",
            ctx.user_id,
            ctx.project_id,
            prompt_version=PROMPT_VERSION,
            inputs={"brief": brief.model_dump(mode="json"), "max_characters": s.max_characters},
        ),  # fmt: skip
    )
    characters = _dedupe_slugs(result.characters[: s.max_characters])
    for c in characters:  # two or three signature features, as the design requires
        feats = [f for f in c.visual.signature_features if f.strip()][:3]
        c.visual.signature_features[:] = feats or ["distinctive silhouette", "bold colour accent"]
    locations = []
    used: set[str] = set()
    for loc in result.bible.locations[:MAX_LOCATIONS]:
        if loc.id not in used:
            used.add(loc.id)
            locations.append(loc)
    # Fields the brief already settled are copied from it, never taken from the model.
    bible: StoryBible = result.bible.model_copy(update={
        "audience": brief.audience, "style_preset": brief.style_preset,
        "format": FormatSpec(type=brief.format, pages=brief.pages, panels=brief.panels,
                             panels_per_page=(str(brief.panels) if brief.panels else "3-4")
                             if brief.format is ComicFormat.STRIP else "4-6"),
        "locations": locations, "cast": [c.id for c in characters],
    })  # fmt: skip
    return BibleAndCast(bible=bible, characters=characters)


@dataclass(frozen=True)
class ScriptOutcome:
    draft: ScriptDraft
    issues: list[ScriptIssue]
    repairs: int


async def write_script(bible: StoryBible, cast: list[CharacterSpec], ctx: CallContext) -> ScriptOutcome:
    """Generate, validate in code, and send back for targeted repair at most twice."""
    s = get_settings()
    lim = script_limits(bible)
    fmt, pages = bible.format.type, bible.format.pages
    character_ids = [c.id for c in cast]
    location_ids = [loc.id for loc in bible.locations]
    panel_rule = (
        f"The single page is one row of exactly {lim.strip_min_panels} panels."
        if fmt is ComicFormat.STRIP and lim.strip_min_panels == lim.strip_max_panels
        else f"The single page is one row of {lim.strip_min_panels} to {lim.strip_max_panels} panels."
        if fmt is ComicFormat.STRIP
        else f"{lim.page_min_panels} to {lim.page_max_panels} panels on every page."
    )
    system = render_prompt(
        "script", pages=pages, panel_rule=panel_rule, max_words=lim.max_words_per_balloon,
        max_balloons=lim.max_balloons_per_panel, max_per_panel=lim.max_characters_per_panel,
        character_ids=", ".join(character_ids), location_ids=", ".join(location_ids),
        audience=bible.audience.value,
    )  # fmt: skip
    base_user = ("Story Bible:\n" + bible.model_dump_json(indent=1) + "\n\nCast:\n"
                 + json.dumps([c.model_dump(mode="json") for c in cast], indent=1))  # fmt: skip
    inputs = {"format": fmt.value, "pages": pages, "panels": bible.format.panels, "character_ids": character_ids,
              "location_ids": location_ids}  # fmt: skip

    def check(draft: ScriptDraft) -> list[ScriptIssue]:
        return validate_script(draft.script, fmt=fmt, expected_pages=pages,
                               character_ids=set(character_ids), location_ids=set(location_ids),
                               limits=lim)  # fmt: skip

    user = base_user
    draft: ScriptDraft | None = None
    issues: list[ScriptIssue] = []
    for attempt in range(s.max_script_repairs + 1):
        draft = await metering.structured(
            role="story",
            system=system,
            user=user,
            schema=ScriptDraft,
            ctx=CallContext(
                "script" if attempt == 0 else "script_repair",
                ctx.user_id,
                ctx.project_id,
                attempt=attempt,
                prompt_version=PROMPT_VERSION,
                inputs={**inputs, "repair": attempt > 0},
            ),  # fmt: skip
        )
        issues = check(draft)
        if not has_errors(issues):
            return ScriptOutcome(draft, issues, attempt)
        errors = [i for i in issues if i.severity == "error"]
        user = (base_user + "\n\nYour previous script:\n" + draft.model_dump_json()
                + "\n\n" + render_prompt("script_repair", issues=errors))  # fmt: skip
    assert draft is not None
    # Still invalid after the bounded repairs: hand it to the user with the issues listed
    # rather than looping. Approval is refused while errors remain.
    return ScriptOutcome(draft, issues, s.max_script_repairs)


def audience_of(bible: StoryBible) -> AgeRating:
    return bible.audience
