"""Real object storage (the bucket created by infra/terraform, or any S3-compatible service the
deployment will use). Exercises exactly the calls the app makes, with the app's credentials."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
import uuid

import pytest

from live_tests.conftest import record, require


@pytest.fixture(autouse=True)
def _live() -> None:
    require("storage", "SP_S3_BUCKET", "SP_S3_ACCESS_KEY_ID", "SP_S3_SECRET_ACCESS_KEY")


def _get(url: str) -> int:
    try:
        return urllib.request.urlopen(url, timeout=20).status  # noqa: S310 - test against our own bucket
    except urllib.error.HTTPError as exc:
        return exc.code


async def test_signed_urls_versioned_deletion_and_iam_scope() -> None:
    from storypanel import storage

    key = f"live-test/{uuid.uuid4().hex}.png"
    await storage.put_bytes(key, b"v1", "image/png")
    await storage.put_bytes(key, b"v2", "image/png")  # second version on a versioned bucket
    url = storage.signed_url(key, ttl=120)
    assert _get(url) == 200  # browser-facing signing endpoint verifies the signature
    assert _get(url.replace("X-Amz-Signature=", "X-Amz-Signature=0")) == 403  # tampered: rejected
    versioned = await storage.bucket_versioned()
    client = storage._client()
    before = len(
        [
            v
            for v in client.list_object_versions(Bucket=os.environ["SP_S3_BUCKET"], Prefix=key).get(
                "Versions", []
            )
        ]
    )
    result = await storage.delete_keys([key])
    listing = client.list_object_versions(Bucket=os.environ["SP_S3_BUCKET"], Prefix=key)
    left = [v for v in listing.get("Versions", []) + listing.get("DeleteMarkers", []) if v["Key"] == key]
    assert result.ok and not left, (result, left)
    assert _get(url) in (403, 404)  # the signed URL no longer serves the object
    # least privilege: the app's credentials cannot touch the bucket's configuration
    import botocore.exceptions

    with pytest.raises(botocore.exceptions.ClientError):
        client.put_bucket_versioning(
            Bucket=os.environ["SP_S3_BUCKET"], VersioningConfiguration={"Status": "Suspended"}
        )
    record(
        "storage",
        "signed_urls_versioned_deletion_and_iam_scope",
        {
            "versioned": versioned,
            "versions_before_delete": before,
            "versions_after_delete": len(left),
            "public_endpoint": os.environ.get("SP_S3_PUBLIC_ENDPOINT_URL")
            or "(same as SP_S3_ENDPOINT_URL / AWS)",
        },
    )
