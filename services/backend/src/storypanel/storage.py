"""Private S3-compatible object storage with short-lived signed downloads.

Also the single place where image bytes are validated (type, dimensions, decoded size)
before anything is stored, whether they came from a provider or anywhere else.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from dataclasses import dataclass
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


def reset_client() -> None:
    _client.cache_clear()


def new_key(owner_id: object, project_id: object | None, kind: str, ext: str) -> str:
    scope = f"projects/{project_id}" if project_id else "account"
    return f"users/{owner_id}/{scope}/{kind}/{uuid.uuid4().hex}.{ext}"


async def _call(fn: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, **kwargs)
    except (BotoCoreError, ClientError) as exc:
        raise AppError(ErrorCode.STORAGE, "Storage is temporarily unavailable.") from exc


async def put_bytes(key: str, data: bytes, content_type: str) -> None:
    await _call(_client().put_object, Bucket=get_settings().s3_bucket, Key=key, Body=data,
                ContentType=content_type, ServerSideEncryption="AES256")  # fmt: skip


async def get_bytes(key: str) -> bytes:
    resp = await _call(_client().get_object, Bucket=get_settings().s3_bucket, Key=key)
    return await asyncio.to_thread(resp["Body"].read)


async def delete_keys(keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        chunk = [{"Key": k} for k in keys[i : i + 1000]]
        await _call(_client().delete_objects, Bucket=get_settings().s3_bucket,
                    Delete={"Objects": chunk, "Quiet": True})  # fmt: skip


def signed_url(key: str, *, filename: str | None = None, ttl: int | None = None) -> str:
    """Presigning is local computation (no network), so it is safe on the request path."""
    params: dict[str, Any] = {"Bucket": get_settings().s3_bucket, "Key": key}
    if filename:
        safe = "".join(c for c in filename if c.isalnum() or c in "-_. ")[:80] or "download"
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return str(
        _client().generate_presigned_url(
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
    await _call(_client().put_bucket_cors, Bucket=s.s3_bucket, CORSConfiguration={"CORSRules": [
        {"AllowedMethods": ["GET"], "AllowedOrigins": s.cors_origins, "AllowedHeaders": ["*"], "MaxAgeSeconds": 600}]})


async def healthy() -> bool:
    try:
        await asyncio.to_thread(_client().head_bucket, Bucket=get_settings().s3_bucket)
        return True
    except Exception:
        return False
