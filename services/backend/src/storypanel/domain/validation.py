"""Deterministic pacing and reference rules enforced by code after script generation."""

from __future__ import annotations

from dataclasses import dataclass

from storypanel.domain.schemas import BeatSheet, ComicFormat, ComicScript, PanelSpec, ScriptIssue
from storypanel.pipeline.lettering import unsupported_chars, unsupported_message

# Server-side text limits for script fields (the browser enforces the same, but is not trusted).
FIELD_LIMITS = {"action": 500, "angle": 120, "mood": 120, "time_of_day": 120, "purpose": 200, "focus": 120}
CHARACTER_FIELD_LIMITS = {"pose": 120, "expression": 120}
MAX_BALLOON_CHARS = 300
MAX_WORD_CHARS = 24  # a longer unbroken word cannot be wrapped inside a balloon


@dataclass(frozen=True)
class ScriptLimits:
    max_pages: int = 8
    max_characters_per_panel: int = 3
    max_words_per_balloon: int = 25
    max_balloons_per_panel: int = 3
    page_min_panels: int = 4
    page_max_panels: int = 6
    strip_min_panels: int = 3
    strip_max_panels: int = 4


def word_count(text: str) -> int:
    return len(text.split())


def validate_script(
    script: ComicScript,
    *,
    fmt: ComicFormat,
    expected_pages: int,
    character_ids: set[str],
    location_ids: set[str],
    limits: ScriptLimits | None = None,
    beats: BeatSheet | None = None,
) -> list[ScriptIssue]:
    lim = limits or ScriptLimits()
    issues: list[ScriptIssue] = []

    def err(code: str, path: str, message: str, severity: str = "error") -> None:
        issues.append(ScriptIssue(code=code, path=path, message=message, severity=severity))  # type: ignore[arg-type]

    n_pages = len(script.pages)
    if n_pages == 0:
        err("no_pages", "pages", "The script has no pages.")
        return issues
    if n_pages > lim.max_pages:
        err("too_many_pages", "pages", f"At most {lim.max_pages} pages are supported.")
    if fmt is ComicFormat.STRIP and n_pages != 1:
        err("strip_single_page", "pages", "A strip is a single row of panels.")
    elif fmt is ComicFormat.WESTERN_PAGE and n_pages != expected_pages:
        err("page_count", "pages", f"Expected {expected_pages} pages, found {n_pages}.")

    lo, hi = (
        (lim.strip_min_panels, lim.strip_max_panels)
        if fmt is ComicFormat.STRIP
        else (lim.page_min_panels, lim.page_max_panels)
    )
    large_panels = 0
    for pi, page in enumerate(script.pages):
        ppath = f"pages[{pi}]"
        count = len(page.panels)
        if not lo <= count <= hi:
            err("panel_count", ppath, f"Page {pi + 1} has {count} panels; use {lo} to {hi}.")
        page_large = sum(1 for p in page.panels if p.emphasis == "large")
        large_panels += page_large
        if page_large > 1:
            err("emphasis", ppath, f"Page {pi + 1} has more than one large panel.")
        for ni, panel in enumerate(page.panels):
            path = f"{ppath}.panels[{ni}]"
            if not panel.action.strip():
                err("empty_action", f"{path}.action", "Describe what we see in this panel.")
            for name, limit in FIELD_LIMITS.items():
                if len(getattr(panel, name)) > limit:
                    err("text_too_long", f"{path}.{name}", f"Use at most {limit} characters here.")
            for ci, ch in enumerate(panel.characters):
                for name, limit in CHARACTER_FIELD_LIMITS.items():
                    if len(getattr(ch, name)) > limit:
                        err("text_too_long", f"{path}.characters[{ci}].{name}",
                            f"Use at most {limit} characters here.")  # fmt: skip
            if panel.location_id not in location_ids:
                err("unknown_location", f"{path}.location_id",
                    f"Location '{panel.location_id}' is not defined in the Story Bible.")  # fmt: skip
            if len(panel.characters) > lim.max_characters_per_panel:
                err("too_many_characters", f"{path}.characters",
                    f"At most {lim.max_characters_per_panel} characters may appear in a panel.")  # fmt: skip
            seen: set[str] = set()
            for ci, ch in enumerate(panel.characters):
                if ch.id not in character_ids:
                    err("unknown_character", f"{path}.characters[{ci}].id",
                        f"Character '{ch.id}' is not in the cast.")  # fmt: skip
                if ch.id in seen:
                    err("duplicate_character", f"{path}.characters[{ci}].id",
                        f"Character '{ch.id}' appears twice in one panel.")  # fmt: skip
                seen.add(ch.id)
            if len(panel.dialogue) > lim.max_balloons_per_panel:
                err("too_many_balloons", f"{path}.dialogue",
                    f"At most {lim.max_balloons_per_panel} balloons per panel.")  # fmt: skip
            for di, d in enumerate(panel.dialogue):
                dpath = f"{path}.dialogue[{di}]"
                if not d.text.strip():
                    err("empty_text", f"{dpath}.text", "Dialogue text is empty.")
                if len(d.text) > MAX_BALLOON_CHARS:
                    err(
                        "text_too_long",
                        f"{dpath}.text",
                        f"Use at most {MAX_BALLOON_CHARS} characters in a balloon.",
                    )
                long_words = [w for w in d.text.split() if len(w) > MAX_WORD_CHARS]
                if long_words:
                    err("word_too_long", f"{dpath}.text",
                        f"“{long_words[0][:30]}” is too long to fit on one line of a balloon. "
                        "Split it or use a shorter word.")  # fmt: skip
                missing = unsupported_chars(d.text)
                if missing:
                    err("unsupported_characters", f"{dpath}.text", unsupported_message(missing))
                if word_count(d.text) > lim.max_words_per_balloon:
                    err("balloon_too_long", f"{dpath}.text",
                        f"Balloon has {word_count(d.text)} words; the limit is "
                        f"{lim.max_words_per_balloon}.")  # fmt: skip
                if d.type in ("speech", "thought"):
                    if d.speaker is None:
                        err("missing_speaker", f"{dpath}.speaker", "Speech needs a speaker.")
                    elif d.speaker not in character_ids:
                        err("unknown_speaker", f"{dpath}.speaker",
                            f"Speaker '{d.speaker}' is not in the cast.")  # fmt: skip
                    elif d.speaker not in seen:
                        err("offscreen_speaker", f"{dpath}.speaker",
                            f"Speaker '{d.speaker}' is not drawn in this panel.", "warning")  # fmt: skip
    if fmt is ComicFormat.WESTERN_PAGE and n_pages >= 2 and large_panels == 0:
        err("no_climax_panel", "pages", "Mark one panel as large for the climax.", "warning")
    if fmt is ComicFormat.STRIP:
        for pi, page in enumerate(script.pages):
            for ni, panel in enumerate(page.panels[:-1]):
                if panel.emphasis == "large":
                    err("strip_large_not_last", f"pages[{pi}].panels[{ni}].emphasis",
                        "In a strip the wide slot is the last panel. This panel will be drawn at normal "
                        "size; mark the last panel as large instead, or leave both normal.", "warning")  # fmt: skip
    if beats is not None:
        issues += beat_coverage(script, beats)
    issues += story_checks(script, beats)
    return issues


