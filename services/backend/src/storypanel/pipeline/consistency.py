"""Story Bible consistency checks before any picture is paid for.

Script check: one metered structured call (light text model) judges, panel by panel, whether the
script keeps every `must` continuity rule and whether each panel shows the beats it is tagged
with (declared coverage is checked deterministically in domain/validation.py; this judges the
content). Verdicts become script issues:

- a violated rule: error (blocks approval) until the panel is edited or the user records that the
  rule does not apply to that panel (`rule_exceptions`, saved as a new script version);
- an uncertain rule, a beat the panel does not (clearly) show, a conflict between rules: review
  (approval needs an explicit, recorded acknowledgement);
- a check that could not run: review ("not checked"), never a silent pass.

Reference-sheet check: one metered vision call reviews the portrait and the turnaround against the
character's signature features and must-have rules. Problems are flagged for the user; nothing is
redrawn automatically.

The model's answers are judgments, not proof; they can be wrong in both directions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from storypanel.catalog import PROMPT_VERSION, render_prompt
from storypanel.domain.schemas import (
    BeatSheet,
    CharacterSpec,
    ComicScript,
    ContinuityRule,
    PanelSpec,
    ScriptConsistencyReport,
    ScriptIssue,
    SheetCheckReport,
)  # fmt: skip
from storypanel.providers import metering
from storypanel.providers.base import CallContext, ImageInput

CONSISTENCY_CODES = frozenset({
    "rule_violation", "rule_uncertain", "beat_not_expressed", "beat_uncertain", "rule_conflict",
    "consistency_incomplete", "consistency_unavailable", "panel_adds_nothing", "dialogue_restates_art",
    "weak_payoff", "reveal_too_early",
})  # fmt: skip


def rules_fingerprint(rules: list[ContinuityRule]) -> str:
    """Identifies the exact rules a check judged against (wording, scope and strength). Stored with
    sheet and panel check results; a different fingerprint now means the result is stale."""
    data = sorted((r.id, r.text, r.applies_to, r.target or "", r.strength, bool(r.visual)) for r in rules)
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:16]


def must_rules(rules: list[ContinuityRule]) -> list[ContinuityRule]:
    return [r for r in rules if r.strength == "must"]


def _applies(rule: ContinuityRule, panel: PanelSpec) -> bool:
    if rule.id in panel.rule_exceptions:
        return False
    if rule.applies_to == "character":
        return rule.target in {c.id for c in panel.characters}
    if rule.applies_to == "location":
        return rule.target == panel.location_id
    return True


def nothing_to_check(script: ComicScript, rules: list[ContinuityRule]) -> bool:
    return not must_rules(rules) and not any(p.beats for page in script.pages for p in page.panels)


async def check_script(script: ComicScript, beats: BeatSheet | None, rules: list[ContinuityRule],
                       ctx: CallContext) -> list[ScriptIssue]:  # fmt: skip
    """Raises AppError when the provider fails (the workflow retries, then records "not checked")."""
    musts = must_rules(rules)
    by_id = {r.id: r for r in musts}
    panels: list[dict[str, Any]] = []
    for pi, page in enumerate(script.pages):
        for ni, p in enumerate(page.panels):
            panels.append({
                "page": pi + 1, "panel": ni + 1, "action": p.action, "location": p.location_id,
                "characters": [c.id for c in p.characters],
                "dialogue": [f"{d.type}{' ' + d.speaker if d.speaker else ''}: {d.text}" for d in p.dialogue],
                "beats": p.beats, "rules": [r.id for r in musts if _applies(r, p)], "purpose": p.purpose,
                "elements": [f"{e.element} ({e.state})" for e in p.element_states],
                "excepted": [r for r in p.rule_exceptions if r in by_id],
            })  # fmt: skip
    lines = ["Continuity rules (must hold where they apply):"]
    lines += [f"- {r.id} [{r.applies_to}{': ' + r.target if r.target else ''}]: {r.text}" for r in musts] or [
        "- none"
    ]
    lines.append("Beat sheet:")
    lines += [f"- {b.name}: {b.summary}" for b in (beats.beats if beats else [])] or ["- none"]
    lines.append("Panels:")
    for d in panels:
        lines.append(f"- page {d['page']} panel {d['panel']} at {d['location']}, drawn: {', '.join(d['characters']) or 'nobody'}; "
                     f"action: {d['action']}; " + (f"purpose: {d['purpose']}; " if d["purpose"] else "")
                     + (f"story elements: {', '.join(d['elements'])}; " if d["elements"] else "")
                     + f"lettering: {' | '.join(d['dialogue']) or 'none'}; "
                     f"claims beats: {', '.join(d['beats']) or 'none'}; check rules: {', '.join(d['rules']) or 'none'}")  # fmt: skip
    report = await metering.structured(
        role="light", system=render_prompt("script_consistency"), user="\n".join(lines),
        schema=ScriptConsistencyReport,
        ctx=CallContext("script_consistency", ctx.user_id, ctx.project_id, prompt_version=PROMPT_VERSION,
                        inputs={"panels": panels, "rules": [r.id for r in musts]}),
    )  # fmt: skip
    return issues_from_report(report, script, musts)


def issues_from_report(report: ScriptConsistencyReport, script: ComicScript,
                       musts: list[ContinuityRule]) -> list[ScriptIssue]:  # fmt: skip
    by_id = {r.id: r for r in musts}
    seen: set[tuple[int, int]] = set()
    out: list[ScriptIssue] = []
    for pc in report.panels:
        pi, ni = pc.page - 1, pc.panel - 1
        if not (0 <= pi < len(script.pages) and 0 <= ni < len(script.pages[pi].panels)):
            continue  # the model numbered a panel that does not exist: ignored
        seen.add((pi, ni))
        panel = script.pages[pi].panels[ni]
        path = f"pages[{pi}].panels[{ni}]"
        where = (f"Page {pi + 1}, panel {ni + 1}" if len(script.pages) > 1 else f"Panel {ni + 1}") + ": "
        for v in pc.rules:
            rule = by_id.get(v.rule_id)
            if rule is None or not _applies(rule, panel):
                continue
            if v.verdict == "violated":
                out.append(ScriptIssue(code="rule_violation", path=f"{path}.rule:{rule.id}",
                                       message=f"{where}breaks the continuity rule “{rule.text}”: {v.reason} Edit the panel, "
                                               "or mark the rule as not applying to this panel."))  # fmt: skip
            elif v.verdict == "uncertain":
                out.append(ScriptIssue(code="rule_uncertain", path=f"{path}.rule:{rule.id}", severity="review",
                                       message=f"{where}can't tell whether it keeps “{rule.text}”: {v.reason}"))  # fmt: skip
        claimed = {b.lower().strip() for b in panel.beats}
        for b in pc.beats:
            if b.beat.lower().strip() not in claimed:
                continue
            if b.expressed == "no":
                out.append(ScriptIssue(code="beat_not_expressed", path=f"{path}.beat:{b.beat}", severity="review",
                                       message=f"{where}tagged with the “{b.beat}” beat, but it doesn't seem to show it: "
                                               f"{b.reason}"))  # fmt: skip
            elif b.expressed == "uncertain":
                out.append(ScriptIssue(code="beat_uncertain", path=f"{path}.beat:{b.beat}", severity="review",
                                       message=f"{where}can't tell whether it shows the “{b.beat}” beat: {b.reason}"))  # fmt: skip
        # Storytelling judgments are advice for the writer: shown, never required to approve.
        note = f" {pc.note.strip()}" if pc.note.strip() else ""
        if pc.adds == "no":
            out.append(ScriptIssue(code="panel_adds_nothing", path=path, severity="warning",
                                   message=f"{where}doesn't seem to change anything for the reader.{note} Give it new "
                                           "information, action or emotion, or merge it with a neighbour."))  # fmt: skip
        if pc.reveals_early:
            out.append(ScriptIssue(code="reveal_too_early", path=path, severity="warning",
                                   message=f"{where}gives away what the story reveals later.{note} Adjust the action, or "
                                           "set the element's state for this panel (absent, hidden or unnoticed) so the "
                                           "reveal still lands."))  # fmt: skip
        if pc.restates_art and panel.dialogue:
            out.append(ScriptIssue(code="dialogue_restates_art", path=f"{path}.dialogue", severity="warning",
                                   message=f"{where}the lettering only describes what the picture already shows.{note} "
                                           "Use it for what the picture can't show (a thought, a joke, a voice)."))  # fmt: skip
    for pi, page in enumerate(script.pages):
        for ni, panel in enumerate(page.panels):
            needs = [r for r in musts if _applies(r, panel)] or panel.beats
            if needs and (pi, ni) not in seen:
                label = f"Page {pi + 1}, panel {ni + 1}" if len(script.pages) > 1 else f"Panel {ni + 1}"
                out.append(ScriptIssue(code="consistency_incomplete", path=f"pages[{pi}].panels[{ni}]", severity="review",
                                       message=f"{label} wasn't covered by the Story Bible check."))  # fmt: skip
    if report.payoff in ("weak", "no"):
        reason = f" {report.payoff_reason.strip()}" if report.payoff_reason.strip() else ""
        out.append(ScriptIssue(code="weak_payoff", path="ending", severity="warning",
                               message=("The ending doesn't pay off the setup." if report.payoff == "no" else
                                        "The ending only weakly pays off the setup.") + reason))  # fmt: skip
    for n, c in enumerate(report.conflicts):
        names = ", ".join(f"“{by_id[i].text}”" for i in c.rule_ids if i in by_id) or "the rules"
        out.append(ScriptIssue(code="rule_conflict", path=f"continuity_rules[{n}]", severity="review",
                               message=f"{ {'rule_vs_rule': 'Rules conflict', 'rule_vs_beat': 'A beat requires breaking a rule', 'impossible': 'A rule cannot be shown in a picture as worded'}[c.kind]}: "
                                       f"{names}. {c.description}"))  # fmt: skip
    return out


def unavailable_issue(reason: str = "") -> ScriptIssue:
    return ScriptIssue(code="consistency_unavailable", path="script", severity="review",
                       message="This version wasn't checked against the Story Bible" + (f" ({reason})" if reason else "")
                               + ". You can approve it anyway; that choice is recorded.")  # fmt: skip


def sheet_items(spec: CharacterSpec, rules: list[ContinuityRule]) -> list[tuple[str, str]]:
    from storypanel.pipeline.art import rules_for_character

    return ([("signature_feature", f) for f in spec.visual.signature_features]
            + [("rule", r.text) for r in rules_for_character(rules, spec.id)])  # fmt: skip


async def check_sheet(spec: CharacterSpec, rules: list[ContinuityRule], portrait: bytes, turnaround: bytes,
                      ctx: CallContext, expressions: bytes | None = None) -> dict[str, Any]:  # fmt: skip
    """Review result for one sheet version: status ok | flagged, with the item verdicts."""
    items = sheet_items(spec, rules)
    report = await metering.structured(
        role="vision", system=render_prompt("sheet_check"),
        user=f"Character: {spec.name}. Must show:\n" + "\n".join(f"- ({k}) {t}" for k, t in items),
        schema=SheetCheckReport,
        images=(ImageInput(portrait, label="Hero portrait:"), ImageInput(turnaround, label="Turnaround sheet:"),
                *([ImageInput(expressions, label="Expression sheet (close-ups of the face):")] if expressions else [])),
        ctx=CallContext("sheet_check", ctx.user_id, ctx.project_id, target_id=spec.id, prompt_version=PROMPT_VERSION,
                        inputs={"items": [t for _, t in items], "character": spec.id}),
    )  # fmt: skip
    asked = {t for _, t in items}
    found = [i for i in report.items if i.item in asked]
    missing = asked - {i.item for i in found}
    # Only a continuity rule can be situational; a signature feature must always be on the sheet.
    situational = [i.model_dump() for i in found if i.kind == "rule" and i.visible == "situational"
                   and i.depicted_correctly != "no"]  # fmt: skip
    # A signature feature marked situational, or a situational rule drawn wrong: judged normally.
    found = [i.model_copy(update={"visible": "uncertain"}) if i.visible == "situational" else i
             for i in found if i.model_dump() not in situational]  # fmt: skip
    problems = [i.model_dump() for i in found if "no" in (i.visible, i.depicted_correctly)]
    unsure = [
        i.model_dump()
        for i in found
        if "uncertain" in (i.visible, i.depicted_correctly) and i.model_dump() not in problems
    ]
    unsure += [{"item": m, "kind": "rule", "visible": "uncertain", "depicted_correctly": "uncertain",
                "note": "Not reported by the check."} for m in sorted(missing)]  # fmt: skip
    flagged = bool(problems or unsure or report.whole_figures != "yes")
    from storypanel.pipeline.art import rules_for_character

    return {"status": "flagged" if flagged else "ok", "problems": problems, "uncertain": unsure,
            "situational": situational, "rules_sha": rules_fingerprint(rules_for_character(rules, spec.id)),
            "whole_figures": report.whole_figures, "notes": report.notes[:4]}  # fmt: skip
