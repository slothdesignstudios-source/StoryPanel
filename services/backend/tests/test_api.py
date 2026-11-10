"""API contract, authorization, ledger, billing webhooks and SSE (no worker needed)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import select, text

from storypanel.db.models import (
    Balloon,
    CreditLedger,
    Outbox,
    Page,
    Panel,
    Project,
    User,
)  # fmt: skip
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.services import credits, events
from tests.conftest import PROMPT, auth


async def _user(client: Any, headers: dict[str, str]) -> uuid.UUID:
    me = (await client.get("/v1/me", headers=headers)).json()
    return uuid.UUID(me["id"])


# ------------------------------------------------------------------ contract & security
async def test_error_contract_and_headers(client: Any, headers: dict[str, str]) -> None:
    r = await client.get("/v1/projects")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "unauthenticated" and err["retryable"] is False
    assert err["request_id"] == r.headers["x-request-id"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in r.headers["content-security-policy"]

    r = await client.post("/v1/projects", json={"prompt": "hi", "pages": 99, "x": 1}, headers=headers)
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert {f["field"] for f in body["fields"]} == {"prompt", "pages", "x"}
    assert "hi" not in json.dumps(body["fields"])  # submitted values are not echoed back

    assert (await client.get("/v1/nope", headers=headers)).json()["error"]["code"] == "not_found"
    bad = await client.get("/v1/projects", headers={"Authorization": "Bearer not.a.jwt"})
    assert bad.status_code == 401 and "Traceback" not in bad.text
    forged = auth()["Authorization"][:-4] + "AAAA"
    assert (await client.get("/v1/projects", headers={"Authorization": forged})).status_code == 401


async def test_origin_allowlist_and_body_limit(client: Any, headers: dict[str, str]) -> None:
    evil = await client.post("/v1/projects", json={"prompt": PROMPT},
                             headers={**headers, "Origin": "https://evil.example"})  # fmt: skip
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "forbidden"
    pre = await client.options("/v1/projects", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})  # fmt: skip
    assert "access-control-allow-origin" not in pre.headers
    ok = await client.options("/v1/projects", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})  # fmt: skip
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    big = await client.post("/v1/projects", content=b"x" * 400_000,
                            headers={**headers, "Content-Type": "application/json"})  # fmt: skip
    assert big.status_code == 413 and big.json()["error"]["code"] == "payload_too_large"


async def test_rate_limit(client: Any, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 3)
    codes = [(await client.get("/v1/me", headers=headers)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429
    other = await client.get("/v1/me", headers=auth("grace@example.com"))
    assert other.status_code == 200  # limits are per user


async def test_create_is_idempotent_and_reserves(client: Any, headers: dict[str, str]) -> None:
    h = {**headers, "Idempotency-Key": "k-1"}
    a = await client.post("/v1/projects", json={"prompt": PROMPT}, headers=h)
    b = await client.post("/v1/projects", json={"prompt": PROMPT}, headers=h)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    clash = await client.post("/v1/projects", json={"prompt": PROMPT + " again"}, headers=h)
    assert clash.status_code == 409
    async with transaction() as db:
        assert len((await db.execute(select(Project))).scalars().all()) == 1
        rows = (await db.execute(select(Outbox))).scalars().all()
        assert [r.kind for r in rows] == ["start_workflow"]  # written with the project, once
        assert rows[0].payload["trace"] is not None
        user = (await db.execute(select(User))).scalar_one()
        assert (user.credit_balance, user.credit_reserved) == (200, 8)
    est = (await client.get("/v1/estimate?pages=4", headers=headers)).json()
    assert est["credits"] == 8 and est["sufficient"] and "about 20 more credits" in est["note"]


async def test_moderation_and_credit_gates_on_create(client: Any, headers: dict[str, str]) -> None:
    r = await client.post(
        "/v1/projects", json={"prompt": "Spider-Man visits a noodle stall"}, headers=headers
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "moderation_blocked"
    assert "original" in r.json()["error"]["fields"][0]["message"].lower()
    uid = await _user(client, headers)
    async with transaction() as db:
        await credits.grant(db, uid, -195, kind="adjust", reason="test", dedupe_key="drain")
    r = await client.post("/v1/projects", json={"prompt": PROMPT}, headers=headers)
    assert r.status_code == 402 and r.json()["error"]["code"] == "insufficient_credits"
    async with transaction() as db:  # the failed request left nothing behind
        assert (await db.execute(select(Project))).scalars().all() == []
        assert (await db.execute(select(Outbox))).scalars().all() == []


# ------------------------------------------------------------------ cross-account access
async def _seed_comic(owner: uuid.UUID) -> dict[str, uuid.UUID]:
    async with transaction() as db:
        project = Project(id=uuid.uuid4(), owner_id=owner, prompt="p", status="completed",
                          style_preset="clean_manga_warm")  # fmt: skip
        page = Page(id=uuid.uuid4(), project_id=project.id, index=0, layout_template_id="w4_grid")
        panel = Panel(id=uuid.uuid4(), project_id=project.id, page_id=page.id, index=0, status="ok",
                      spec={"characters": [], "dialogue": [], "location_id": "x"})  # fmt: skip
        balloon = Balloon(id=uuid.uuid4(), project_id=project.id, panel_id=panel.id, order_index=0,
                          type="caption", text="Hi", geometry={"x": 50, "y": 50, "w": 120, "h": 60})  # fmt: skip
        db.add(project)
        await db.flush()
        db.add(page)
        await db.flush()
        db.add_all([panel])
        await db.flush()
        db.add(balloon)
        return {"project": project.id, "page": page.id, "panel": panel.id, "balloon": balloon.id}


async def test_cross_account_access_is_denied_everywhere(client: Any, headers: dict[str, str]) -> None:
    ids = await _seed_comic(await _user(client, headers))
    mallory = auth("mallory@example.com")
    p, pg, pn, b = ids["project"], ids["page"], ids["panel"], ids["balloon"]
    attempts = [
        ("get", f"/v1/projects/{p}", None), ("delete", f"/v1/projects/{p}", None),
        ("get", f"/v1/projects/{p}/events", None), ("get", f"/v1/projects/{p}/estimate", None),
        ("post", f"/v1/projects/{p}/cancel", None), ("post", f"/v1/projects/{p}/resume", None),
        ("post", f"/v1/projects/{p}/exports", {"format": "pdf"}),
        ("post", f"/v1/projects/{p}/characters/approve", {"cast_version": 1}),
        ("post", f"/v1/projects/{p}/script/approve", {"script_version": 1}),
        ("post", f"/v1/projects/{p}/brief/answers", {"answers": {}}),
        ("post", f"/v1/panels/{pn}/regenerate", None),
        ("post", f"/v1/panels/{pn}/edit", {"instruction": "make it rain"}),
        ("get", f"/v1/panels/{pn}/versions", None),
        ("post", f"/v1/panels/{pn}/versions/{uuid.uuid4()}/restore", None),
        ("post", f"/v1/panels/{pn}/balloons", {"type": "caption", "text": "x"}),
        ("patch", f"/v1/balloons/{b}", {"version": 1, "text": "pwned"}),
        ("delete", f"/v1/balloons/{b}", None),
        ("patch", f"/v1/pages/{pg}/layout", {"layout_template_id": "w4_top_large"}),
        ("post", f"/v1/pages/{pg}/reorder", {"panel_ids": [str(pn)]}),
        ("post", f"/v1/exports/{uuid.uuid4()}/retry", None),
    ]  # fmt: skip
    for method, url, body in attempts:
        r = await client.request(method, url, json=body, headers=mallory)
        # identical to "does not exist": ownership cannot be probed
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        assert r.json()["error"]["code"] == "not_found"
    assert (await client.get("/v1/projects", headers=mallory)).json()["projects"] == []
    async with transaction() as db:
        assert (await db.execute(select(Balloon.text))).scalar_one() == "Hi"
    # the owner can still use the same routes
    ok = await client.patch(f"/v1/balloons/{b}", json={"version": 1, "text": "Mine"}, headers=headers)
    assert ok.status_code == 200 and ok.json()["user_edited"] is True


async def test_editor_layout_reorder_and_balloon_rules(client: Any, headers: dict[str, str]) -> None:
    owner = await _user(client, headers)
    ids = await _seed_comic(owner)
    async with transaction() as db:
        for n in range(1, 4):
            db.add(Panel(id=uuid.uuid4(), project_id=ids["project"], page_id=ids["page"], index=n,
                         status="ok", spec={"characters": [], "dialogue": [], "location_id": "x"}))  # fmt: skip
    project = (await client.get(f"/v1/projects/{ids['project']}", headers=headers)).json()
    page = project["pages"][0]
    assert {o["id"] for o in page["layout_options"]} >= {"w4_grid", "w4_top_large"}
    order = [p["id"] for p in page["panels"]]
    r = await client.post(f"/v1/pages/{page['id']}/reorder", headers=headers,
                          json={"panel_ids": order[::-1]})  # fmt: skip
    assert r.status_code == 200, r.text
    after = (await client.get(f"/v1/projects/{ids['project']}", headers=headers)).json()
    assert [p["id"] for p in after["pages"][0]["panels"]] == order[::-1]
    moved = after["pages"][0]["panels"][-1]  # the balloon travelled with its panel
    assert moved["balloons"] and moved["balloons"][0]["geometry"]["x"] >= moved["rect"][0]
    assert after["edit_version"] == project["edit_version"] + 1
    bad = await client.post(f"/v1/pages/{page['id']}/reorder", headers=headers, json={"panel_ids": order[:2]})
    assert bad.status_code == 422
    r = await client.patch(f"/v1/pages/{page['id']}/layout", headers=headers,
                           json={"layout_template_id": "w4_top_large"})  # fmt: skip
    assert r.status_code == 200
    wrong = await client.patch(f"/v1/pages/{page['id']}/layout", headers=headers,
                               json={"layout_template_id": "w6_grid"})  # fmt: skip
    assert wrong.status_code == 422
    b = moved["balloons"][0]
    long = await client.patch(f"/v1/balloons/{b['id']}", headers=headers,
                              json={"version": b["version"] + 1, "text": " ".join(["w"] * 26)})  # fmt: skip
    assert long.status_code == 422 and long.json()["error"]["fields"][0]["field"] == "text"
    blocked = await client.patch(f"/v1/balloons/{b['id']}", headers=headers,
                                 json={"version": b["version"] + 1, "text": "Batman says hi"})  # fmt: skip
    assert blocked.json()["error"]["code"] == "moderation_blocked"
    for n in range(2):
        ok = await client.post(f"/v1/panels/{moved['id']}/balloons", headers=headers,
                               json={"type": "caption", "text": f"Extra {n}"})  # fmt: skip
        assert ok.status_code == 201
    full = await client.post(f"/v1/panels/{moved['id']}/balloons", headers=headers,
                             json={"type": "caption", "text": "Too many"})  # fmt: skip
    assert full.status_code == 422  # three balloons per panel


# ------------------------------------------------------------------ credit ledger
async def test_concurrent_reservations_cannot_overspend(client: Any, headers: dict[str, str]) -> None:
    uid = await _user(client, headers)  # 200 credits

    async def attempt(n: int) -> bool:
        try:
            async with transaction() as db:
                await credits.reserve(db, uid, 30, purpose="t", idempotency_key=f"race-{n}")
            return True
        except AppError as exc:
            assert exc.code is ErrorCode.INSUFFICIENT_CREDITS
            return False

    results = await asyncio.gather(*[attempt(n) for n in range(20)])
    assert sum(results) == 6  # 6 x 30 = 180 <= 200 < 210
    async with transaction() as db:
        bal = await credits.get_balance(db, uid)
        assert (bal.balance, bal.reserved, bal.available) == (200, 180, 20)
        assert await credits.reconcile(db) == []


async def test_reserve_charge_release_are_idempotent(client: Any, headers: dict[str, str]) -> None:
    uid = await _user(client, headers)
    async with transaction() as db:
        r1 = await credits.reserve(db, uid, 10, purpose="t", idempotency_key="same")
    async with transaction() as db:
        r2 = await credits.reserve(db, uid, 10, purpose="t", idempotency_key="same")
        assert r1.id == r2.id and (await credits.get_balance(db, uid)).reserved == 10

    async def charge(key: str, units: int = 1) -> bool:
        async with transaction() as db:
            return await credits.charge(db, r1.id, units, reason="unit", dedupe_key=key)

    applied = await asyncio.gather(*[charge("unit-1") for _ in range(8)], charge("unit-2"))
    assert sum(applied) == 2  # a retried activity charges once
    assert not await charge("too-much", 9)  # never more than was reserved
    async with transaction() as db:
        assert await credits.close(db, r1.id) == 8
        assert await credits.close(db, r1.id) == 0
        bal = await credits.get_balance(db, uid)
        assert (bal.balance, bal.reserved) == (198, 0)
        assert await credits.reconcile(db) == []
    assert not await charge("after-close")
    async with transaction() as db:  # a clawback never cuts into credits held for running work
        await credits.reserve(db, uid, 100, purpose="t", idempotency_key="held")
        await credits.grant(db, uid, -500, kind="refund", reason="r", dedupe_key="claw")
        bal = await credits.get_balance(db, uid)
        assert (bal.balance, bal.reserved) == (100, 100) and await credits.reconcile(db) == []


async def test_ledger_is_append_only(client: Any, headers: dict[str, str]) -> None:
    await _user(client, headers)
    for stmt in ("update credit_ledger set delta = 1000000", "delete from credit_ledger"):
        with pytest.raises(Exception, match="append-only"):
            async with transaction() as db:
                await db.execute(text(stmt))
    async with transaction() as db:
        rows = (await db.execute(select(CreditLedger))).scalars().all()
        assert [(r.delta, r.kind, r.balance_after) for r in rows] == [(200, "grant", 200)]


# ------------------------------------------------------------------ stripe webhooks
def _sign(payload: bytes, secret: str = "whsec_test_secret", ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


async def _hook(client: Any, event: dict[str, Any], **kw: Any) -> httpx.Response:
    payload = json.dumps(event).encode()
    return await client.post("/webhooks/stripe", content=payload,
                             headers={"stripe-signature": kw.get("sig") or _sign(payload, **kw)})  # fmt: skip


async def test_webhook_signature_duplicates_and_ordering(client: Any, headers: dict[str, str]) -> None:
    uid = str(await _user(client, headers))
    sub = lambda eid, created, status, price="price_pro": {  # noqa: E731
        "id": eid, "type": "customer.subscription.updated", "created": created,
        "data": {"object": {"id": "sub_1", "status": status, "metadata": {"user_id": uid},
                            "items": {"data": [{"price": {"id": price}}]}}}}  # fmt: skip
    assert (await _hook(client, sub("evt_x", 100, "active"), sig="t=1,v1=bad")).status_code == 403
    assert (await _hook(client, sub("evt_x", 100, "active"), secret="whsec_wrong")).status_code == 403
    stale = await _hook(client, sub("evt_x", 100, "active"), ts=int(time.time()) - 3600)
    assert stale.status_code == 403  # replay of an old signed payload
    async with transaction() as db:
        assert (await db.execute(select(User.plan))).scalar_one() == "free"

    assert (await _hook(client, sub("evt_2", 200, "active"))).json() == {"status": "subscription_updated"}
    assert (await _hook(client, sub("evt_2", 200, "active"))).json() == {"status": "duplicate"}
    # an older event delivered late must not roll the subscription back
    assert (await _hook(client, sub("evt_1", 150, "canceled"))).json() == {"status": "stale"}
    async with transaction() as db:
        assert (await db.execute(select(User.plan))).scalar_one() == "pro"

    invoice = {"id": "evt_inv", "type": "invoice.paid", "created": 210, "data": {"object": {
        "id": "in_1", "customer": "cus_1", "metadata": {"user_id": uid},
        "lines": {"data": [{"price": {"id": "price_pro"}}]}}}}  # fmt: skip
    assert (await _hook(client, invoice)).json() == {"status": "credits_granted"}
    again = {**invoice, "id": "evt_inv_redelivered_under_new_id"}
    await _hook(client, again)  # a second event about the same invoice grants nothing more
    pack = {"id": "evt_pack", "type": "checkout.session.completed", "created": 220, "data": {"object": {
        "id": "cs_1", "mode": "payment", "payment_status": "paid", "customer": "cus_1",
        "client_reference_id": uid, "metadata": {"user_id": uid, "product": "credit_pack"}}}}  # fmt: skip
    results = await asyncio.gather(*[_hook(client, pack) for _ in range(5)])  # concurrent delivery
    assert sorted(r.json()["status"] for r in results) == ["credits_granted"] + ["duplicate"] * 4
    refund = lambda eid, refunded: {
        "id": eid,
        "type": "charge.refunded",
        "created": 230,
        "data": {  # noqa: E731
            "object": {"id": "ch_1", "customer": "cus_1", "amount": 1000, "amount_refunded": refunded}
        },
    }
    await _hook(client, refund("evt_r1", 500))
    await _hook(client, refund("evt_r2", 1000))
    async with transaction() as db:
        user = (await db.execute(select(User))).scalar_one()
        assert user.credit_balance == 200 + 240 + 40 - 20 - 20
        assert await credits.reconcile(db) == []
    out = (await client.get("/v1/credits", headers=headers)).json()
    assert out["plan"] == "pro" and [h["kind"] for h in out["history"][:2]] == ["refund", "refund"]


# ------------------------------------------------------------------ SSE over real HTTP
async def test_sse_replay_reconnect_and_reset(app: Any, headers: dict[str, str]) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=10) as http:
            uid = uuid.UUID((await http.get("/v1/me", headers=headers)).json()["id"])
            ids = await _seed_comic(uid)
            pid = ids["project"]

            async def emit(n: int) -> None:
                async with transaction() as db:
                    await events.emit(db, pid, "panel.updated", {"n": n})
                    await events.notify_after_commit(db)

            async def read(count: int, last: str | None) -> list[tuple[int, dict[str, Any]]]:
                got: list[tuple[int, dict[str, Any]]] = []
                h = {**headers, **({"Last-Event-ID": last} if last else {})}
                async with http.stream("GET", f"/v1/projects/{pid}/events", headers=h) as resp:
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    seq, name = -1, ""
                    async for line in resp.aiter_lines():
                        if line.startswith("id: "):
                            seq = int(line[4:])
                        elif line.startswith("event: "):
                            name = line[7:]
                        elif line.startswith("data: "):
                            got.append(
                                (seq if name != "reset" else -1, {"event": name, **json.loads(line[6:])})
                            )
                            if len(got) == count:
                                break
                return got

            for n in (1, 2, 3):
                await emit(n)
            first = await read(2, None)
            assert [s for s, _ in first] == [1, 2]
            # while disconnected, more events happen; a live one arrives after reconnecting
            await emit(4)
            reader = asyncio.create_task(read(3, "2"))
            await asyncio.sleep(0.4)
            await emit(5)
            resumed = await asyncio.wait_for(reader, 8)
            assert [s for s, _ in resumed] == [3, 4, 5]  # nothing missed, nothing repeated
            assert [d["n"] for _, d in resumed] == [3, 4, 5]
            # retention removed the events this client still needs: it is told to refetch
            async with transaction() as db:
                await db.execute(text("delete from project_events where seq <= 4"))
            after_purge = await read(2, "1")
            assert after_purge[0][1]["event"] == "reset" and after_purge[1][0] == 5
            denied = await http.get(f"/v1/projects/{pid}/events", headers=auth("mallory@example.com"))
            assert denied.status_code == 404
    finally:
        server.should_exit = True
        await task
