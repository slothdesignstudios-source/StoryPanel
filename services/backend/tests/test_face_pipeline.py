"""The automatic face-position path, end to end, on real artwork without paid calls:

    panel check (VisionQAReport boxes, fractions of the GENERATED image)
    -> qa.evaluate_panel -> stored qa_scores["head_boxes"]
    -> stages_art.letter_panel (image -> panel mapping through the renderer's crop)
    -> lettering.layout_panel -> render.render_page (PNG) and render.build_pdf (PDF)

The panel check's answer is SIMULATED here (boxes written in, standing in for the model): this proves
the plumbing, the coordinate conversion and the handling of bad boxes, not the model's accuracy.
Artwork: real Train panels from the user's export (tests/fixtures/real/). The face positions used to
write the simulated answers were read off that art by eye.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from storypanel.domain.schemas import AgeRating, CharacterSpec, PanelSpec, SafetyVerdict, VisionQAReport
from storypanel.pipeline import lettering, qa, render
from storypanel.pipeline.stages_art import _export_flags, letter_panel
from storypanel.providers.base import CallContext
from storypanel.providers.openai_image import size_for

REAL = Path(__file__).parent / "fixtures" / "real"
PAGE = (1000, 1500)


def char(cid: str, name: str) -> CharacterSpec:
    return CharacterSpec.model_validate({
        "id": cid, "name": name, "role": "lead", "personality": "warm", "voice": "soft",
        "visual": {"type": "teen", "body": "slim", "head": "round", "hair": "dark", "outfit": "cardigan",
                   "palette": {"a": "#fff"}, "signature_features": ["sketchbook"]}})  # fmt: skip


CAST = {"mira": char("mira", "Mira"), "teo": char("teo", "Teo")}
# Train panels (w6_bottom_large): rect, the image size the app requests for that slot (OpenAI, long
# edge 1536), and faces read off the real art (fractions of the PANEL as rendered).
P4 = dict(rect=(355.0, 468.0, 291.0, 404.0), aspect="3:4",
          heads={"mira": (0.05, 0.40, 0.32, 0.18), "teo": (0.58, 0.13, 0.30, 0.20)},
          dialogue=[("speech", "teo", "Delayed! I can make it!")])  # fmt: skip
P6 = dict(rect=(40.0, 896.0, 920.0, 565.0), aspect="16:9",
          heads={"mira": (0.24, 0.31, 0.11, 0.13), "teo": (0.88, 0.05, 0.06, 0.13)},
          dialogue=[("speech", "mira", "Tomorrow... same table?")])  # fmt: skip


def image_size(aspect: str) -> tuple[int, int]:
    w, h = size_for(aspect).split("x")
    return int(w), int(h)


def panel_to_image(
    box: tuple[float, ...], isize: tuple[int, int], psize: tuple[float, float]
) -> dict[str, float]:
    """Where a face seen in the rendered panel sits in the full generated image (the inverse of the crop)."""
    iw, ih = isize
    pw, ph = psize
    scale = max(pw / iw, ph / ih)
    rw, rh = iw * scale, ih * scale
    left, top = (rw - pw) / 2, (rh - ph) / 2
    x, y, w, h = box
    return {"x": (x * pw + left) / rw, "y": (y * ph + top) / rh, "w": w * pw / rw, "h": h * ph / rh}


def spec_for(p: dict[str, Any]) -> PanelSpec:
    return PanelSpec.model_validate({
        "shot": "medium", "angle": "eye-level", "location_id": "cafe", "time_of_day": "day",
        "characters": [{"id": "mira", "position": "left", "pose": "p", "expression": "e"},
                       {"id": "teo", "position": "right", "pose": "p", "expression": "e"}],
        "action": "a", "mood": "m", "dialogue": [{"type": t, "speaker": s, "text": x} for t, s, x in p["dialogue"]]})  # fmt: skip


async def checked_scores(monkeypatch: pytest.MonkeyPatch, heads: dict[str, Any]) -> dict[str, Any]:
    """Run the real QA scoring on a simulated panel-check answer and return the scores it stores."""
    from storypanel.providers import metering

    report = VisionQAReport.model_validate({
        "character_count": 2, "action_matches": True, "visible_text": False, "anatomy_defects": [], "fix_notes": [],
        "characters": [{"id": cid, "present": True, "missing_features": [], "identity_rating": 5,
                        **({"head_box": heads[cid]} if heads.get(cid) is not None else {})} for cid in ("mira", "teo")]})  # fmt: skip

    async def structured(*, schema: Any, **_: Any) -> Any:
        return (
            SafetyVerdict(allowed=True, categories=[], severity="none") if schema is SafetyVerdict else report
        )

    monkeypatch.setattr(metering, "structured", structured)
    monkeypatch.setattr(qa.get_settings(), "identity_qa", "none", raising=False)
    out = await qa.evaluate_panel(image=b"", panel=spec_for(P4), cast=CAST, audience=AgeRating.ALL_AGES,
                                  reference_images={}, reference_embeddings={}, ctx=CallContext("panel_qa", "u", "p"))  # fmt: skip
    return out.scores | {"head_boxes": out.head_boxes}


def face_share(b: lettering.Placed, rect: tuple[float, ...], head: tuple[float, ...]) -> float:
    h = (rect[0] + head[0] * rect[2], rect[1] + head[1] * rect[3], head[2] * rect[2], head[3] * rect[3])
    return lettering.overlap((b.x, b.y, b.w, b.h), h) / (h[2] * h[3])


@pytest.mark.parametrize("p", [P4, P6], ids=["p4", "p6"])
async def test_detected_faces_reach_the_letterer_in_panel_coordinates(
    monkeypatch: pytest.MonkeyPatch, p: dict[str, Any]
) -> None:
    isize = image_size(p["aspect"])
    rect = p["rect"]
    heads_img = {k: panel_to_image(v, isize, (rect[2], rect[3])) for k, v in p["heads"].items()}
    scores = await checked_scores(monkeypatch, heads_img)
    assert scores["faces_unlocated"] == []
    placed, basis = letter_panel(spec=spec_for(p), rect=rect, page_size=PAGE, scores=scores, image_size=isize,
                                 prompt_version="v4", kind="generate", font="comic_neue", version_id="v-1")  # fmt: skip
    assert basis == {"version_id": "v-1", "faces": "located", "located": ["mira", "teo"], "box_problems": {}}
    for b in placed:
        assert all(face_share(b, rect, h) < 0.15 for h in p["heads"].values()), b.flags
        assert not {
            "face_position_estimated",
            "tail_target_estimated",
            "covers_face",
            "tail_ambiguous",
        } & set(b.flags)
        speaker = lettering._abs(dict(zip("xywh", p["heads"][b.speaker], strict=True)), rect)  # type: ignore[arg-type]
        assert speaker is not None and b.tail is not None
        assert lettering._dist_to(b.tail, speaker) < 0.05 * rect[2]  # the tail reaches its speaker


async def test_boxes_used_without_the_crop_would_be_misplaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversion matters: a 16:9 image fills panel 6 (920 x 565) with ~4.6% trimmed off each side."""
    isize = image_size("16:9")
    rect = P6["rect"]
    teo = panel_to_image(P6["heads"]["teo"], isize, (rect[2], rect[3]))
    mapped, problem = lettering.image_box_to_panel(teo, isize, (rect[2], rect[3]))
    assert problem is None and mapped is not None and abs(mapped["x"] - 0.88) < 0.002
    raw_x = teo["x"]  # what the letterer used before: the image fraction taken as a panel fraction
    assert abs(raw_x - 0.88) > 0.03


