#!/usr/bin/env python3
"""Copy, check and restore the objects of the LOCAL S3-compatible storage (images, exports).

    services/backend/.venv/bin/python scripts/s3-snapshot.py export DIR       # read-only on storage
    services/backend/.venv/bin/python scripts/s3-snapshot.py verify DIR       # read-only on storage
    services/backend/.venv/bin/python scripts/s3-snapshot.py import DIR       # writes to storage

Endpoint, bucket and credentials come from --endpoint/--bucket or SP_S3_ENDPOINT_URL, SP_S3_BUCKET,
SP_S3_ACCESS_KEY_ID, SP_S3_SECRET_ACCESS_KEY (defaults: http://127.0.0.1:9000, storypanel-assets).
Only endpoints on this machine are accepted. Credentials are never printed.

export  Lists every object (all pages), downloads each one, stores it under DIR/objects/<sha256>
        and writes DIR/manifest.jsonl (key, size, sha256, content type) and DIR/SUMMARY.json.
        Each stored file is re-hashed after writing. Refuses a DIR that already has a manifest.
verify  For each manifest entry, downloads the object from storage and compares its SHA-256.
        Reports ok / missing (storage answered not found) / mismatch / unknown (any other error).
        Also re-hashes the files in DIR. Exit 0 only if every object is ok.
import  Uploads every manifest entry that storage does not have, with its content type, then
        downloads it again and checks the hash. An object that exists with the same hash is
        skipped; one that exists with a DIFFERENT hash stops the import (nothing is overwritten).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def client(endpoint: str) -> Any:
    import boto3
    from botocore.config import Config

    host = urllib.parse.urlsplit(endpoint).hostname or ""
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"refusing: {endpoint!r} is not on this machine")
    return boto3.client("s3", endpoint_url=endpoint, region_name=os.environ.get("SP_S3_REGION", "us-east-1"),
                        aws_access_key_id=os.environ.get("SP_S3_ACCESS_KEY_ID", "local"),
                        aws_secret_access_key=os.environ.get("SP_S3_SECRET_ACCESS_KEY", "local"),
                        config=Config(retries={"max_attempts": 3, "mode": "standard"},
                                      s3={"addressing_style": "path"}))  # fmt: skip


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def _not_found(exc: Exception) -> bool:
    return _code(exc) in ("NoSuchKey", "NoSuchBucket", "404", "NotFound")  # storage said: not there


def _manifest(d: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (d / "manifest.jsonl").read_text().splitlines() if line.strip()]


def export(s3: Any, bucket: str, d: Path) -> int:
    if (d / "manifest.jsonl").exists():
        raise SystemExit(f"refusing: {d} already holds a snapshot")
    (d / "objects").mkdir(parents=True, exist_ok=True)
    n = size = 0
    with (d / "manifest.jsonl.partial").open("w") as man:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                obj = s3.get_object(Bucket=bucket, Key=item["Key"])
                data = obj["Body"].read()
                digest = hashlib.sha256(data).hexdigest()
                path = d / "objects" / digest
                if not path.exists():
                    path.write_bytes(data)
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise SystemExit(f"write check failed for {item['Key']}")
                man.write(json.dumps({"key": item["Key"], "size": len(data), "sha256": digest,
                                      "content_type": obj.get("ContentType") or "application/octet-stream"}) + "\n")
                n += 1
                size += len(data)
    (d / "manifest.jsonl.partial").rename(d / "manifest.jsonl")  # present only when complete
    (d / "SUMMARY.json").write_text(json.dumps({"bucket": bucket, "objects": n, "bytes": size, "complete": True,
                                               "created_at": datetime.now(UTC).isoformat()}, indent=1))  # fmt: skip
    print(f"exported {n} object(s), {size / 1e6:.1f} MB, all pages listed -> {d}")
    return 0


def verify(s3: Any, bucket: str, d: Path) -> int:
    counts = {"ok": 0, "missing": 0, "mismatch": 0, "unknown": 0, "file_bad": 0}
    for e in _manifest(d):
        f = d / "objects" / e["sha256"]
        if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest() != e["sha256"]:
            counts["file_bad"] += 1
        try:
            data = s3.get_object(Bucket=bucket, Key=e["key"])["Body"].read()
        except Exception as exc:  # noqa: BLE001 - classified below
            counts["missing" if _not_found(exc) else "unknown"] += 1
            continue
        counts["ok" if hashlib.sha256(data).hexdigest() == e["sha256"] else "mismatch"] += 1
    print(json.dumps({"bucket": bucket, "entries": sum(counts.values()) - counts["file_bad"], **counts}))
    return 0 if counts["ok"] == len(_manifest(d)) and not counts["file_bad"] else 1


def import_(s3: Any, bucket: str, d: Path) -> int:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001
        if not _not_found(exc) and _code(exc) not in ("NoSuchBucket",):
            raise SystemExit(f"bucket check failed ({_code(exc)}); nothing imported") from exc
        s3.create_bucket(Bucket=bucket)
    put = skipped = 0
    for e in _manifest(d):
        try:
            existing = s3.get_object(Bucket=bucket, Key=e["key"])["Body"].read()
        except Exception as exc:  # noqa: BLE001
            if not _not_found(exc):
                raise SystemExit(f"cannot read {e['key']} ({_code(exc)}); stopped, nothing overwritten") from exc
            existing = None
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != e["sha256"]:
                raise SystemExit(f"conflict: {e['key']} exists with different content; stopped, nothing overwritten")
            skipped += 1
            continue
        data = (d / "objects" / e["sha256"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != e["sha256"]:
            raise SystemExit(f"snapshot file for {e['key']} is damaged; stopped")
        s3.put_object(Bucket=bucket, Key=e["key"], Body=data, ContentType=e["content_type"])
        back = s3.get_object(Bucket=bucket, Key=e["key"])["Body"].read()
        if hashlib.sha256(back).hexdigest() != e["sha256"]:
            raise SystemExit(f"read-back check failed for {e['key']}")
        put += 1
    print(f"imported {put}, already present {skipped}; every object re-read and hash-checked")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "verify", "import"])
    ap.add_argument("dir", type=Path)
    ap.add_argument("--endpoint", default=os.environ.get("SP_S3_ENDPOINT_URL", "http://127.0.0.1:9000"))
    ap.add_argument("--bucket", default=os.environ.get("SP_S3_BUCKET", "storypanel-assets"))
    a = ap.parse_args()
    s3 = client(a.endpoint)
    return {"export": export, "verify": verify, "import": import_}[a.action](s3, a.bucket, a.dir)


if __name__ == "__main__":
    sys.exit(main())
