"""Export retries (review finding 7): asserted through real outbox delivery and workflow
execution, not just the API's returned status."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select, text

from storypanel.db.models import Export
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake
from tests.conftest import run_to_completion, wait_for


def _fail_upscale(n: int) -> None:
    fake.control.errors["upscale"] = [
        AppError(ErrorCode.VALIDATION, "rejected", retryable=False) for _ in range(n)
    ]


async def _export(
    client: Any, headers: dict[str, str], pid: str, export_id: str, want: tuple[str, ...]
) -> dict[str, Any]:
    p = await wait_for(client, pid, headers,
                       lambda p: any(e["id"] == export_id and e["status"] in want for e in p["exports"]))  # fmt: skip
    return next(e for e in p["exports"] if e["id"] == export_id)  # type: ignore[no-any-return]


async def test_failed_export_can_be_retried_more_than_once(
    client: Any, headers: dict[str, str], worker: Any
) -> None:
    p = await run_to_completion(client, headers)
    pid = p["id"]
    _fail_upscale(100)  # every upscale fails until cleared
    r = await client.post(f"/v1/projects/{pid}/exports", json={"format": "pdf"}, headers=headers)
    export_id = r.json()["id"]
    assert (await _export(client, headers, pid, export_id, ("failed", "ready")))["status"] == "failed"

    # the first retry fails too
    assert (await client.post(f"/v1/exports/{export_id}/retry", headers=headers)).status_code == 202
    await asyncio.sleep(1.0)
    assert (await _export(client, headers, pid, export_id, ("failed", "ready")))["status"] == "failed"
    await asyncio.sleep(1.0)  # let any page activity still running finish
    fake.control.errors.pop("upscale", None)

    # the second retry must actually start a workflow and finish
    assert (await client.post(f"/v1/exports/{export_id}/retry", headers=headers)).status_code == 202
    done = await _export(client, headers, pid, export_id, ("failed", "ready"))
    assert done["status"] == "ready" and done.get("download_url")
    async with transaction() as db:
        starts = (await db.execute(text(
            "select dedupe_key, status from outbox where kind = 'start_workflow' "
            "and payload->'input'->>'export_id' = :e order by id"), {"e": export_id})).all()  # fmt: skip
        export = (await db.execute(select(Export).where(Export.id == export_id))).scalar_one()
    assert len(starts) == 3 and all(s == "delivered" for _, s in starts), starts
    assert len({k for k, _ in starts}) == 3
    assert export.attempt == 3


async def test_duplicate_and_concurrent_retries_start_one_workflow(client: Any, headers: dict[str, str],
                                                                   worker: Any) -> None:  # fmt: skip
    p = await run_to_completion(client, headers)
    pid = p["id"]
    _fail_upscale(100)
    r = await client.post(f"/v1/projects/{pid}/exports", json={"format": "png"}, headers=headers)
    export_id = r.json()["id"]
    assert (await _export(client, headers, pid, export_id, ("failed", "ready")))["status"] == "failed"
    await asyncio.sleep(1.0)
    fake.control.errors.pop("upscale", None)
    codes = [x.status_code for x in await asyncio.gather(
        *[client.post(f"/v1/exports/{export_id}/retry", headers=headers) for _ in range(5)])]  # fmt: skip
    assert codes.count(202) == 1 and codes.count(409) == 4, codes
    assert (await _export(client, headers, pid, export_id, ("failed", "ready")))["status"] == "ready"
    async with transaction() as db:
        starts = (await db.execute(text(
            "select count(*) from outbox where kind = 'start_workflow' and payload->'input'->>'export_id' = :e"),
            {"e": export_id})).scalar_one()  # fmt: skip
    assert starts == 2  # the original and exactly one retry
