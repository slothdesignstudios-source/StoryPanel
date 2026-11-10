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
from storypanel.domain.schemas import CharacterSpec, ComicFormat, LocationSpec, PageSpec, PanelSpec
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import nearest_aspect

SHOT_WORDS = {"wide": "Wide", "medium": "Medium", "close_up": "Close-up"}
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
    fix_notes: tuple[str, ...] = (),
) -> CompiledPanel:  # fmt: skip
    """Fixed order: style token, shot, setting, characters, action, mood, constraints."""
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
    prompt = render_prompt(
        "panel", style_token=style.token, shot=SHOT_WORDS[panel.shot],
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
        for ch in panel.characters]}  # fmt: skip
    return CompiledPanel(prompt, PROMPT_VERSION, prompt_sha256(prompt), aspect, tuple(slots), inputs)


def compile_sheet_prompt(kind: str, spec: CharacterSpec, style_preset: str,
                         instruction: str | None = None) -> tuple[str, dict[str, Any]]:  # fmt: skip
    style = styles().get(style_preset)
    palette = ", ".join(f"{k} {v}" for k, v in spec.visual.palette.items())
    ctx: dict[str, Any] = {"style_token": style.token, "name": spec.name,
                           "canonical_description": spec.canonical_description(),
                           "palette": palette}  # fmt: skip
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


def compile_edit_prompt(instruction: str, style_preset: str,
                        characters: list[tuple[str, int]]) -> str:  # fmt: skip
    return render_prompt("panel_edit", style_token=styles().get(style_preset).token,
                         instruction=instruction.strip(),
                         characters=[{"name": n, "ref": r} for n, r in characters])  # fmt: skip