def _words(text: str) -> list[str]:
    return [w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if w]


def story_checks(script: ComicScript, beats: BeatSheet | None = None) -> list[ScriptIssue]:
    """Deterministic storytelling warnings (never blocking): repeated lines, panels that would look
    interchangeable, framing that never changes, a last panel that doesn't carry the final beat, and
    panels without a stated purpose. Judgments that need reading comprehension (does the panel add
    something, does the lettering restate the art, does the ending pay off) are made by the Story
    Bible check instead."""
    out: list[ScriptIssue] = []
    flat = [(pi, ni, p) for pi, page in enumerate(script.pages) for ni, p in enumerate(page.panels)]
    multi = len(script.pages) > 1

    def where(pi: int, ni: int) -> str:
        return f"Page {pi + 1}, panel {ni + 1}" if multi else f"Panel {ni + 1}"

    said: dict[tuple[str, ...], tuple[int, int]] = {}
    for pi, ni, p in flat:
        for di, d in enumerate(p.dialogue):
            key = tuple(_words(d.text))
            if d.type == "sfx" or len(key) < 2:
                continue
            if key in said:
                first = where(*said[key])
                out.append(ScriptIssue(code="repeated_dialogue", path=f"pages[{pi}].panels[{ni}].dialogue[{di}].text",
                                       severity="warning",
                                       message=f"{where(pi, ni)} repeats a line from {first.lower() if not multi else first}. "
                                               "Cut it or say something new."))  # fmt: skip
            else:
                said[key] = (pi, ni)

    def look(p: PanelSpec) -> tuple[object, ...]:
        return (p.shot, p.location_id, tuple(sorted((c.id, c.position, c.pose.lower().strip(), c.expression.lower().strip())
                                                    for c in p.characters)))  # fmt: skip

    for (pi, ni, p), (qi, qn, q) in zip(flat, flat[1:], strict=False):
        if look(p) == look(q) or (p.shot == q.shot and p.location_id == q.location_id
                                  and {c.id for c in p.characters} == {c.id for c in q.characters}
                                  and _similar(p.action, q.action)):  # fmt: skip
            out.append(ScriptIssue(code="interchangeable_panels", path=f"pages[{qi}].panels[{qn}]", severity="warning",
                                   message=f"{where(pi, ni)} and the next panel would look almost the same (same shot, "
                                           "place, cast and poses). Change the action, framing or expression, or merge them."))  # fmt: skip
    for pi, page in enumerate(script.pages):
        shots = [(p.shot, p.angle.lower().strip()) for p in page.panels]
        if len(shots) >= 3 and len(set(shots)) == 1:
            out.append(ScriptIssue(code="unvaried_framing", path=f"pages[{pi}]", severity="warning",
                                   message=(f"Page {pi + 1}: every" if multi else "Every") + " panel uses the same shot and "
                                           "angle. Vary the framing where it helps the reader (for example, a close-up for "
                                           "the reaction)."))  # fmt: skip
    if beats is not None and beats.beats and any(p.beats for _, _, p in flat):
        last_beat = _norm(beats.beats[-1].name)
        pi, ni, last = flat[-1]
        if last_beat not in {_norm(b) for b in last.beats}:
            out.append(ScriptIssue(code="payoff_not_last", path=f"pages[{pi}].panels[{ni}].beats", severity="warning",
                                   message=f"The last panel doesn't show the final beat (“{beats.beats[-1].name}”), so the "
                                           "ending may not pay off the setup."))  # fmt: skip
    if any(p.purpose.strip() for _, _, p in flat):
        for pi, ni, p in flat:
            if not p.purpose.strip():
                out.append(ScriptIssue(code="purpose_missing", path=f"pages[{pi}].panels[{ni}].purpose", severity="warning",
                                       message=f"{where(pi, ni)} has no stated purpose: say what it adds for the reader."))  # fmt: skip
    return out