async def test_missing_faces_are_still_reported_as_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original failure (Train p4 as exported): no boxes, a balloon over Teo's face. Kept covered."""
    scores = await checked_scores(monkeypatch, {"mira": None, "teo": None})
    assert scores["faces_unlocated"] == ["mira", "teo"]
    rect = P4["rect"]
    placed, basis = letter_panel(spec=spec_for(P4), rect=rect, page_size=PAGE, scores=scores, image_size=image_size("3:4"),
                                 prompt_version="v4", kind="generate", font="comic_neue", version_id="v-1")  # fmt: skip
    assert basis["faces"] == "guessed"
    b = placed[0]
    assert face_share(b, rect, P4["heads"]["teo"]) > 0.5  # still on his face: a guess can't avoid it
    assert "face_position_estimated" in b.flags and "covers_face" not in b.flags


@pytest.mark.parametrize(("box", "problem"), [
    ({"x": "left", "y": 0.1, "w": 0.2, "h": 0.2}, "box_malformed"),
    ({"x": 0.1, "y": 0.1}, "box_malformed"),
    ({"x": 0.1, "y": 0.1, "w": -0.2, "h": 0.2}, "box_out_of_range"),
    ({"x": 45, "y": 12, "w": 20, "h": 18}, "box_out_of_range"),  # percentages or pixels, not fractions
    ({"x": 0.9, "y": 0.9, "w": 0.4, "h": 0.4}, "box_out_of_range"),
])  # fmt: skip
def test_bad_boxes_are_refused_and_the_face_counts_as_not_located(box: dict[str, Any], problem: str) -> None:
    scores = {
        "head_boxes": {
            "teo": box,
            "mira": panel_to_image(P4["heads"]["mira"], image_size("3:4"), (291.0, 404.0)),
        }
    }
    placed, basis = letter_panel(spec=spec_for(P4), rect=P4["rect"], page_size=PAGE, scores=scores,
                                 image_size=image_size("3:4"), prompt_version="v4", kind="generate",
                                 font="comic_neue", version_id="v-1")  # fmt: skip
    assert basis["box_problems"] == {"head:teo": problem} and basis["faces"] == "partial"
    assert "tail_target_estimated" in placed[0].flags  # Teo's face is treated as guessed, never trusted


