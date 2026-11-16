"""Art Director: deterministic layout selection and panel prompt compilation.

No model is involved here. The same script, cast and catalog versions always produce the
same layout and byte-identical prompts, which is what makes prompt changes testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from storypanel.catalog import (
    PROMPT_VERSION,
    LayoutTemplate,
    layouts,
    prompt_sha256,
    render_prompt,
    styles,
)  # fmt: skip
from storypanel.domain.schemas import (
    CharacterSpec,
    ComicFormat,
    ContinuityRule,
    LocationSpec,
    PageSpec,
    PanelSpec,
)  # fmt: skip
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import nearest_aspect

SHOT_WORDS = {"wide": "Wide", "medium": "Medium", "close_up": "Close-up"}
# What each shot means for the framing, so the image model gets more than one word.
COMPOSITION = {
    "wide": "Show full figures small within the setting, with the surroundings clearly visible.",
    "medium": "Frame the characters from about the waist up.",
    "close_up": "The main character's head and shoulders fill most of the frame; little background.",
}
CLIMAX = " This is the climactic moment of the page: give it the most dramatic composition."


# ------------------------------------------------------------------ continuity rules
def rules_for_panel(
    rules: list[ContinuityRule], panel: PanelSpec
) -> tuple[list[ContinuityRule], list[ContinuityRule]]:
    """Visual rules that apply to one panel: (must, prefer). A character rule applies wherever
    that character is drawn; a location rule wherever the panel is set there. Rules the user
    excepted for this panel are left out (the exception is recorded in the script)."""
    present = {c.id for c in panel.characters}
    applies = [r for r in rules if r.visual and r.id not in panel.rule_exceptions and (
        r.applies_to == "every_panel"
        or (r.applies_to == "character" and r.target in present)
        or (r.applies_to == "location" and r.target == panel.location_id))]  # fmt: skip
    return [r for r in applies if r.strength == "must"], [r for r in applies if r.strength == "prefer"]


def rules_for_character(rules: list[ContinuityRule], character_id: str) -> list[ContinuityRule]:
    """Must-have visual rules about one character: part of the reference sheet."""
    return [r for r in rules if r.visual and r.strength == "must" and r.applies_to == "character"
            and r.target == character_id]  # fmt: skip


def _rule_text(r: ContinuityRule) -> str:
    return r.text.strip().rstrip(".") + "."


POSITION_PHRASES = {"left": "On the left", "center": "In the centre", "right": "On the right"}


# ------------------------------------------------------------------ layout
def choose_template(fmt: ComicFormat, page: PageSpec) -> LayoutTemplate:
    """Pick by panel count and where the emphasised panel falls in reading order."""
    count = len(page.panels)
    large = next((i for i, p in enumerate(page.panels) if p.emphasis == "large"), None)
    candidates = [t for t in layouts().values() if t.format is fmt and t.panel_count == count]
    if not candidates:
        raise AppError(ErrorCode.VALIDATION, f"No page layout for {count} panels.", retryable=False)
    if large is None:
        return next((t for t in candidates if t.large_index is None), candidates[0])
    with_large = [t for t in candidates if t.large_index is not None]
    if not with_large:
        return candidates[0]
    return min(with_large, key=lambda t: (abs((t.large_index or 0) - large), t.id))


def templates_for(fmt: ComicFormat, count: int) -> list[LayoutTemplate]:
    return sorted((t for t in layouts().values() if t.format is fmt and t.panel_count == count),
                  key=lambda t: t.id)  # fmt: skip


def panel_aspect(template: LayoutTemplate, slot: int) -> str:
    _, _, w, h = template.rects[slot]
    return nearest_aspect(w, h)


def balloon_zone(panel: PanelSpec) -> str:
    """Where the image model should leave room. Heads sit mid-frame, so text goes on top."""
    if not panel.dialogue:
        return "top edge"
    positions = {c.position for c in panel.characters}
    if positions == {"left"}:
        return "upper right"
    if positions == {"right"}:
        return "upper left"
    return "upper third"


# ------------------------------------------------------------------ references
def view_for(panel: PanelSpec) -> str:
    """Match the reference view to the shot (the side view for a profile shot)."""
    angle = panel.angle.lower()
    if "profile" in angle or "side" in angle:
        return "side"
    if "behind" in angle or "back" in angle or "over-the-shoulder" in angle:
        return "back"
    if panel.shot == "close_up":
        return "portrait"
    return "three_quarter"


VIEW_FALLBACKS = {
    "side": ("side", "three_quarter", "portrait"),
    "back": ("back", "three_quarter", "portrait"),
    "portrait": ("portrait", "front", "three_quarter"),
    "three_quarter": ("three_quarter", "portrait", "front"),
}


@dataclass(frozen=True)
class RefSlot:
    role: str  # character | location | continuity
    key: str  # character slug / location slug / previous panel id
    view: str | None
    asset_id: str


@dataclass(frozen=True)
class CompiledPanel:
    prompt: str
    prompt_version: str
    prompt_sha256: str
    aspect: str
    references: tuple[RefSlot, ...]
    inputs: dict[str, Any] = field(default_factory=dict)


def compile_panel(
    *, panel: PanelSpec, aspect: str, style_preset: str, cast: dict[str, CharacterSpec],
    locations: dict[str, LocationSpec], character_refs: dict[str, dict[str, str]],
    location_refs: dict[str, str], continuity: tuple[str, str] | None, max_references: int,
    fix_notes: tuple[str, ...] = (), rules: list[ContinuityRule] | None = None,
    panel_size: tuple[float, float] | None = None, prompt_version: str = PROMPT_VERSION,
) -> CompiledPanel:  # fmt: skip
    """The panel prompt in the given prompt version. v2 is kept byte-identical so the prompts of
    existing comics can be rebuilt and checked against their recorded hashes."""
    if prompt_version == "v2":
        return _compile_panel_v2(panel=panel, aspect=aspect, style_preset=style_preset, cast=cast,
                                 locations=locations, character_refs=character_refs, location_refs=location_refs,
                                 continuity=continuity, max_references=max_references, fix_notes=fix_notes,
                                 rules=rules)  # fmt: skip
    if prompt_version != "v3":
        raise AppError(ErrorCode.INVALID_STATE, f"Panel prompts {prompt_version} can't be compiled.", retryable=False)
    return _compile_panel_v3(panel=panel, aspect=aspect, style_preset=style_preset, cast=cast, locations=locations,
                             character_refs=character_refs, location_refs=location_refs, continuity=continuity,
                             max_references=max_references, fix_notes=fix_notes, rules=rules,
                             panel_size=panel_size or (600.0, 600.0))  # fmt: skip


def _compile_panel_v2(
    *, panel: PanelSpec, aspect: str, style_preset: str, cast: dict[str, CharacterSpec],
    locations: dict[str, LocationSpec], character_refs: dict[str, dict[str, str]],
    location_refs: dict[str, str], continuity: tuple[str, str] | None, max_references: int,
    fix_notes: tuple[str, ...] = (), rules: list[ContinuityRule] | None = None,
) -> CompiledPanel:  # fmt: skip
    """Prompts v2 (frozen). Fixed order: style token, shot, setting, characters, action, mood, constraints."""
    style = styles().get(style_preset)
    view = view_for(panel)
    slots: list[RefSlot] = []
    for ch in panel.characters:
        views = character_refs.get(ch.id, {})
        chosen = next((v for v in VIEW_FALLBACKS[view] if v in views), None)
        if chosen:
            slots.append(RefSlot("character", ch.id, chosen, views[chosen]))
    # Characters always keep their slots; location, then continuity, use what is left.
    slots = slots[:max_references]
    if panel.location_id in location_refs and len(slots) < max_references:
        slots.append(RefSlot("location", panel.location_id, None, location_refs[panel.location_id]))
    if continuity and len(slots) < max_references:
        slots.append(RefSlot("continuity", continuity[0], None, continuity[1]))
    index = {(s.role, s.key): n + 1 for n, s in enumerate(slots)}

    characters = []
    for ch in panel.characters:
        spec = cast[ch.id]
        characters.append({
            "position_phrase": POSITION_PHRASES[ch.position], "name": spec.name,
            "canonical_description": spec.canonical_description(),
            "ref": index.get(("character", ch.id)), "pose": ch.pose.strip().rstrip("."),
            "expression": ch.expression.strip().rstrip("."),
        })  # fmt: skip
    location = locations[panel.location_id]
    must, prefer = rules_for_panel(rules or [], panel)
    composition = COMPOSITION[panel.shot] + (CLIMAX if panel.emphasis == "large" else "")
    prompt = render_prompt(
        "panel", "v2", style_token=style.token, shot=SHOT_WORDS[panel.shot], composition=composition,
        must_rules=[_rule_text(r) for r in must], prefer_rules=[_rule_text(r) for r in prefer],
        angle=panel.angle.strip().rstrip("."), aspect=aspect,
        location_description=location.description.strip().rstrip("."),
        time_of_day=panel.time_of_day.strip().rstrip("."),
        location_ref=index.get(("location", panel.location_id)), characters=characters,
        action=panel.action.strip().rstrip("."), mood=panel.mood.strip().rstrip(".").capitalize(),
        continuity_ref=index.get(("continuity", continuity[0])) if continuity else None,
        fix_notes=list(fix_notes), balloon_zone=balloon_zone(panel),
    )  # fmt: skip
    inputs = {"kind": "panel", "characters": [
        {"id": ch.id, "position": ch.position, "palette": cast[ch.id].visual.palette,
         "signature_features": cast[ch.id].visual.signature_features}
        for ch in panel.characters], "required_rules": [r.id for r in must]}  # fmt: skip
    return CompiledPanel(prompt, "v2", prompt_sha256(prompt), aspect, tuple(slots), inputs)


# ------------------------------------------------------------------ v3 visual plan
VIEW_WORDS = {"portrait": "full-body front view", "front": "front view", "three_quarter": "three-quarter view",
              "side": "side view", "back": "back view"}  # fmt: skip
COMPOSITION_V3 = {
    "wide": "Full figures, small enough that the surroundings tell the reader where we are.",
    "medium": "Characters framed from about the waist up, close enough to read faces and hands.",
    "close_up": "{who} head and shoulders fill most of the frame; keep the background simple.",
}
CLIMAX_V3 = " This is the most important moment on the page: the strongest, clearest composition, larger figures."


def frame_words(aspect: str) -> str:
    """What the panel's real shape means for the composition (the slot in the page layout)."""
    a, b = (float(x) for x in aspect.split(":"))
    r = a / b
    if r >= 1.8:
        return f"a very wide {aspect} frame: spread the subjects across the width"
    if r >= 1.2:
        return f"a landscape {aspect} frame"
    if r > 0.83:
        return f"a square {aspect} frame"
    return f"a tall {aspect} frame: use the height (full figures, or foreground and background)"


