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

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from storypanel.db.models import CreditLedger, CreditReservation, CreditReservationEvent, User
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


def _event(resv: CreditReservation, kind: str, amount: int) -> CreditReservationEvent:
    return CreditReservationEvent(reservation_id=resv.id, user_id=resv.user_id, project_id=resv.project_id,
                                  purpose=resv.purpose, kind=kind, amount=amount)  # fmt: skip


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
            select(User.credit_balance, User.credit_reserved)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
    resv = (await db.execute(select(CreditReservation).where(CreditReservation.id == new_id))).scalar_one()
    db.add(_event(resv, "hold", amount))
    return resv


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
            select(CreditReservation)
            .where(CreditReservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
    db.add(_event(resv, "settle", units))  # the money moved in the ledger row above
    return True


async def close(db: AsyncSession, reservation_id: uuid.UUID) -> int:
    """Release whatever was not charged. Safe to call repeatedly."""
    resv = (
        await db.execute(
            select(CreditReservation)
            .where(CreditReservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
        db.add(_event(resv, "release", unused))
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


PURPOSE_LABELS = {"plan": "Cast, sheets and script", "panels": "Illustration", "revision": "Character revision",
                  "regenerate": "Regenerate a panel", "edit": "Fix a panel by instruction"}  # fmt: skip


async def account_view(db: AsyncSession, user_id: uuid.UUID, limit: int = 100) -> dict[str, object]:
    """Open holds and recent activity from immutable records only (ledger rows and hold events).

    Settle events are not listed: the matching charge is the ledger row. Holds made before hold
    events existed are reported with history "unavailable" instead of a reconstructed past."""
    from storypanel.db.models import Project

    titles: dict[uuid.UUID, str | None] = {}

    async def title(pid: uuid.UUID | None) -> str | None:
        if pid is None:
            return None
        if pid not in titles:
            titles[pid] = (await db.execute(select(Project.title).where(
                Project.id == pid, Project.owner_id == user_id))).scalar_one_or_none()  # fmt: skip
        return titles[pid]

    open_rows = (await db.execute(select(CreditReservation).where(
        CreditReservation.user_id == user_id, CreditReservation.status == "open")
        .order_by(CreditReservation.created_at))).scalars().all()  # fmt: skip
    recorded = set((await db.execute(select(CreditReservationEvent.reservation_id).where(
        CreditReservationEvent.user_id == user_id, CreditReservationEvent.kind == "hold"))).scalars())  # fmt: skip
    holds = [{"id": str(r.id), "purpose": r.purpose, "label": PURPOSE_LABELS.get(r.purpose, r.purpose),
              "project_id": str(r.project_id) if r.project_id else None, "project_title": await title(r.project_id),
              "held": r.amount, "charged": r.settled, "remaining": r.amount - r.settled, "opened_at": r.created_at,
              "history": "recorded" if r.id in recorded else "unavailable"} for r in open_rows]  # fmt: skip
    without = (await db.execute(select(func.count()).select_from(CreditReservation).where(
        CreditReservation.user_id == user_id,
        CreditReservation.id.not_in(select(CreditReservationEvent.reservation_id).where(
            CreditReservationEvent.user_id == user_id))))).scalar_one()  # fmt: skip
    since = (await db.execute(select(func.min(CreditReservationEvent.created_at)).where(
        CreditReservationEvent.user_id == user_id))).scalar_one()  # fmt: skip

    ledger = (await db.execute(select(CreditLedger).where(CreditLedger.user_id == user_id)
                               .order_by(CreditLedger.id.desc()).limit(limit))).scalars().all()  # fmt: skip
    events = (await db.execute(select(CreditReservationEvent).where(
        CreditReservationEvent.user_id == user_id, CreditReservationEvent.kind.in_(("hold", "release")))
        .order_by(CreditReservationEvent.id.desc()).limit(limit))).scalars().all()  # fmt: skip
    held_charges = set((await db.execute(select(CreditReservationEvent.reservation_id).where(
        CreditReservationEvent.user_id == user_id, CreditReservationEvent.kind == "hold"))).scalars())  # fmt: skip
    activity: list[dict[str, object]] = []
    for e in ledger:
        # A charge settled from a recorded hold lowers the hold by the same amount: available is unchanged.
        from_hold = e.kind == "charge" and e.reservation_id is not None and e.reservation_id in held_charges
        held_change = e.delta if from_hold else 0
        activity.append({"at": e.created_at, "type": e.kind if e.kind in ("grant", "purchase", "charge", "refund")
                         else "adjust", "reason": e.reason, "project_id": str(e.project_id) if e.project_id else None,
                         "project_title": await title(e.project_id), "balance_change": e.delta,
                         "held_change": held_change, "available_change": e.delta - held_change,
                         "balance_after": e.balance_after, "_order": (e.created_at, 1, e.id)})  # fmt: skip
    for ev in events:
        sign = 1 if ev.kind == "hold" else -1
        verb = "held" if ev.kind == "hold" else "released (not used)"
        activity.append({"at": ev.created_at, "type": ev.kind,
                         "reason": f"{PURPOSE_LABELS.get(ev.purpose, ev.purpose)}: {verb}",
                         "project_id": str(ev.project_id) if ev.project_id else None,
                         "project_title": await title(ev.project_id), "balance_change": 0,
                         "held_change": sign * ev.amount, "available_change": -sign * ev.amount,
                         "balance_after": None, "_order": (ev.created_at, 0 if ev.kind == "hold" else 2, ev.id)})  # fmt: skip
    activity.sort(key=lambda a: a["_order"], reverse=True)  # type: ignore[arg-type,return-value]
    for a in activity:
        a.pop("_order")
    return {
        "holds": holds,
        "activity": activity[:limit],
        "hold_history_since": since,
        "holds_without_history": without,
    }