def test_an_unknown_image_size_is_recorded() -> None:
    scores = {"head_boxes": {k: dict(zip("xywh", v, strict=True)) for k, v in P4["heads"].items()}}
    _, basis = letter_panel(spec=spec_for(P4), rect=P4["rect"], page_size=PAGE, scores=scores, image_size=None,
                            prompt_version="v4", kind="generate", font="comic_neue", version_id="v-1")  # fmt: skip
    assert basis["box_problems"] == {"head:mira": "image_size_unknown", "head:teo": "image_size_unknown"}


def test_a_face_cropped_out_of_the_panel_is_not_used() -> None:
    mapped, problem = lettering.image_box_to_panel(
        {"x": 0.0, "y": 0.2, "w": 0.02, "h": 0.1}, (1536, 864), (920.0, 565.0)
    )
    assert mapped is None and problem == "cropped_out"


def test_balloons_placed_for_another_picture_are_marked_stale_at_export() -> None:
    from storypanel.db.models import Balloon

    b = Balloon(flags=[], style={"basis": {"version_id": "old"}})
    assert _export_flags(b, "new") == ["lettering_stale"]
    assert _export_flags(b, "old") == []
    assert (
        _export_flags(Balloon(flags=[], style={}), "new") == []
    )  # placed before bases were recorded: unknown


def pdf_words(pdf: Path) -> str:
    return subprocess.run(
        ["pdftotext", "-bbox", str(pdf), "-"], capture_output=True, text=True, check=True
    ).stdout


async def test_png_and_pdf_draw_the_balloon_where_the_letterer_put_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if shutil.which("pdftotext") is None:
        pytest.skip("pdftotext not installed")
    isize = image_size("3:4")
    rect = P4["rect"]
    scores = await checked_scores(
        monkeypatch, {k: panel_to_image(v, isize, (rect[2], rect[3])) for k, v in P4["heads"].items()}
    )
    placed, _ = letter_panel(spec=spec_for(P4), rect=rect, page_size=PAGE, scores=scores, image_size=isize,
                             prompt_version="v4", kind="generate", font="comic_neue", version_id="v-1")  # fmt: skip
    art = (REAL / "train-p4.jpg").read_bytes()
    balloons = tuple(
        {"type": b.type, "text": b.text, "geometry": b.geometry(), "order_index": b.index} for b in placed
    )
    doc = render.PageDoc(PAGE, (render.PanelDoc(rect, art, balloons),))
    png = render.render_page(doc, 1000)
    assert Image.open(io.BytesIO(png)).size == (1000, 1500)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(render.build_pdf([doc], [render.render_page(doc, 1000, lettering=False)], "t"))
    import asyncio
    import re

    html = await asyncio.to_thread(pdf_words, pdf)

    words = [(float(a), float(b), float(c), float(d)) for a, b, c, d, t in re.findall(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)', html) if t in ("Delayed!", "it!")]  # fmt: skip
    assert words
    pt = 432.0 / 1000  # page unit -> PDF point; 9 pt bleed
    b = placed[0]
    cx = (min(w[0] for w in words) + max(w[2] for w in words)) / 2
    cy = (min(w[1] for w in words) + max(w[3] for w in words)) / 2
    assert abs(cx - (9 + (b.x + b.w / 2) * pt)) < 6 and abs(cy - (9 + (b.y + b.h / 2) * pt)) < 6


@pytest.mark.workflow
async def test_lettering_follows_a_new_picture_but_never_moves_a_hand_placed_balloon(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    import uuid

    from sqlalchemy import select

    from storypanel.db.models import Balloon, Panel
    from storypanel.db.session import transaction
    from storypanel.pipeline import stages_art
    from tests.conftest import run_to_completion

    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )
    page_id = uuid.UUID(p["pages"][0]["id"])
    async with transaction() as db:
        panel = (
            (await db.execute(select(Panel).where(Panel.page_id == page_id).order_by(Panel.index)))
            .scalars()
            .first()
        )
        assert panel is not None
        balloons = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel.id))).scalars())
        assert balloons and all(
            b.style["basis"]["version_id"] == str(panel.current_version_id) for b in balloons
        )
        for b in balloons:  # as if the picture had changed since they were placed
            b.style = {"basis": {**b.style["basis"], "version_id": "an-older-picture"}}
        panel_id = panel.id
    await stages_art.stage_letter_page(page_id)
    async with transaction() as db:
        fresh = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel_id))).scalars())
        current = (
            await db.execute(select(Panel.current_version_id).where(Panel.id == panel_id))
        ).scalar_one()
        assert fresh and all(b.style["basis"]["version_id"] == str(current) for b in fresh)  # placed again
        for b in fresh:
            b.style, b.user_edited = {"basis": {**b.style["basis"], "version_id": "an-older-picture"}}, True
        kept = {b.id: b.geometry for b in fresh}
    await stages_art.stage_letter_page(page_id)
    async with transaction() as db:
        after = list((await db.execute(select(Balloon).where(Balloon.panel_id == panel_id))).scalars())
        assert {b.id: b.geometry for b in after} == kept  # never moved
        assert all("lettering_stale" in b.flags for b in after)  # but flagged for the user