@dataclass(frozen=True)
class LetteringPlan:
    """Where a panel's lettering is planned, shared by the image prompt and the letterer so the
    space the picture leaves free is the space the balloons go to. Normalised to the panel."""

    zone: tuple[float, float, float, float] | None
    phrase: str
    items: int


def lettering_plan(panel: PanelSpec, panel_size: tuple[float, float]) -> LetteringPlan:
    """Estimate how much room the panel's balloons and captions need at the normal lettering size,
    and put it above the first speaker (where the letterer places speech). Sound effects get no
    reserved space: they sit near the action. A panel without lettering reserves nothing."""
    from storypanel.pipeline import lettering

    items = [d for d in panel.dialogue if d.type != "sfx"]
    if not items:
        return LetteringPlan(None, "", 0)
    pw, ph = panel_size
    area = 0.0
    for d in items:
        max_w = pw * (0.92 if d.type == "caption" else 0.62)
        w, h = lettering._balloon_size(d.type, " ".join(d.text.split()), "comic_neue", lettering.FONT_MAX,
                                        max_w - (0 if d.type == "caption" else 8))  # fmt: skip
        area += min(w, max_w) * h * 1.25  # room around each balloon
    position = {c.id: c.position for c in panel.characters}
    sides = [position.get(d.speaker or "", "center") for d in items if d.type in ("speech", "thought")]
    if not sides:  # captions only: the letterer starts at the top-left corner
        x, width, where = 0.0, 1.0, "of the frame"
    elif len(set(sides)) > 1:
        x, width, where = 0.0, 1.0, "of the frame, above the speakers,"
    else:
        x, width, where = {"left": (0.0, 0.55, "of the left side of the frame, above the speaker,"),
                           "center": (0.2, 0.6, "of the middle of the frame, above the speaker,"),
                           "right": (0.45, 0.55, "of the right side of the frame, above the speaker,")}[sides[0]]  # fmt: skip
    height = min(0.45, max(0.14, area / (pw * ph) / width))
    pct = int(round(height * 20) * 5)
    noun = "caption" if not sides else "balloon"
    phrase = (f"keep the top {pct}% {where} free of faces, hands and important objects, with plain background "
              f"there (sky, wall or floor) for {len(items)} {noun if len(items) == 1 else noun + 's'}")
    return LetteringPlan((x, 0.0, width, height), phrase, len(items))


