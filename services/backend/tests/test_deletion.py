"""Object deletion and account deletion (review findings 4 and 5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from storypanel import storage
from storypanel.config import get_settings


class _PartialDenial:
    """Isolated double around the real S3 client: the batch call succeeds at the HTTP
    level but reports per-key errors, as S3 does."""

    def __init__(self, real: Any, deny: set[str], code: str = "AccessDenied", times: int = 99) -> None:
        self._real, self._deny, self._code, self._times = real, deny, code, times
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def delete_objects(self, **kw: Any) -> dict[str, Any]:
        self.calls += 1
        objs = kw["Delete"]["Objects"]
        if self._times <= 0:
            return self._real.delete_objects(**kw)  # type: ignore[no-any-return]
        self._times -= 1
        allowed = [o for o in objs if o["Key"] not in self._deny]
        if allowed:
            self._real.delete_objects(**{**kw, "Delete": {**kw["Delete"], "Objects": allowed}})
        return {"Errors": [{"Key": o["Key"], "Code": self._code, "Message": "x"}
                           for o in objs if o["Key"] in self._deny]}  # fmt: skip


async def _put(n: int) -> list[str]:
    keys = [f"users/{uuid.uuid4()}/account/test/{i}.png" for i in range(n)]
    for k in keys:
        await storage.put_bytes(k, b"x", "image/png")
    return keys


async def _exists(key: str) -> bool:
    try:
        await storage.get_bytes(key)
        return True
    except Exception:
        return False


async def test_partial_access_denied_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = await _put(3)
    double = _PartialDenial(storage._client(), {keys[1]})
    monkeypatch.setattr(storage, "_client", lambda: double)
    result = await storage.delete_keys(keys)
    assert result is not None, "delete_keys must report per-key outcomes"
    assert set(result.failed) == {keys[1]} and result.failed[keys[1]] == "AccessDenied"
    assert set(result.deleted) == {keys[0], keys[2]}
    monkeypatch.undo()
    assert await _exists(keys[1]) and not await _exists(keys[0])


async def test_transient_errors_are_retried_until_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = await _put(4)
    double = _PartialDenial(storage._client(), {keys[0], keys[3]}, code="SlowDown", times=1)
    monkeypatch.setattr(storage, "_client", lambda: double)
    result = await storage.delete_keys(keys)
    assert result.ok and sorted(result.deleted) == sorted(keys) and double.calls == 2
    monkeypatch.undo()
    assert not any([await _exists(k) for k in keys])


async def test_repeated_deletion_is_idempotent() -> None:
    keys = await _put(2)
    first, second = await storage.delete_keys(keys), await storage.delete_keys(keys)
    assert first.ok and second.ok and sorted(second.deleted) == sorted(keys)


async def test_versioned_bucket_deletes_every_version(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    bucket = f"sp-versioned-{uuid.uuid4().hex[:8]}"
    client = storage._client()
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    monkeypatch.setattr(s, "s3_bucket", bucket)
    keep, gone = "users/x/plain-delete.png", "users/x/durable-delete.png"
    for key in (keep, gone):
        for body in (b"v1", b"v2"):
            await storage.put_bytes(key, body, "image/png")

    def versions(key: str) -> int:
        resp = client.list_object_versions(Bucket=bucket, Prefix=key)
        return len([v for v in resp.get("Versions", []) if v["Key"] == key])

    # what the previous implementation did: a plain delete leaves both versions recoverable
    client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": keep}], "Quiet": True})
    assert versions(keep) == 2
    assert await storage.bucket_versioned()
    result = await storage.delete_keys([gone])
    assert result.ok and versions(gone) == 0
    resp = client.list_object_versions(Bucket=bucket, Prefix=gone)
    assert not [m for m in resp.get("DeleteMarkers", []) if m["Key"] == gone]


async def test_unconfirmed_deletions_keep_a_durable_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import func, select

    from storypanel.db.models import Asset, PendingObjectDeletion, Project, User
    from storypanel.db.session import transaction
    from storypanel.services import maintenance

    uid, pid = uuid.uuid4(), uuid.uuid4()
    keys = await _put(3)
    async with transaction() as db:
        db.add(User(id=uid, auth_subject=f"t:{uid}"))
        await db.flush()
        db.add(Project(id=pid, owner_id=uid, prompt="p"))
        await db.flush()
        for k in keys:
            db.add(Asset(owner_id=uid, project_id=pid, kind="panel", storage_key=k, content_type="image/png",
                         bytes=1, sha256="0" * 64))  # fmt: skip
    double = _PartialDenial(storage._client(), {keys[2]})
    monkeypatch.setattr(storage, "_client", lambda: double)
    await maintenance.purge_project(pid)
    async with transaction() as db:
        assert (await db.execute(select(func.count()).select_from(Asset))).scalar_one() == 0
        pending = (await db.execute(select(PendingObjectDeletion))).scalars().all()
    assert [(p.storage_key, p.last_error, p.owner_id) for p in pending] == [(keys[2], "AccessDenied", uid)]
    assert await _exists(keys[2])
    # still denied: stays recorded; permission fixed: confirmed and cleared
    await maintenance.retry_pending_object_deletions(owner_id=uid)
    async with transaction() as db:
        assert (await db.execute(select(PendingObjectDeletion.attempts))).scalar_one() == 2
    monkeypatch.undo()
    assert await maintenance.retry_pending_object_deletions(owner_id=uid) == 0
    assert not await _exists(keys[2])


# ------------------------------------------------------------------ account deletion (finding 4)
STRIP = "A robot and a girl share noodles in the rain, as a 4 panel strip"


async def _counts(uid: uuid.UUID | None = None) -> dict[str, int]:
    from sqlalchemy import text

    from storypanel.db.session import transaction

    q = {"projects": "select count(*) from projects", "assets": "select count(*) from assets",
         "pending_objects": "select count(*) from pending_object_deletions",
         "ledger": "select count(*) from credit_ledger", "attempts": "select count(*) from provider_attempts"}  # fmt: skip
    async with transaction() as db:
        return {k: int((await db.execute(text(v))).scalar_one()) for k, v in q.items()}


async def _job(uid: uuid.UUID) -> Any:
    from sqlalchemy import select

    from storypanel.db.models import AccountDeletion
    from storypanel.db.session import transaction

    async with transaction() as db:
        return (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uid))).scalar_one()


async def _uid(client: Any, headers: dict[str, str]) -> uuid.UUID:
    return uuid.UUID((await client.get("/v1/me", headers=headers)).json()["id"])


async def test_account_deletion_blocks_purges_and_resists_recreation(client: Any, headers: dict[str, str],
                                                                     worker: Any) -> None:  # fmt: skip
    import asyncio

    from tests.conftest import auth, run_to_completion

    p = await run_to_completion(client, headers, STRIP)
    uid = await _uid(client, headers)
    from sqlalchemy import select

    from storypanel.db.models import Asset, User
    from storypanel.db.session import transaction

    async with transaction() as db:
        keys = list((await db.execute(select(Asset.storage_key))).scalars())
    other = auth("grace@example.com")
    assert (await client.get("/v1/me", headers=other)).status_code == 200
    assert (await client.delete("/v1/me", headers=headers)).status_code == 204
    job = await _job(uid)
    assert job.status == "completed" and job.auth_subject is None and set(job.steps) >= {
        "cancelling", "draining", "purging", "billing", "identity", "anonymising"}  # fmt: skip
    c = await _counts()
    assert c["projects"] == 0 and c["assets"] == 0 and c["pending_objects"] == 0 and c["ledger"] > 0
    assert not any([await _exists(k) for k in keys])
    async with transaction() as db:
        row = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    assert row.email is None and row.auth_subject == f"deleted:{uid}"
    # the retained token (issued before deletion) is dead, for every endpoint
    for method, path in (("GET", "/v1/me"), ("GET", "/v1/projects"), ("GET", f"/v1/projects/{p['id']}"),
                         ("POST", "/v1/projects"), ("DELETE", "/v1/me")):  # fmt: skip
        r = await client.request(
            method, path, headers=headers, json={"prompt": STRIP} if method == "POST" else None
        )
        assert r.status_code == 401, (method, path, r.status_code)
    # a fresh sign-in after completion is a new, empty account without a second welcome grant
    await asyncio.sleep(1.1)
    fresh = auth()
    me = (await client.get("/v1/me", headers=fresh)).json()
    assert uuid.UUID(me["id"]) != uid and me["balance"] == 0
    assert (await client.get("/v1/projects", headers=fresh)).json()["projects"] == []
    # another identity with the same email gets no welcome grant either
    from storypanel.api.deps import issue_dev_token
    from storypanel.config import get_settings

    alias = {
        "Authorization": f"Bearer {issue_dev_token('dev_alias_subject', 'ada@example.com', get_settings())}"
    }
    assert (await client.get("/v1/me", headers=alias)).json()["balance"] == 0
    # someone else's account is untouched
    assert (await client.get("/v1/me", headers=other)).json()["balance"] > 0


async def test_deletion_during_generation_drains_cancels_and_leaves_nothing(
    client: Any, headers: dict[str, str], worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from sqlalchemy import select, text

    from storypanel.db.models import ProviderAttempt
    from storypanel.db.session import transaction
    from storypanel.providers import fake
    from storypanel.services import deletion
    from tests.test_resilience import start

    fake.control.hang_fetches = 1  # one paid image request stays in flight at the provider
    pid = await start(client, headers)
    uid = await _uid(client, headers)
    for _ in range(200):
        await asyncio.sleep(0.1)
        async with transaction() as db:
            started = (
                await db.execute(text("select count(*) from provider_attempts where status = 'started'"))
            ).scalar_one()
        if started:
            break
    assert started
    assert (await client.delete("/v1/me", headers=headers)).status_code == 204
    job = await _job(uid)
    assert job.status == "draining" and "paid provider calls still in flight" in (job.last_error or "")
    assert (await client.get(f"/v1/projects/{pid}", headers=headers)).status_code == 401  # blocked at once
    monkeypatch.setattr(get_settings(), "deletion_drain_seconds", 0)
    assert await deletion.advance(uid) == "completed"
    async with transaction() as db:
        att = (await db.execute(select(ProviderAttempt).where(ProviderAttempt.user_id == uid,
                                                              ProviderAttempt.provider_request_id.is_not(None))
                                .order_by(ProviderAttempt.created_at.desc()).limit(1))).scalar_one()  # fmt: skip
    assert att.status in ("cancelled", "succeeded") and att.cost_counted_usd > 0  # financial record kept
    assert (att.model, att.provider_request_id) in fake.control.cancels or att.status == "succeeded"
    await asyncio.sleep(2)  # any workflow activity still running cannot store content any more
    c = await _counts()
    assert c["projects"] == 0 and c["assets"] == 0


async def test_deletion_during_export_cancels_the_export(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    from tests.conftest import run_to_completion

    p = await run_to_completion(client, headers, STRIP)
    uid = await _uid(client, headers)
    r = await client.post(f"/v1/projects/{p['id']}/exports", headers={**headers, "Idempotency-Key": "e1"},
                          json={"format": "pdf"})  # fmt: skip
    assert r.status_code in (200, 202), r.text
    assert (await client.delete("/v1/me", headers=headers)).status_code == 204
    assert (await _job(uid)).status == "completed"
    import asyncio

    await asyncio.sleep(2)
    c = await _counts()
    assert c["assets"] == 0 and c["projects"] == 0


async def test_concurrent_requests_during_deletion(client: Any, headers: dict[str, str]) -> None:
    import asyncio

    uid = await _uid(client, headers)

    async def create() -> int:
        return (await client.post("/v1/projects", headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                                  json={"prompt": STRIP})).status_code  # fmt: skip

    results = await asyncio.gather(client.delete("/v1/me", headers=headers), *[create() for _ in range(6)],
                                   *[client.delete("/v1/me", headers=headers) for _ in range(2)])  # fmt: skip
    codes = [r if isinstance(r, int) else r.status_code for r in results]
    assert set(codes) <= {201, 202, 204, 401, 409}, codes
    from storypanel.services import deletion

    assert await deletion.advance(uid) == "completed"
    assert (await _counts())["projects"] == 0
    assert (await client.post("/v1/projects", headers={**headers, "Idempotency-Key": "late"},
                              json={"prompt": STRIP})).status_code == 401  # fmt: skip


async def test_billing_failure_keeps_identifiers_and_retries(client: Any, headers: dict[str, str],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from sqlalchemy import update

    from storypanel.db.models import User
    from storypanel.db.session import transaction
    from storypanel.errors import AppError, ErrorCode
    from storypanel.services import billing, deletion

    uid = await _uid(client, headers)
    async with transaction() as db:
        await db.execute(update(User).where(User.id == uid).values(
            stripe_customer_id="cus_del", subscription_id="sub_del", subscription_status="active"))  # fmt: skip
    calls: list[dict[str, Any]] = []

    async def cancel_all(**kw: Any) -> int:
        calls.append(kw)
        if len(calls) == 1:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The payment service is unavailable.")
        return 1

    monkeypatch.setattr(billing, "cancel_all_subscriptions", cancel_all)
    assert (await client.delete("/v1/me", headers=headers)).status_code == 204
    job = await _job(uid)
    assert (
        job.status == "billing"
        and job.stripe_customer_id == "cus_del"
        and job.subscription_ids == ["sub_del"]
    )
    assert job.attempts == 1 and "PROVIDER_ERROR" not in (job.last_error or "x") or job.last_error
    assert (await _counts())["projects"] == 0  # content is already gone; billing cleanup is pending
    assert await deletion.advance(uid) == "completed"
    job = await _job(uid)
    assert job.stripe_customer_id is None and job.subscription_ids == []
    assert calls[-1] == {"customer_id": "cus_del", "subscription_ids": ["sub_del"],
                         "idempotency_prefix": f"deletion:{uid}"}  # fmt: skip


async def test_clerk_identity_is_deleted_and_webhook_is_idempotent(client: Any, headers: dict[str, str],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    import httpx
    from pydantic import SecretStr

    from storypanel.services import deletion

    uid = await _uid(client, headers)
    s = get_settings()
    seen: list[tuple[str, str, str]] = []
    status = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(next(status))

    real = httpx.AsyncClient
    monkeypatch.setattr(
        deletion.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    await deletion.request_deletion(uid, source="user")
    monkeypatch.setattr(s, "auth_mode", "clerk")
    monkeypatch.setattr(s, "clerk_secret_key", SecretStr("sk_clerk_test"))
    assert await deletion.advance(uid) == "identity"  # Clerk answered 503: retried later
    assert (await _job(uid)).auth_subject is not None  # still needed for the retry
    assert await deletion.advance(uid) == "completed"
    subject = seen[0][1].rsplit("/", 1)[1]
    assert seen == [("DELETE", f"/v1/users/{subject}", "Bearer sk_clerk_test")] * 2
    # repeated deliveries / requests do nothing new
    assert await deletion.request_deletion(uid, source="clerk_webhook") == "completed"
    assert await deletion.advance(uid) == "completed" and len(seen) == 2
