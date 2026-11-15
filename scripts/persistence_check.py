#!/usr/bin/env python3
"""Helper for scripts/persistence-check.sh (isolated stack only; simulated providers for `seed` and
`approve`; nothing here makes a paid call).

    seed STATE      create one comic, wait at the cast approval, record every object's SHA-256 and
                    one image fetched through the app's own signed URL
    verify STATE [STATUS]  after a restart: every recorded object present with the same hash, the image
                    retrievable through a fresh signed URL, the comic still waiting (default: at the cast approval)
    approve STATE   approve the cast and wait for the script approval (the workflow survived)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
import httpx

API = f"http://127.0.0.1:{os.environ['SP_API_PORT']}"
EMAIL = "persistence-check@example.com"


def s3() -> Any:
    return boto3.client("s3", endpoint_url=os.environ["SP_S3_ENDPOINT_URL"], region_name="us-east-1",
                        aws_access_key_id=os.environ["SP_S3_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["SP_S3_SECRET_ACCESS_KEY"])  # fmt: skip


def objects() -> dict[str, str]:
    c, out = s3(), {}
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=os.environ["SP_S3_BUCKET"]):
        for o in page.get("Contents", []):
            out[o["Key"]] = hashlib.sha256(c.get_object(Bucket=os.environ["SP_S3_BUCKET"], Key=o["Key"])["Body"].read()).hexdigest()
    return out


def session(h: httpx.Client) -> dict[str, str]:
    return {"Authorization": "Bearer " + h.post(f"{API}/v1/dev/login", json={"email": EMAIL}).json()["token"]}


def wait(h: httpx.Client, pid: str, hd: dict[str, str], ok: Any, limit: float = 180) -> dict[str, Any]:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        p = h.get(f"{API}/v1/projects/{pid}", headers=hd).json()
        if ok(p):
            return p  # type: ignore[no-any-return]
        time.sleep(1)
    raise SystemExit(f"FAIL comic {pid} did not reach the expected state")


def portrait(p: dict[str, Any]) -> tuple[str, str]:
    c = next(c for c in p["characters"] if c.get("images", {}).get("portrait"))
    return c["slug"], c["images"]["portrait"]


def main() -> int:
    action, state = sys.argv[1], Path(sys.argv[2])
    with httpx.Client(timeout=30) as h:
        hd = session(h)
        if action == "seed":
            pid = h.post(f"{API}/v1/projects", headers=hd,
                         json={"prompt": "A robot and a girl share noodles in the rain, as a 4 panel strip"}).json()["id"]
            p = wait(h, pid, hd, lambda p: p["status"] == "awaiting_cast_approval" and all(
                (c.get("sheet_check") or {}).get("status") != "pending" for c in p["characters"]))
            slug, url = portrait(p)
            img = h.get(url)
            if img.status_code != 200 or not img.content.startswith(b"\x89PNG"):
                raise SystemExit(f"FAIL image not retrievable before the restart (HTTP {img.status_code})")
            objs = objects()
            state.write_text(json.dumps({"project_id": pid, "slug": slug,
                                         "image_sha256": hashlib.sha256(img.content).hexdigest(), "objects": objs}))
            print(f"seeded comic {pid}: {len(objs)} object(s) stored, image retrieved via signed URL")
            return 0
        st = json.loads(state.read_text())
        if action == "verify":
            expected = sys.argv[3] if len(sys.argv) > 3 else "awaiting_cast_approval"
            now = objects()
            missing = [k for k in st["objects"] if k not in now]
            changed = [k for k in st["objects"] if k in now and now[k] != st["objects"][k]]
            p = h.get(f"{API}/v1/projects/{st['project_id']}", headers=hd).json()
            url = next((c["images"].get("portrait") for c in p.get("characters", []) if c["slug"] == st["slug"]), None)
            img = h.get(url) if url else None
            got = hashlib.sha256(img.content).hexdigest() if img is not None and img.status_code == 200 else None
            checks = {
                "every stored object still present": not missing,
                "every object unchanged (SHA-256)": not changed,
                "image retrieved through a fresh signed URL, same bytes": got == st["image_sha256"],
                f"comic still waiting ({expected})": p.get("status") == expected,
            }
            for name, ok in checks.items():
                print(("PASS " if ok else "FAIL ") + name)
            if missing:
                print(f"     {len(missing)} of {len(st['objects'])} object(s) missing")
            return 0 if all(checks.values()) else 1
        if action == "approve":
            p = h.get(f"{API}/v1/projects/{st['project_id']}", headers=hd).json()
            acks = [c["review"]["key"] for c in p["characters"] if c.get("review")]
            r = h.post(f"{API}/v1/projects/{st['project_id']}/characters/approve", headers=hd,
                       json={"cast_version": p["cast_version"], "acknowledged": acks})
            wait(h, st["project_id"], hd, lambda p: p["status"] == "awaiting_script_approval", 120)
            print(f"PASS workflow survived the restart: approval accepted ({r.status_code}) and the comic moved on")
            return 0
    raise SystemExit(f"unknown action {action}")


if __name__ == "__main__":
    sys.exit(main())