def _reference_slots(panel: PanelSpec, cast: dict[str, CharacterSpec], character_refs: dict[str, dict[str, str]],
                     location_refs: dict[str, str], continuity: tuple[str, str] | None,
                     max_references: int) -> tuple[list[RefSlot], list[str]]:  # fmt: skip
    """Characters first (every drawn character needs one: never dropped silently), then the location,
    then the previous panel. Returns the slots and what was left out, with the reason."""
    view = view_for(panel)
    slots: list[RefSlot] = []
    for ch in panel.characters:
        views = character_refs.get(ch.id, {})
        chosen = next((v for v in VIEW_FALLBACKS[view] if v in views), None)
        if chosen is None:
            raise AppError(ErrorCode.INVALID_STATE, f"{cast[ch.id].name} has no approved reference image, so this "
                                                    "panel can't be drawn consistently.", retryable=False)  # fmt: skip
        slots.append(RefSlot("character", ch.id, chosen, views[chosen]))
    if len(slots) > max_references:
        raise AppError(ErrorCode.VALIDATION, f"This panel has {len(slots)} characters, but the image model accepts "
                                             f"{max_references} reference images. Take a character out of the panel.",
                       retryable=False)  # fmt: skip
    omitted: list[str] = []
    if panel.location_id in location_refs:
        if len(slots) < max_references:
            slots.append(RefSlot("location", panel.location_id, None, location_refs[panel.location_id]))
        else:
            omitted.append("location: no reference slot left after the characters")
    if continuity:
        if len(slots) < max_references:
            slots.append(RefSlot("continuity", continuity[0], None, continuity[1]))
        else:
            omitted.append("previous panel: no reference slot left")
    return slots, omitted


