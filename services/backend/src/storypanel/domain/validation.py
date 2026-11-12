"""Deterministic pacing and reference rules enforced by code after script generation."""

from __future__ import annotations

from dataclasses import dataclass

from storypanel.domain.schemas import ComicFormat, ComicScript, ScriptIssue
from storypanel.pipeline.lettering import unsupported_chars, unsupported_message


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
    return issues


def has_errors(issues: list[ScriptIssue]) -> bool:
    return any(i.severity == "error" for i in issues)
