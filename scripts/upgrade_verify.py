#!/usr/bin/env python3
"""Upgrade verification driver (used by scripts/upgrade-verify.sh; simulated providers only).

    seed      OLD code running: create comics paused at the cast gate, paused at the script gate,
              and one completed; write their ids to state.json.
    history   save every workflow history of those comics (JSON) from the Temporal server.
    replay    replay the saved histories against THIS checkout's workflow code (non-determinism
              fails the run).
    finish    NEW code running on the upgraded database: check how legacy rows are reported, then
              take the paused comics through the remaining gates to completion and check credits.

Talks only to the local API given by --api; makes no paid call (fake providers).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "A robot and a girl share noodles in the rain, as a 4 panel strip"


def ok(cond: bool, what: str, report: list[dict[str, Any]], detail: Any = None) -> None:
    report.append({"check": what, "ok": bool(cond), **({"detail": detail} if detail is not None else {})})
    print(("PASS " if cond else "FAIL ") + what + (f"  ({detail})" if detail is not None and not cond else ""))


async def login(c: httpx.AsyncClient, email: str) -> dict[str, str]:
    r = await c.post("/v1/dev/login", json={"email": email})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def wait(c: httpx.AsyncClient, pid: str, h: dict[str, str], pred: Any, timeout: float = 120) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while True:
        p = (await c.get(f"/v1/projects/{pid}", headers=h)).json()
        if pred(p):
            return p
        if p.get("status") in ("failed", "blocked", "cancelled", "expired") or time.monotonic() > end:
            raise SystemExit(f"comic {pid}: status {p.get('status')} failure {p.get('failure')}")
        await asyncio.sleep(0.5)


def sheets_done(p: dict[str, Any]) -> bool:
    # old code has no sheet review; new code reviews sheets after they are drawn
    return all(c.get("sheet_status") == "ready" for c in p.get("characters", [])) and all(
        (c.get("sheet_check") or {}).get("status") != "pending" for c in p.get("characters", []))


async def seed(api: str, state: Path) -> None:
    async with httpx.AsyncClient(base_url=api, timeout=30) as c:
        h = await login(c, "upgrade-check@example.com")
        ids = {}
        for name in ("cast_gate", "script_gate", "completed"):
            r = await c.post("/v1/projects", json={"prompt": PROMPT}, headers=h)
            r.raise_for_status()
            ids[name] = r.json()["id"]
        for pid in ids.values():
            await wait(c, pid, h, lambda p: p["status"] == "awaiting_cast_approval" and sheets_done(p))
        for name in ("script_gate", "completed"):
            p = (await c.get(f"/v1/projects/{ids[name]}", headers=h)).json()
            (await c.post(f"/v1/projects/{ids[name]}/characters/approve", json={"cast_version": p["cast_version"]},
                          headers=h)).raise_for_status()  # fmt: skip
            await wait(c, ids[name], h, lambda p: p["status"] == "awaiting_script_approval")
        p = (await c.get(f"/v1/projects/{ids['completed']}", headers=h)).json()
        (await c.post(f"/v1/projects/{ids['completed']}/script/approve",
                      json={"script_version": p["script_version"]}, headers=h)).raise_for_status()  # fmt: skip
        await wait(c, ids["completed"], h, lambda p: p["status"] in ("completed", "partially_completed"), 240)
        me = (await c.get("/v1/me", headers=h)).json()
        projects = {k: (await c.get(f"/v1/projects/{v}", headers=h)).json() for k, v in ids.items()}
    state.write_text(json.dumps({"ids": ids,
                                 "before": {"balance": me.get("balance"), "reserved": me.get("reserved"),
                                            "status": {k: p["status"] for k, p in projects.items()}}}, indent=1))  # fmt: skip
    print("seeded", ids)


async def history(address: str, state: Path, out: Path) -> None:
    from temporalio.client import Client

    st = json.loads(state.read_text())
    client = await Client.connect(address)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    async for wf in client.list_workflows():
        h = await client.get_workflow_handle(wf.id, run_id=wf.run_id).fetch_history()
        (out / f"{wf.workflow_type}-{wf.id}.json").write_text(h.to_json())
        n += 1
    st["histories"] = n
    state.write_text(json.dumps(st, indent=1))
    print(f"saved {n} histories to {out}")


async def replay(hist_dir: Path, report_path: Path) -> int:
    sys.path.insert(0, str(ROOT / "services/backend/src"))
    from temporalio.client import WorkflowHistory
    from temporalio.contrib.opentelemetry import TracingInterceptor
    from temporalio.worker import Replayer

    from storypanel.workflows.worker import WORKFLOWS

    replayer = Replayer(workflows=WORKFLOWS, interceptors=[TracingInterceptor()])
    report: list[dict[str, Any]] = []
    for f in sorted(hist_dir.glob("*.json")):
        wid = f.stem.split("-", 1)[1]
        try:
            await replayer.replay_workflow(WorkflowHistory.from_json(wid, f.read_text()))
            ok(True, f"replay {f.stem}", report)
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            ok(False, f"replay {f.stem}", report, f"{type(exc).__name__}: {exc}")
    report_path.write_text(json.dumps(report, indent=1))
    return 0 if report and all(r["ok"] for r in report) else 1


async def finish(api: str, state: Path, report_path: Path, credits_before: str) -> int:
    st = json.loads(state.read_text())
    ids = st["ids"]
    report: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=api, timeout=30) as c:
        h = await login(c, "upgrade-check@example.com")
        pre = {k: (await c.get(f"/v1/projects/{v}", headers=h)).json() for k, v in ids.items()}
        ok(all(pre[k]["status"] == st["before"]["status"][k] for k in ids), "every comic keeps its status after the upgrade",
           report, {k: pre[k]["status"] for k in ids})  # fmt: skip
        ok(all(c_["sheet_check"] is None and c_["review"] is None and c_["sheet_rules_current"] is None
               for c_ in pre["cast_gate"]["characters"]),
           "sheets drawn before reviews existed: no review, nothing to confirm, rules currency unknown", report)  # fmt: skip
        ok(pre["cast_gate"]["bible"]["continuity_rules"] == [] and len(pre["cast_gate"]["bible"]["world_rules"]) > 0,
           "old Story Bible reads with no continuity rules and its world rules unchanged", report)  # fmt: skip
        script = pre["script_gate"]["script"]
        legacy = [r for r in script["review_required"] if r["kind"] == "Script wasn't checked against the Story Bible"] if script else []
        ok(bool(script) and script.get("consistency") is None and len(legacy) == 1,
           "script written before the Bible check: reported as not checked, confirmation required", report,
           script and [r["kind"] for r in script["review_required"]])  # fmt: skip
        credits0 = (await c.get("/v1/credits", headers=h)).json()
        ok(credits0["holds_without_history"] >= 1 and all(x["history"] == "unavailable" for x in credits0["holds"]),
           "holds made before the hold history existed are listed as history unavailable", report,
           {"holds": credits0["holds"], "without_history": credits0["holds_without_history"]})  # fmt: skip
        me0 = (await c.get("/v1/me", headers=h)).json()
        bal, held = (int(x) for x in credits_before.split("|"))  # read from the database before the upgrade
        ok((me0["balance"], me0["reserved"]) == (bal, held), "balance and held credits unchanged by the upgrade",
           report, {"after": me0, "before": {"balance": bal, "reserved": held}})  # fmt: skip

        # cast gate -> new code continues: approve the cast, the new script check runs
        cg = ids["cast_gate"]
        r = await c.post(f"/v1/projects/{cg}/characters/approve", json={"cast_version": pre["cast_gate"]["cast_version"]},
                         headers=h)  # fmt: skip
        ok(r.status_code == 200, "comic paused at the cast gate before the upgrade can be approved after it", report, r.text)
        p = await wait(c, cg, h, lambda p: p["status"] == "awaiting_script_approval"
                       and (p.get("script") or {}).get("consistency", {}).get("status") not in (None, "pending"))  # fmt: skip
        ok(p["script"]["consistency"]["status"] in ("done", "skipped"),
           "its script, written after the upgrade, gets the new Story Bible check", report, p["script"]["consistency"])
        r = await c.post(f"/v1/projects/{cg}/script/approve", headers=h, json={
            "script_version": p["script_version"], "acknowledged": [x["key"] for x in p["script"]["review_required"]]})  # fmt: skip
        ok(r.status_code == 200, "and its script can be approved", report, r.text)

        # script gate -> legacy "not checked" item must be confirmed explicitly
        sg = ids["script_gate"]
        p = pre["script_gate"]
        r = await c.post(f"/v1/projects/{sg}/script/approve", json={"script_version": p["script_version"]}, headers=h)
        ok(r.status_code == 422, "approving the pre-upgrade script without confirming 'not checked' is refused", report,
           r.status_code)  # fmt: skip
        r = await c.post(f"/v1/projects/{sg}/script/approve", headers=h, json={
            "script_version": p["script_version"], "acknowledged": [x["key"] for x in p["script"]["review_required"]]})  # fmt: skip
        ok(r.status_code == 200, "with the confirmation it is approved (recorded as an override)", report, r.text)

        done = {}
        for name in ("cast_gate", "script_gate"):
            done[name] = await wait(c, ids[name], h, lambda p: p["status"] in ("completed", "partially_completed"), 300)
        ok(all(p["status"] == "completed" for p in done.values()),
           "both paused comics finish with the new code", report, {k: p["status"] for k, p in done.items()})  # fmt: skip
        comp = (await c.get(f"/v1/projects/{ids['completed']}", headers=h)).json()
        ok(comp["status"] == st["before"]["status"]["completed"] and all(
            pn["image_url"] for pg in comp["pages"] for pn in pg["panels"]),
           "the comic completed before the upgrade still reads with every panel", report)  # fmt: skip
        ok(all(pn["qa_rules_current"] is None for pg in comp["pages"] for pn in pg["panels"]),
           "its panel checks predate rule fingerprints: reported unknown, not current", report)  # fmt: skip
        r = await c.post(f"/v1/projects/{ids['completed']}/exports", json={"format": "pdf"}, headers=h)
        ok(r.status_code in (200, 201, 202), "the pre-upgrade comic exports with the new code", report, r.text)
        credits1 = (await c.get("/v1/credits", headers=h)).json()
        me1 = (await c.get("/v1/me", headers=h)).json()
        ok(me1["reserved"] == 0 and credits1["holds"] == [], "no credits left held at the end", report, credits1["holds"])
    report_path.write_text(json.dumps(report, indent=1))
    return 0 if all(r["ok"] for r in report) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["seed", "history", "replay", "finish"])
    ap.add_argument("--api", default="http://127.0.0.1:8010")
    ap.add_argument("--temporal", default="127.0.0.1:7244")
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--histories", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--credits-before", default="", help="balance|reserved read from the database before the upgrade")
    a = ap.parse_args()
    if a.step == "seed":
        asyncio.run(seed(a.api, a.state))
    elif a.step == "history":
        asyncio.run(history(a.temporal, a.state, a.histories))
    elif a.step == "replay":
        return asyncio.run(replay(a.histories, a.report))
    else:
        return asyncio.run(finish(a.api, a.state, a.report, a.credits_before))
    return 0


if __name__ == "__main__":
    sys.exit(main())
