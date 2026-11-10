"""Append-only credit ledger with atomic reservations.

Billing policy (documented in docs/billing-policy.md):
- Work is reserved up front at its maximum price and settled per delivered unit.
- Automatic QA retries are never charged; they are a platform cost.
- User-driven regenerate/edit/revision is charged only if it produces a visible result.
- Safety-blocked, failed and cancelled units are released, not charged.
Customer credits are separate from provider cost (see ProviderUsage).

Concurrency: every mutation is a single guarded UPDATE on the user row, so two requests
cannot both spend the same credits, and every ledger row carries a unique dedupe key so a
retried activity or a replayed webhook cannot apply twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.db.models import CreditLedger, CreditReservation, User
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics


@dataclass(frozen=True)
class Balance:
    balance: int
    reserved: int

    @property
    def available(self) -> int:
        return self.balance - self.reserved


async def get_balance(db: AsyncSession, user_id: uuid.UUID) -> Balance:
    row = (
        await db.execute(select(User.credit_balance, User.credit_reserved).where(User.id == user_id))
    ).one()
    return Balance(row[0], row[1])


async def _append(
    db: AsyncSession, *, user_id: uuid.UUID, delta: int, kind: str, reason: str, dedupe_key: str,
    balance_after: int, project_id: uuid.UUID | None = None, job_ref: str | None = None,
    reservation_id: uuid.UUID | None = None,
) -> None:  # fmt: skip
    db.add(CreditLedger(user_id=user_id, delta=delta, kind=kind, reason=reason,
                        dedupe_key=dedupe_key, balance_after=balance_after, project_id=project_id,
                        job_ref=job_ref, reservation_id=reservation_id))  # fmt: skip
    metrics.credit_events.labels(kind).inc()


async def _already_applied(db: AsyncSession, dedupe_key: str) -> bool:
    return (
        await db.execute(select(CreditLedger.id).where(CreditLedger.dedupe_key == dedupe_key))
    ).first() is not None


async def grant(
    db: AsyncSession, user_id: uuid.UUID, amount: int, *, kind: str, reason: str, dedupe_key: str
) -> bool:
    """Add (or, for refunds/adjustments, remove) credits. Returns False if already applied."""
    # The advisory lock serialises concurrent deliveries of the same dedupe key.
    await db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": dedupe_key})
    if await _already_applied(db, dedupe_key):
        return False
    bal, held = (
        await db.execute(
            select(User.credit_balance, User.credit_reserved).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )  # fmt: skip
    ).one()
    # A clawback never cuts into credits already promised to running work; the ledger
    # records the delta that was actually applied so it always sums to the balance.
    new_balance = max(bal + amount, held)
    amount = new_balance - bal
    await db.execute(update(User).where(User.id == user_id).values(credit_balance=new_balance))
    await _append(db, user_id=user_id, delta=amount, kind=kind, reason=reason,
                  dedupe_key=dedupe_key, balance_after=new_balance)  # fmt: skip
    return True


async def reserve(
    db: AsyncSession, user_id: uuid.UUID, amount: int, *, purpose: str, idempotency_key: str,
    project_id: uuid.UUID | None = None,
) -> CreditReservation:  # fmt: skip
    """Hold credits for upcoming work. Idempotent on `idempotency_key`."""
    if amount <= 0:
        raise AppError(ErrorCode.VALIDATION, "Reservation amount must be positive.")
    stmt = (
        insert(CreditReservation)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            project_id=project_id,
            purpose=purpose,
            amount=amount,
            idempotency_key=idempotency_key,
        )  # fmt: skip
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(CreditReservation.id)
    )
    new_id = (await db.execute(stmt)).scalar_one_or_none()
    if new_id is None:  # same request seen before: the hold already exists
        return (
            await db.execute(
                select(CreditReservation).where(CreditReservation.idempotency_key == idempotency_key)
            )
        ).scalar_one()
    held = (
        await db.execute(
            update(User)
            .where(User.id == user_id, User.credit_balance - User.credit_reserved >= amount)
            .values(credit_reserved=User.credit_reserved + amount)
            .returning(User.id)
        )
    ).scalar_one_or_none()
    if held is None:
        metrics.credit_events.labels("insufficient").inc()
        raise AppError(ErrorCode.INSUFFICIENT_CREDITS,
                       "You don't have enough credits for this. Add credits to continue.")  # fmt: skip
    metrics.credit_events.labels("reserve").inc()
    return (await db.execute(select(CreditReservation).where(CreditReservation.id == new_id))).scalar_one()


async def charge(
    db: AsyncSession, reservation_id: uuid.UUID, units: int, *, reason: str, dedupe_key: str,
    job_ref: str | None = None,
) -> bool:  # fmt: skip
    """Settle `units` of an open reservation as a real charge. Idempotent on dedupe_key."""
    if units <= 0:
        return False
    await db.execute(text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": dedupe_key})
    if await _already_applied(db, dedupe_key):
        return False
    resv = (
        await db.execute(
            select(CreditReservation).where(CreditReservation.id == reservation_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()
    if resv.status != "open" or resv.settled + units > resv.amount:
        # Never charge beyond what the customer agreed to when the work was reserved.
        metrics.credit_events.labels("charge_rejected").inc()
        return False
    resv.settled += units
    new_balance = (
        await db.execute(
            update(User)
            .where(User.id == resv.user_id)
            .values(credit_balance=User.credit_balance - units, credit_reserved=User.credit_reserved - units)
            .returning(User.credit_balance)
        )  # fmt: skip
    ).scalar_one()
    await _append(db, user_id=resv.user_id, delta=-units, kind="charge", reason=reason,
                  dedupe_key=dedupe_key, balance_after=new_balance, project_id=resv.project_id,
                  job_ref=job_ref, reservation_id=resv.id)  # fmt: skip
    return True


async def close(db: AsyncSession, reservation_id: uuid.UUID) -> int:
    """Release whatever was not charged. Safe to call repeatedly."""
    resv = (
        await db.execute(
            select(CreditReservation).where(CreditReservation.id == reservation_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if resv is None or resv.status != "open":
        return 0
    unused = resv.amount - resv.settled
    resv.status = "closed"
    if unused:
        await db.execute(
            update(User).where(User.id == resv.user_id)
            .values(credit_reserved=User.credit_reserved - unused)
        )  # fmt: skip
    metrics.credit_events.labels("release").inc()
    return unused


async def close_for_project(db: AsyncSession, project_id: uuid.UUID) -> int:
    ids = (
        await db.execute(
            select(CreditReservation.id).where(
                CreditReservation.project_id == project_id, CreditReservation.status == "open"
            )
        )
    ).scalars().all()  # fmt: skip
    return sum([await close(db, rid) for rid in ids])


async def open_reservation(db: AsyncSession, project_id: uuid.UUID, purpose: str) -> CreditReservation | None:
    return (
        await db.execute(
            select(CreditReservation)
            .where(CreditReservation.project_id == project_id,
                   CreditReservation.purpose == purpose, CreditReservation.status == "open")
            .order_by(CreditReservation.created_at.desc())
        )  # fmt: skip
    ).scalars().first()  # fmt: skip


async def reconcile(db: AsyncSession) -> list[dict[str, object]]:
    """Find users whose cached balance or holds disagree with the ledger of record."""
    rows = (
        await db.execute(
            text(
                """
                select u.id, u.credit_balance, u.credit_reserved,
                       coalesce(l.total, 0) as ledger_total, coalesce(r.held, 0) as held
                from users u
                left join (select user_id, sum(delta) total from credit_ledger group by 1) l
                       on l.user_id = u.id
                left join (select user_id, sum(amount - settled) held from credit_reservations
                           where status = 'open' group by 1) r on r.user_id = u.id
                where u.credit_balance <> coalesce(l.total, 0)
                   or u.credit_reserved <> coalesce(r.held, 0)
                """
            )
        )
    ).mappings().all()  # fmt: skip
    metrics.credit_reconciliation_drift.set(len(rows))
    return [
        {"user_id": str(r["id"]), "balance": r["credit_balance"], "ledger_total": r["ledger_total"],
         "reserved": r["credit_reserved"], "open_holds": r["held"]}
        for r in rows
    ]  # fmt: skip
