"""Private S3-compatible object storage with short-lived signed downloads.

Also the single place where image bytes are validated (type, dimensions, decoded size)
before anything is stored, whether they came from a provider or anywhere else.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from storypanel.config import get_settings
from storypanel.errors import AppError, ErrorCode

Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression-bomb guard (Pillow raises above 2x)
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_SIDE = 8192
ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes  # re-encoded PNG with all metadata stripped
    width: int
    height: int
    sha256: str
    content_type: str = "image/png"


def validate_image(raw: bytes) -> ValidatedImage:
    """Decode, bound-check and re-encode. Re-encoding drops EXIF/ICC/text chunks."""
    if len(raw) > MAX_IMAGE_BYTES:
        raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Image is too large.", retryable=False)
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in ALLOWED_FORMATS:
                raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Unsupported image type.",
                               retryable=False)  # fmt: skip
            if probe.width > MAX_SIDE or probe.height > MAX_SIDE or min(probe.size) < 16:
                raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Image dimensions out of range.",
                               retryable=False)  # fmt: skip
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            clean = img.convert("RGB")
    except AppError:
        raise
    except Exception as exc:  # truncated, bomb, or not an image at all
        raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Image could not be decoded.") from exc
    out = io.BytesIO()
    clean.save(out, format="PNG", optimize=False)
    data = out.getvalue()
    return ValidatedImage(data, clean.width, clean.height, hashlib.sha256(data).hexdigest())


@lru_cache
def _client() -> Any:
    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id.get_secret_value() if s.s3_access_key_id else None,
        aws_secret_access_key=(s.s3_secret_access_key.get_secret_value() if s.s3_secret_access_key else None),
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),  # fmt: skip
    )


@lru_cache
def _signing_client() -> Any:
    """Presigns for the browser-facing endpoint. Purely local computation."""
    s = get_settings()
    if not s.s3_public_endpoint_url or s.s3_public_endpoint_url == s.s3_endpoint_url:
        return _client()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_public_endpoint_url,
        region_name=s.s3_region,
        aws_access_key_id=s.s3_access_key_id.get_secret_value() if s.s3_access_key_id else None,
        aws_secret_access_key=(s.s3_secret_access_key.get_secret_value() if s.s3_secret_access_key else None),
        config=Config(signature_version="s3v4"),  # same addressing as the main client
    )


def reset_client() -> None:
    _client.cache_clear()
    _signing_client.cache_clear()


def new_key(owner_id: object, project_id: object | None, kind: str, ext: str) -> str:
    scope = f"projects/{project_id}" if project_id else "account"
    return f"users/{owner_id}/{scope}/{kind}/{uuid.uuid4().hex}.{ext}"


async def _call(fn: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, **kwargs)
    except (BotoCoreError, ClientError) as exc:
        raise AppError(ErrorCode.STORAGE, "Storage is temporarily unavailable.") from exc


async def put_bytes(key: str, data: bytes, content_type: str) -> None:
    s = get_settings()
    sse = (
        {} if s.s3_server_side_encryption == "none" else {"ServerSideEncryption": s.s3_server_side_encryption}
    )
    await _call(_client().put_object, Bucket=s.s3_bucket, Key=key, Body=data, ContentType=content_type, **sse)


async def get_bytes(key: str) -> bytes:
    resp = await _call(_client().get_object, Bucket=get_settings().s3_bucket, Key=key)
    return await asyncio.to_thread(resp["Body"].read)


RECOVERABLE_DELETE_ERRORS = {"InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout",
                             "OperationAborted", "request_failed"}  # fmt: skip


@dataclass
class DeleteResult:
    """Per-key outcome of a deletion. `failed` maps key -> S3 error code. A key is only in
    `deleted` once S3 confirmed it (and, for versioned buckets, every version of it)."""

    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


_versioned: dict[str, bool] = {}


async def bucket_versioned() -> bool:
    """Versioned buckets keep old versions after a plain delete (it only adds a delete
    marker), so user-data deletion must remove each version explicitly."""
    bucket = get_settings().s3_bucket
    if bucket not in _versioned:
        resp = await _call(_client().get_bucket_versioning, Bucket=bucket)
        _versioned[bucket] = resp.get("Status") in ("Enabled", "Suspended")
    return _versioned[bucket]


async def _versions(key: str) -> list[dict[str, str]]:
    bucket = get_settings().s3_bucket
    out: list[dict[str, str]] = []
    kw: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
    while True:
        resp = await _call(_client().list_object_versions, **kw)
        for v in resp.get("Versions", []) + resp.get("DeleteMarkers", []):
            if v["Key"] == key:
                out.append({"Key": key, "VersionId": v["VersionId"]})
        if not resp.get("IsTruncated"):
            return out
        kw.update(KeyMarker=resp["NextKeyMarker"], VersionIdMarker=resp["NextVersionIdMarker"])


async def delete_keys(keys: list[str], *, retries: int = 3) -> DeleteResult:
    """Delete objects permanently and report each key's outcome. Per-key errors returned in
    a successful batch response are inspected (S3 reports AccessDenied etc. that way);
    recoverable ones are retried with backoff. Callers must keep a durable record of
    `failed` keys rather than dropping the only reference to them."""
    result = DeleteResult()
    if not keys:
        return result
    bucket = get_settings().s3_bucket
    pending = list(dict.fromkeys(keys))
    for attempt in range(retries + 1):
        retry: list[str] = []
        try:
            versioned = await bucket_versioned()
        except AppError:
            versioned = None
        for i in range(0, len(pending), 1000):
            chunk = pending[i : i + 1000]
            if versioned is None:
                result.failed.update({k: "request_failed" for k in chunk})
                retry.extend(chunk)
                continue
            try:
                if versioned:
                    objects = [v for k in chunk for v in await _versions(k)]
                else:
                    objects = [{"Key": k} for k in chunk]
                errors: dict[str, str] = {}
                for j in range(0, len(objects), 1000):
                    if not objects[j : j + 1000]:
                        continue
                    resp = await _call(_client().delete_objects, Bucket=bucket,
                                       Delete={"Objects": objects[j : j + 1000], "Quiet": True})  # fmt: skip
                    for err in resp.get("Errors") or []:
                        errors.setdefault(err["Key"], err.get("Code") or "Unknown")
            except AppError:
                errors = {k: "request_failed" for k in chunk}
            for k in chunk:
                if k in errors:
                    result.failed[k] = errors[k]
                    if errors[k] in RECOVERABLE_DELETE_ERRORS:
                        retry.append(k)
                else:
                    result.failed.pop(k, None)
                    result.deleted.append(k)
        if not retry or attempt == retries:
            break
        pending = retry
        await asyncio.sleep(min(2.0, 0.2 * 2**attempt))
    return result


def signed_url(key: str, *, filename: str | None = None, ttl: int | None = None) -> str:
    """Presigning is local computation (no network), so it is safe on the request path."""
    params: dict[str, Any] = {"Bucket": get_settings().s3_bucket, "Key": key}
    if filename:
        safe = "".join(c for c in filename if c.isalnum() or c in "-_. ")[:80] or "download"
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return str(
        _signing_client().generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl or get_settings().signed_url_ttl_seconds
        )
    )


async def ensure_bucket() -> None:
    """Local/test convenience; deployed buckets are created by infrastructure code."""
    s = get_settings()
    try:
        await asyncio.to_thread(_client().head_bucket, Bucket=s.s3_bucket)
    except ClientError:
        await _call(_client().create_bucket, Bucket=s.s3_bucket)
    # Same rule infra/terraform applies to deployed buckets: the web app may GET signed
    # objects cross-origin (needed for the canvas to read pixels it drew).
    await _call(
        _client().put_bucket_cors,
        Bucket=s.s3_bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedMethods": ["GET"],
                    "AllowedOrigins": s.cors_origins,
                    "AllowedHeaders": ["*"],
                    "MaxAgeSeconds": 600,
                }
            ]
        },
    )


async def healthy() -> bool:
    try:
        await asyncio.to_thread(_client().head_bucket, Bucket=get_settings().s3_bucket)
        return True
    except Exception:
        return False
