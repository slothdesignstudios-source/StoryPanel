"""End-to-end through the public API with a real Temporal worker and fake providers:
prompt -> cast approval -> script approval -> generation -> edit -> export."""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import Any

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from storypanel.db.session import transaction
from storypanel.services import credits
from tests.conftest import run_to_completion, wait_for

pytestmark = pytest.mark.workflow


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient() as http:
        r = await http.get(url)
        assert r.status_code == 200
        return r.content


async def _export(client: Any, headers: dict[str, str], pid: str, fmt: str) -> bytes:
    r = await client.post(f"/v1/projects/{pid}/exports", json={"format": fmt}, headers=headers)
    assert r.status_code == 202, r.text
    export_id = r.json()["id"]
    p = await wait_for(
        client,
        pid,
        headers,
        lambda p: any(e["id"] == export_id and e["status"] in ("ready", "failed") for e in p["exports"]),
    )
    export = next(e for e in p["exports"] if e["id"] == export_id)
    assert export["status"] == "ready", export
    return await _download(export["download_url"])


async def test_prompt_to_export(client: Any, headers: dict[str, str], worker: Any) -> None:
    p = await run_to_completion(client, headers)
    assert p["status"] == "completed", p["failure"]
    assert len(p["pages"]) == 2
    panels = [panel for page in p["pages"] for panel in page["panels"]]
    assert all(panel["status"] == "ok" and panel["image_url"] for panel in panels)
    assert all(page["status"] == "ready" and page["preview_url"] for page in p["pages"])
    assert p["progress"]["panels_done"] == p["progress"]["panels_total"] == len(panels)
    # every dialogue line became an editable vector balloon; none carry layout problems
    for panel in panels:
        assert len(panel["balloons"]) == len(panel["spec"]["dialogue"])
        assert all(
            "overflow" not in b["flags"] and "reading_order" not in b["flags"] for b in panel["balloons"]
        )
    # credits: plan (cast + script) + one per panel; nothing left on hold
    async with transaction() as db:
        from sqlalchemy import select

        from storypanel.db.models import User

        user = (await db.execute(select(User))).scalar_one()
        assert user.credit_reserved == 0
        assert user.credit_balance == 200 - 8 - len(panels)
        assert await credits.reconcile(db) == []

    pid = p["id"]
    # --- edit: retype and move a balloon (optimistic concurrency)
    balloon = panels[0]["balloons"][0]
    r = await client.patch(
        f"/v1/balloons/{balloon['id']}",
        headers=headers,
        json={
            "version": balloon["version"],
            "text": "Edited line.",
            "geometry": {
                **{k: balloon["geometry"][k] for k in ("x", "y", "w", "h", "font_size")},
                "x": balloon["geometry"]["x"] + 5,
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "Edited line." and r.json()["version"] == balloon["version"] + 1
    stale = await client.patch(
        f"/v1/balloons/{balloon['id']}",
        headers=headers,
        json={"version": balloon["version"], "text": "Late write"},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    # --- regenerate a panel, compare versions, restore the original
    target = panels[1]
    r = await client.post(f"/v1/panels/{target['id']}/regenerate", headers=headers)
    assert r.status_code == 202, r.text
    p = await wait_for(
        client,
        pid,
        headers,
        lambda p: (
            next(x for pg in p["pages"] for x in pg["panels"] if x["id"] == target["id"])[
                "current_version_id"
            ]
            != target["current_version_id"]
        ),
    )
    versions = (await client.get(f"/v1/panels/{target['id']}/versions", headers=headers)).json()["versions"]
    assert len(versions) == 2 and sum(v["is_current"] for v in versions) == 1
    r = await client.post(
        f"/v1/panels/{target['id']}/versions/{target['current_version_id']}/restore", headers=headers
    )
    assert r.status_code == 200
    # --- instruction edit
    r = await client.post(
        f"/v1/panels/{target['id']}/edit", headers=headers, json={"instruction": "make her smile"}
    )
    assert r.status_code == 202
    await wait_for(
        client,
        pid,
        headers,
        lambda p: (
            len([1 for pg in p["pages"] for x in pg["panels"] if x["busy"]]) == 0
            and next(x for pg in p["pages"] for x in pg["panels"] if x["id"] == target["id"])[
                "current_version_id"
            ]
            != target["current_version_id"]
        ),
    )

    # --- exports: valid files, right page count and order, real dimensions
    pdf = PdfReader(io.BytesIO(await _export(client, headers, pid, "pdf")))
    assert len(pdf.pages) == 2
    cbz = zipfile.ZipFile(io.BytesIO(await _export(client, headers, pid, "cbz")))
    names = [n for n in cbz.namelist() if n.endswith(".png")]
    assert names == ["page_001.png", "page_002.png"]
    assert b"<PageCount>2</PageCount>" in cbz.read("ComicInfo.xml")
    for name in names:
        with Image.open(io.BytesIO(cbz.read(name))) as img:
            assert img.size == (2000, 3000)
    png = zipfile.ZipFile(io.BytesIO(await _export(client, headers, pid, "png")))
    assert sorted(png.namelist()) == ["page_001.png", "page_002.png"]


async def test_strip_with_clarifying_question(client: Any, headers: dict[str, str], worker: Any) -> None:
    r = await client.post("/v1/projects", json={"prompt": "A cat discovers the moon"}, headers=headers)
    pid = r.json()["id"]
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_answers")
    assert 1 <= len(p["questions"]) <= 3
    r = await client.post(
        f"/v1/projects/{pid}/brief/answers", headers=headers, json={"answers": {"length": "A 4-panel strip"}}
    )
    assert r.status_code == 202
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    assert p["format"] == "strip" and p["brief"]["pages"] == 1


async def test_reload_restores_active_job(client: Any, headers: dict[str, str], worker: Any) -> None:
    """A fresh client (browser reload) sees the same project, gate and event position."""
    r = await client.post("/v1/projects", json={"prompt": "A robot learns to cook. 2 pages"}, headers=headers)
    pid = r.json()["id"]
    await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    listing = (await client.get("/v1/projects", headers=headers)).json()["projects"]
    assert [x["id"] for x in listing] == [pid] and listing[0]["status"] == "awaiting_cast_approval"
    again = (await client.get(f"/v1/projects/{pid}", headers=headers)).json()
    assert again["event_seq"] > 0 and len(again["characters"]) == 2
    assert all(c["sheet_status"] == "ready" and c["images"]["portrait"] for c in again["characters"])
    await asyncio.sleep(0)