def _compile_panel_v3(
    *, panel: PanelSpec, aspect: str, style_preset: str, cast: dict[str, CharacterSpec],
    locations: dict[str, LocationSpec], character_refs: dict[str, dict[str, str]],
    location_refs: dict[str, str], continuity: tuple[str, str] | None, max_references: int,
    fix_notes: tuple[str, ...], rules: list[ContinuityRule] | None, panel_size: tuple[float, float],
) -> CompiledPanel:  # fmt: skip
    """The panel's visual plan, in priority order: what each reference image is for, the panel's
    purpose, framing for its real shape, subjects (position, identity, pose, expression), action,
    required elements, setting and mood, focal point, corrections, and the planned lettering space."""
    style = styles().get(style_preset)
    slots, omitted = _reference_slots(panel, cast, character_refs, location_refs, continuity, max_references)
    index = {(s.role, s.key): n + 1 for n, s in enumerate(slots)}
    location = locations[panel.location_id]
    purposes = []
    for n, slot in enumerate(slots, start=1):
        if slot.role == "character":
            purposes.append({"n": n, "purpose": f"{cast[slot.key].name}'s identity ({VIEW_WORDS.get(slot.view or '', slot.view)}"
                                                f" from the approved sheet): face, body, proportions, colours, outfit and "
                                                "signature features; not the pose or the background"})  # fmt: skip
        elif slot.role == "location":
            purposes.append({"n": n, "purpose": f"the location, {location.name}: architecture, props, colours and light; "
                                                "not the camera angle or the composition"})  # fmt: skip
        else:
            purposes.append({"n": n, "purpose": "the previous panel, only to keep lighting, time of day and the state of "
                                                "objects consistent; choose a new camera position and composition for "
                                                "this panel"})  # fmt: skip
    characters = [{
        "position_phrase": POSITION_PHRASES[ch.position], "name": cast[ch.id].name,
        "canonical_description": cast[ch.id].canonical_description(), "ref": index.get(("character", ch.id)),
        "pose": ch.pose.strip().rstrip("."), "expression": ch.expression.strip().rstrip("."),
    } for ch in panel.characters]  # fmt: skip
    who = (cast[panel.characters[0].id].name + "'s") if panel.characters else "The subject's"
    composition = COMPOSITION_V3[panel.shot].format(who=who) + (CLIMAX_V3 if panel.emphasis == "large" else "")
    focus = panel.focus.strip().rstrip(".")
    if not focus and panel.shot == "close_up" and panel.characters:
        focus = f"{cast[panel.characters[0].id].name}'s face"
    plan = lettering_plan(panel, panel_size)
    must, prefer = rules_for_panel(rules or [], panel)
    prompt = render_prompt(
        "panel", "v3", style_token=style.token, references=purposes, purpose=panel.purpose.strip().rstrip("."),
        shot=SHOT_WORDS[panel.shot], angle=panel.angle.strip().rstrip("."), frame=frame_words(aspect),
        composition=composition, characters=characters, relative_scale=len(panel.characters) > 1,
        action=panel.action.strip().rstrip("."), must_rules=[_rule_text(r) for r in must],
        prefer_rules=[_rule_text(r) for r in prefer], location_description=location.description.strip().rstrip("."),
        time_of_day=panel.time_of_day.strip().rstrip("."), mood=panel.mood.strip().rstrip(".").capitalize(),
        focus=focus, fix_notes=list(fix_notes), lettering=plan.phrase,
    )  # fmt: skip
    inputs = {"kind": "panel", "characters": [
        {"id": ch.id, "position": ch.position, "palette": cast[ch.id].visual.palette,
         "signature_features": cast[ch.id].visual.signature_features}
        for ch in panel.characters], "required_rules": [r.id for r in must],
        "references_omitted": omitted, "lettering_zone": list(plan.zone) if plan.zone else None}  # fmt: skip
    return CompiledPanel(prompt, "v3", prompt_sha256(prompt), aspect, tuple(slots), inputs)