@pytest.mark.workflow
async def test_the_face_check_stops_before_a_call_that_would_cross_its_cap(
    client: Any, headers: dict[str, str], worker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scripts/face-check.py: with a cap below one call's reservation, no call is made at all."""
    import json as _json
    import uuid
    from decimal import Decimal

    from storypanel.config import get_settings
    from storypanel.pipeline import qa as qa_mod
    from tests.conftest import run_to_completion

    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )
    fc = _face_check_module()
    calls: list[Any] = []

    async def no_call(**kw: Any) -> Any:
        calls.append(kw)
        raise AssertionError("a call was made")

    monkeypatch.setattr(qa_mod, "vision_report", no_call)
    monkeypatch.setattr(get_settings(), "provider_mode", "live", raising=False)
    monkeypatch.setattr(fc, "reservation", lambda data, r: Decimal("0.02"))
    out = tmp_path / "fc"
    out.mkdir()
    assert await fc.run(uuid.UUID(p["id"]), out, Decimal("0.01"), None, None) == 0
    summary = _json.loads((out / "face-check.json").read_text())
    assert calls == [] and summary["totals"]["calls"] == 0
    assert summary["stopped"].startswith("stopped before page 1 panel 1")


def _face_check_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "face_check", Path(__file__).resolve().parents[3] / "scripts/face-check.py"
    )
    assert spec and spec.loader
    fc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fc)
    return fc


