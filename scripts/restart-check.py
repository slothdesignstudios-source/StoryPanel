#!/usr/bin/env python3
"""Free check that a comic paused at an approval survives a restart of the local stack.

    services/backend/.venv/bin/python scripts/restart-check.py

Simulated providers only: refuses to run if the API reports live providers (a live comic would
make paid calls). Creates one comic as restart-check@example.com, waits at the cast approval,
stops and starts scripts/native-stack.sh, approves, and checks the comic reaches the script
approval. Exit 0 = workflow state survived. The test comic stays in that account; delete it in
the app if you like.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("SP_SMOKE_API", "http://127.0.0.1:8000")
STACK = str(ROOT / "scripts/native-stack.sh")


async def wait(c: httpx.AsyncClient, pid: str, h: dict[str, str], pred: object, limit: float) -> dict:
    end = time.monotonic() + limit
    p: dict = {}
    while time.monotonic() < end:
        p = (await c.get(f"{API}/v1/projects/{pid}", headers=h)).json()
        if pred(p):  # type: ignore[operator]
            return p
        await asyncio.sleep(1)
    raise SystemExit(f"FAIL: comic {pid} stayed {p.get('status')} (workflow state did not survive)")


async def pause() -> str | None:
    """Create a comic and wait at the cast approval; None if the API runs live providers."""
    async with httpx.AsyncClient(timeout=30) as c:
        meta = (await c.get(f"{API}/v1/meta")).json()
        if meta.get("provider_mode") != "fake":
            print("refusing: the API runs live providers; start the stack without SP_STACK_LIVE=1 for this check")
            return None
        h = {"Authorization": f"Bearer {(await c.post(f'{API}/v1/dev/login', json={'email': 'restart-check@example.com'})).json()['token']}"}
        pid = (await c.post(f"{API}/v1/projects", headers=h, json={
            "prompt": "A robot and a girl share noodles in the rain, as a 4 panel strip"})).json()["id"]
        await wait(c, pid, h, lambda p: p["status"] == "awaiting_cast_approval" and all(
            (x.get("sheet_check") or {}).get("status") != "pending" for x in p["characters"]), 180)
        print(f"comic {pid} paused at the cast approval; restarting the stack")
        return str(pid)


async def approve(pid: str) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        h = {"Authorization": f"Bearer {(await c.post(f'{API}/v1/dev/login', json={'email': 'restart-check@example.com'})).json()['token']}"}
        p = (await c.get(f"{API}/v1/projects/{pid}", headers=h)).json()
        acks = [x["review"]["key"] for x in p["characters"] if x.get("review")]
        r = await c.post(f"{API}/v1/projects/{pid}/characters/approve", headers=h,
                         json={"cast_version": p["cast_version"], "acknowledged": acks})
        print("approve after restart:", r.status_code)
        await wait(c, pid, h, lambda p: p["status"] == "awaiting_script_approval", 120)
    print("PASS: the paused comic resumed after a restart (workflow state is kept on disk)")


def main() -> int:
    pid = asyncio.run(pause())
    if pid is None:
        return 2
    subprocess.run([STACK, "stop"], check=True)  # noqa: S603 - fixed path in this repository
    subprocess.run([STACK, "start"], check=True)  # noqa: S603
    asyncio.run(approve(pid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
