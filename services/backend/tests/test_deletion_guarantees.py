"""Account deletion: no new paid work after the request, no indefinite wait on unresolved charges,
and only minimal accounting retained (2026-11-12 verification round)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from storypanel.config import get_settings
from storypanel.db.models import AccountDeletion, CreditLedger, ProviderAttempt, ProviderUsage, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.providers import fake, metering
from storypanel.providers.base import CallContext, ImageRequest
from storypanel.services import deletion


async def _user() -> uuid.UUID:
    async with transaction() as db:
        u = User(id=uuid.uuid4(), auth_subject=f"t:{uuid.uuid4().hex}", email=f"{uuid.uuid4().hex[:8]}@example.com")
        db.add(u)
    return u.id


async def _attempts(uid: uuid.UUID) -> list[ProviderAttempt]:
    async with transaction() as db:
        return list((await db.execute(select(ProviderAttempt).where(ProviderAttempt.user_id == uid)
                                      .order_by(ProviderAttempt.created_at))).scalars())  # fmt: skip


@pytest.fixture(autouse=True)
def _reset_fake() -> Any:
    fake.control.reset()
    yield
    fake.control.reset()


async def test_no_new_paid_call_is_reserved_or_submitted_after_deletion_is_requested() -> None:
    """An activity that was already running when the user asked to be deleted cannot start
    another paid request: the reservation itself is refused, so nothing reaches the provider."""
    uid = await _user()
    ctx = CallContext("panel_generate", str(uid), str(uuid.uuid4()))
    await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"before:{uid}")  # allowed
    submitted = len(fake.control.submits)
    assert await deletion.request_deletion(uid, source="user") == "requested"
    with pytest.raises(AppError) as exc:
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"after:{uid}")
    assert exc.value.code is ErrorCode.FORBIDDEN and not exc.value.retryable  # Temporal does not retry it
    with pytest.raises(AppError):
        await metering.structured(role="light", system="s", user="u", schema=_schema(), ctx=ctx)
    assert len(fake.control.submits) == submitted  # nothing new was sent
    assert len(await _attempts(uid)) == 1  # and nothing new was reserved


def _schema() -> Any:
    from storypanel.domain.schemas import ModerationVerdict

    return ModerationVerdict


async def test_deletion_completes_while_a_charge_is_still_unresolved_and_reconciliation_settles_it_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion waits `deletion_drain_seconds` for in-flight paid calls, then cancels them at the
    provider and moves on. The potential charge stays counted; reconciliation resolves it by
    request id afterwards, without the deletion job waiting for it."""
    uid = await _user()
    ctx = CallContext("panel_generate", str(uid), str(uuid.uuid4()))
    fake.control.pending_fetches = 99  # the provider job never finishes while we watch
    with pytest.raises(AppError):  # the activity gives up waiting (retryable timeout)
        await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"inflight:{uid}", wait_seconds=0.01)
    (att,) = await _attempts(uid)
    assert att.status == "started" and att.provider_request_id
    monkeypatch.setattr(get_settings(), "deletion_drain_seconds", 0)
    await deletion.request_deletion(uid, source="user")
    assert await deletion.advance(uid) == "completed"  # not held up by the unresolved charge
    (att,) = await _attempts(uid)
    assert (att.status, att.cost_basis) == ("cancelled", "unresolved")  # still a potential charge
    assert att.cost_counted_usd == att.cost_estimate_usd > 0
    assert fake.control.cancels == [(att.model, att.provider_request_id)]
    # later, independently: the provider acknowledges the cancel -> resolved, still not assumed free
    fake.control.pending_fetches = 0
    fake.control.cancel_reports_job_failed = True
    async with transaction() as db:
        await db.execute(text("update provider_attempts set updated_at = now() - interval '1 hour' where id = :i"),
                         {"i": att.id})  # fmt: skip
    out = await metering.reconcile_open_requests(older_than=timedelta(minutes=30))
    assert out["failed"] == 1
    (att,) = await _attempts(uid)
    assert (att.status, att.cost_basis, att.error_code) == ("cancel_confirmed", "unresolved", "cancel_acknowledged")


async def test_only_minimal_accounting_is_retained_after_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    from storypanel.services import credits

    uid = await _user()
    ctx = CallContext("character_sheet", str(uid), str(uuid.uuid4()), target_id="captain_gull")
    await metering.generate_image(ImageRequest("p", "4:3"), ctx, key=f"sheet:{uid}")
    async with transaction() as db:
        await credits.grant(db, uid, 5, kind="grant", reason="Character revision", dedupe_key=f"r:{uid}")
    monkeypatch.setattr(get_settings(), "deletion_drain_seconds", 0)
    await deletion.request_deletion(uid, source="user")
    status = await deletion.advance(uid)
    async with transaction() as db:
        err = (await db.execute(select(AccountDeletion.last_error).where(AccountDeletion.user_id == uid))).scalar_one()
    assert status == "completed", err
    async with transaction() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        reasons = list((await db.execute(select(CreditLedger.reason).where(CreditLedger.user_id == uid))).scalars())
        ops = list((await db.execute(select(ProviderUsage).where(ProviderUsage.user_id == uid))).scalars())
        job = (await db.execute(select(AccountDeletion).where(AccountDeletion.user_id == uid))).scalar_one()
    assert user.email is None and user.auth_subject == f"deleted:{uid}"
    assert reasons == ["Character revision"]  # the ledger stays (append-only, financial) with no user text
    (op,) = ops
    assert op.target_id is None  # the character slug is gone
    assert op.provider_request_id and op.cost_counted_usd > Decimal(0)  # reconciliation still possible
    assert job.auth_subject is None and job.stripe_customer_id is None and job.subscription_ids == []


def test_ledger_reasons_never_contain_user_written_text() -> None:
    """Financial records are retained after deletion and the ledger is append-only, so a reason
    may never embed user content (a character's name was embedded before this round)."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src/storypanel"
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "reason" and isinstance(node.value, ast.JoinedStr):
                names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "")
                         for v in node.value.values if isinstance(v, ast.FormattedValue) for n in ast.walk(v.value)}
                if names & {"name", "title", "prompt", "text", "instruction", "email"}:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