@pytest.mark.workflow
async def test_the_face_check_budget_is_shared_by_both_comics_and_every_rerun(
    client: Any,
    headers: dict[str, str],
    worker: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Earlier stage-1A spend (another comic, an earlier or interrupted run) counts against the same
    USD 0.20: a rerun never starts from zero."""
    import json as _json
    import uuid
    from decimal import Decimal

    from storypanel.config import get_settings
    from storypanel.pipeline import qa as qa_mod
    from tests.conftest import run_to_completion

    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )
    fc = _face_check_module()
    assert (await fc.stage_spent())[0] >= 0  # the real ledger query runs
    calls: list[Any] = []

    async def no_call(**kw: Any) -> Any:
        calls.append(kw)
        raise AssertionError("a call was made")

    async def earlier_spend() -> tuple[Decimal, int]:
        return Decimal("0.195"), 9  # e.g. the Train run, then a retry; 0.195 + 0.0091 > 0.20

    monkeypatch.setattr(qa_mod, "vision_report", no_call)
    monkeypatch.setattr(get_settings(), "provider_mode", "live", raising=False)
    monkeypatch.setattr(fc, "reservation", lambda data, r: Decimal("0.0091"))
    monkeypatch.setattr(fc, "stage_spent", earlier_spend)
    assert (
        await fc.preflight(uuid.UUID(p["id"])) == 1
    )  # no Anthropic key here, and the budget is nearly spent
    shown = capsys.readouterr().out
    assert "remaining USD 0.0050" in shown and "paid checks allowed on this comic: yes" in shown
    assert "preflight: FAIL" in shown and "sk-" not in shown
    out = tmp_path / "fc"
    out.mkdir()
    assert await fc.run(uuid.UUID(p["id"]), out, Decimal("0.20"), None, None) == 0
    summary = _json.loads((out / "face-check.json").read_text())
    assert calls == []
    assert "all runs" in summary["stopped"] and summary["stage_1a_cumulative"]["remaining_usd"] == "0.005"


def test_a_flagged_defect_is_detection_not_placement_and_no_annotation_means_unavailable() -> None:
    from storypanel.pipeline.lettering import Placed

    fc = _face_check_module()
    rect = (0.0, 0.0, 400.0, 300.0)
    face = {"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.3}

    def balloon(flags: list[str], x: float = 10, y: float = 10) -> Any:
        return Placed(index=0, type="speech", text="Hello", speaker="mira", x=x, y=y, w=80, h=40, font_size=12.0, tail=None,
                      flags=flags)  # fmt: skip

    flagged = fc.evaluate(
        [balloon(["covers_face"])], {"mira": face}, {"heads": {"mira": face}}, rect, ["mira"], []
    )
    assert (
        flagged["outcome"] == "unresolved_defect_detected" and not flagged["counts_as_successful_placement"]
    )
    clean = fc.evaluate([balloon([])], {"mira": face}, {"heads": {"mira": face}}, rect, ["mira"], [])
    assert clean["outcome"] == "clean_automatic_placement" and clean["counts_as_successful_placement"]
    unannotated = fc.evaluate([balloon([])], {"mira": face}, None, rect, ["mira"], [])
    assert unannotated["outcome"] == "clean_unverified" and not unannotated["counts_as_successful_placement"]
    assert unannotated["measurements"].startswith("unavailable")
    guessed = fc.evaluate([balloon(["face_position_estimated"])], {}, None, rect, ["mira"], ["mira"])
    assert guessed["outcome"] == "uncertain_faces_not_located"
    # a balloon squarely on the hand-placed face, with no flag: a missed defect, never "clean"
    missed = fc.evaluate(
        [balloon([], x=160, y=95)], {"mira": face}, {"heads": {"mira": face}}, rect, ["mira"], []
    )
    assert missed["outcome"] == "missed_defect"
    counts = fc.outcome_counts(
        [{"page": 1, "panel": 3, "evaluation": flagged}, {"page": 1, "panel": 4, "evaluation": clean}]
    )
    assert counts["clean_automatic_placement"] == ["p1-4"]
    assert counts["unresolved_defect_detected (detection, not placement)"] == ["p1-3"]


@pytest.mark.workflow
async def test_the_face_check_writes_only_ledger_rows_and_never_starts_workers_or_migrations(
    client: Any, headers: dict[str, str], worker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full (simulated) face-check run on a finished comic: Temporal and Alembic are tripwired, and
    every table is fingerprinted before and after. Only the paid-request ledger may change."""
    import json as _json
    import uuid

    import alembic.command
    import temporalio.client
    from sqlalchemy import text

    from storypanel.db.session import transaction
    from tests.conftest import run_to_completion

    p = await run_to_completion(
        client, headers, "A robot and a girl share noodles in the rain, as a 4 panel strip"
    )

    async def no_temporal(*a: Any, **k: Any) -> Any:
        raise AssertionError("the face check connected to Temporal")

    def no_migration(*a: Any, **k: Any) -> Any:
        raise AssertionError("the face check ran a migration")

    monkeypatch.setattr(temporalio.client.Client, "connect", no_temporal)
    monkeypatch.setattr(alembic.command, "upgrade", no_migration)

    async def fingerprint() -> dict[str, str]:
        async with transaction() as db:
            tables = [r[0] for r in await db.execute(text(
                "select tablename from pg_tables where schemaname = current_schema() order by 1"))]  # fmt: skip
            out = {}
            for t in tables:
                out[t] = (await db.execute(text(
                    f'select md5(coalesce(string_agg(md5(x::text), \'\' order by md5(x::text)), \'\')) from "{t}" x'))).scalar_one()  # fmt: skip
            return out

    fc = _face_check_module()
    before = await fingerprint()
    out = tmp_path / "fc"
    out.mkdir()
    assert await fc.run(uuid.UUID(p["id"]), out, None, None, None) == 0
    after = await fingerprint()
    changed = sorted(t for t in before if before[t] != after.get(t))
    assert set(changed) <= {"provider_usage", "provider_attempts"}, changed
    summary = _json.loads((out / "face-check.json").read_text())
    assert summary["totals"]["calls"] >= 1 and "provider_attempts" in changed
    assert all(c["status"] == "succeeded" for c in summary["calls"])
    assert {f.name for f in out.iterdir()} >= {
        "face-check.json",
        "after-auto.pdf",
        "page-1-before-reading-size.png",
    }