def compile_sheet_prompt(kind: str, spec: CharacterSpec, style_preset: str,
                         instruction: str | None = None,
                         rules: list[ContinuityRule] | None = None) -> tuple[str, dict[str, Any]]:  # fmt: skip
    style = styles().get(style_preset)
    palette = ", ".join(f"{k} {v}" for k, v in spec.visual.palette.items())
    ctx: dict[str, Any] = {"style_token": style.token, "name": spec.name,
                           "canonical_description": spec.canonical_description(), "palette": palette,
                           "must_rules": [_rule_text(r) for r in rules_for_character(rules or [], spec.id)]}  # fmt: skip
    if kind == "revise":
        ctx["instruction"] = instruction
        ctx.pop("palette")
    elif kind != "portrait":
        ctx.pop("palette")
    inputs = {"kind": kind, "characters": [{"id": spec.id, "position": "center",
                                            "palette": spec.visual.palette}]}  # fmt: skip
    return render_prompt(kind, **ctx), inputs


def compile_location_prompt(loc: LocationSpec, style_preset: str, palette: list[str]) -> str:
    return render_prompt("location", style_token=styles().get(style_preset).token,
                         description=loc.description.strip().rstrip("."),
                         palette=", ".join(palette))  # fmt: skip


def compile_edit_prompt(instruction: str, style_preset: str, characters: list[tuple[str, int]],
                        must_rules: list[ContinuityRule] | None = None) -> str:  # fmt: skip
    return render_prompt("panel_edit", style_token=styles().get(style_preset).token,
                         instruction=instruction.strip(),
                         characters=[{"name": n, "ref": r} for n, r in characters],
                         must_rules=[_rule_text(r) for r in must_rules or []])  # fmt: skip


def compile_reference_repair_prompt(spec: CharacterSpec, style_preset: str, fix_notes: tuple[str, ...],
                                    box: dict[str, float]) -> str:  # fmt: skip
    """Reference-conditioned repair: the edit model sees the panel and the approved portrait."""

    def pct(v: float) -> int:
        return round(100 * v)

    return render_prompt("repair_reference", style_token=styles().get(style_preset).token, name=spec.name,
                         canonical_description=spec.canonical_description(), fix_notes=list(fix_notes),
                         left=pct(box["x"]), right=pct(box["x"] + box["w"]), top=pct(box["y"]),
                         bottom=pct(box["y"] + box["h"]))  # fmt: skip


def compile_repair_prompt(spec: CharacterSpec, style_preset: str, fix_notes: tuple[str, ...]) -> str:
    """Prompt for repainting one character's region. Uses the canonical description so the
    repaired figure is described in exactly the words every other prompt uses."""
    return render_prompt("repair", style_token=styles().get(style_preset).token, name=spec.name,
                         canonical_description=spec.canonical_description(), fix_notes=list(fix_notes))  # fmt: skip