def _similar(a: str, b: str) -> bool:
    wa, wb = set(_words(a)), set(_words(b))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.8


def _norm(name: str) -> str:
    return " ".join(name.lower().split())


def beat_coverage(script: ComicScript, beats: BeatSheet) -> list[ScriptIssue]:
    """Declared coverage, checked deterministically: every beat is claimed by at least one panel,
    in order. Whether a panel really shows its beat is judged by the consistency check."""
    names = [_norm(b.name) for b in beats.beats]
    panels = [(pi, ni, p) for pi, page in enumerate(script.pages) for ni, p in enumerate(page.panels)]
    issues: list[ScriptIssue] = []
    if not any(p.beats for _, _, p in panels):
        return [ScriptIssue(code="beat_coverage_unchecked", path="beats", severity="warning",
                            message="Panels aren't tagged with beats, so beat coverage wasn't checked.")]  # fmt: skip
    claimed: set[str] = set()
    last = -1
    for pi, ni, p in panels:
        idx = []
        for tag in p.beats:
            if _norm(tag) in names:
                claimed.add(_norm(tag))
                idx.append(names.index(_norm(tag)))
            else:
                issues.append(ScriptIssue(code="unknown_beat", path=f"pages[{pi}].panels[{ni}].beats", severity="warning",
                                          message=f"“{tag}” isn't a beat in the beat sheet."))  # fmt: skip
        if idx and min(idx) < last:
            issues.append(ScriptIssue(code="beat_order", path=f"pages[{pi}].panels[{ni}].beats", severity="warning",
                                      message="This panel shows an earlier beat after a later one."))  # fmt: skip
        last = max([last, *idx])
    for b in beats.beats:
        if _norm(b.name) not in claimed:
            issues.append(ScriptIssue(code="beat_uncovered", path="beats",
                                      message=f"No panel shows the “{b.name}” beat. Tag a panel with it, or change the "
                                              "script so a panel shows it."))  # fmt: skip
    return issues


def has_errors(issues: list[ScriptIssue]) -> bool:
    return any(i.severity == "error" for i in issues)
