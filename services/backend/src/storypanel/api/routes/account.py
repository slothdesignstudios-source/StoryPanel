from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from svix.webhooks import Webhook, WebhookVerificationError

from storypanel import storage
from storypanel.api import schemas as api
from storypanel.api.deps import AuthUser, current_user, issue_dev_token, rate_limit
from storypanel.api.routes.projects import ERR
from storypanel.catalog import styles
from storypanel.config import get_settings
from storypanel.db.models import CreditLedger, Project, User
from storypanel.db.session import transaction
from storypanel.domain.states import ACTIVE
from storypanel.errors import AppError, ErrorCode, not_found
from storypanel.obs import metrics
from storypanel.services import billing, credits, events, maintenance, outbox, repo

router = APIRouter(tags=["account"])


def _prices() -> dict[str, int]:
    s = get_settings()
    return {"cast": s.credits_cast, "script": s.credits_script, "panel": s.credits_per_panel,
            "regenerate": s.credits_regenerate, "edit": s.credits_edit,
            "character_revision": s.credits_character_revision, "credit_pack": s.credit_pack_size}  # fmt: skip


@router.get("/v1/meta", response_model=api.MetaOut)
async def meta(request: Request) -> api.MetaOut:
    """Public product catalog: styles, limits and credit prices."""
    s, cat = get_settings(), styles()
    return api.MetaOut(
        styles=[api.StyleOut(id=p.id, name=p.name, blurb=p.blurb, swatch=list(p.swatch),
                             available=True) for p in cat.presets.values()],
        default_style=cat.default, default_pages=s.default_pages, max_pages=s.max_pages,
        limits={"max_characters": s.max_characters, "max_characters_per_panel": s.max_characters_per_panel,
                "max_words_per_balloon": s.max_words_per_balloon,
                "max_balloons_per_panel": s.max_balloons_per_panel},
        credit_prices=_prices(), auth_mode=s.auth_mode, provider_mode=s.provider_mode)  # fmt: skip


@router.get("/v1/me", response_model=api.MeOut, responses=ERR)
async def me(user: AuthUser = Depends(current_user)) -> api.MeOut:
    async with transaction() as db:
        row = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        return api.MeOut(id=str(row.id), email=row.email, plan=row.plan, balance=row.credit_balance,
                         available=row.credit_balance - row.credit_reserved)  # fmt: skip


@router.delete("/v1/me", status_code=204, responses=ERR)
async def delete_account(request: Request, user: AuthUser = Depends(current_user)) -> None:
    """Delete the account and all of its comics, images and exports."""
    async with transaction() as db:
        rows = (await db.execute(select(Project.workflow_id).where(
            Project.owner_id == user.id, Project.status.in_([s.value for s in ACTIVE])))).scalars()  # fmt: skip
        for wf in rows:
            if wf:
                await outbox.enqueue(db, "cancel", f"cancel:{wf}", {"workflow_id": wf})
        await repo.audit(db, user_ref=user.ref, action="account.delete",
                         request_id=request.state.request_id)  # fmt: skip
    request.app.state.outbox_wake.set()
    await maintenance.purge_user(user.id)


@router.get("/v1/credits", response_model=api.CreditsOut, responses=ERR)
async def get_credits(user: AuthUser = Depends(current_user)) -> api.CreditsOut:
    """Balance and usage history."""
    s = get_settings()
    async with transaction() as db:
        row = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        ledger = (await db.execute(select(CreditLedger).where(CreditLedger.user_id == user.id)
                                   .order_by(CreditLedger.id.desc()).limit(100))).scalars()  # fmt: skip
        history = [api.LedgerEntry(id=e.id, delta=e.delta, kind=e.kind, reason=e.reason,
                                   project_id=str(e.project_id) if e.project_id else None,
                                   balance_after=e.balance_after, created_at=e.created_at)
                   for e in ledger]  # fmt: skip
        return api.CreditsOut(
            plan=row.plan, balance=row.credit_balance, reserved=row.credit_reserved,
            available=row.credit_balance - row.credit_reserved,
            subscription_status=row.subscription_status, history=history, prices=_prices(),
            plans=s.plan_monthly_credits, billing_enabled=billing.enabled(s))  # fmt: skip


@router.post("/v1/billing/checkout", response_model=api.UrlOut, responses=ERR)
async def checkout(body: api.Checkout, user: AuthUser = Depends(current_user)) -> api.UrlOut:
    async with transaction() as db:
        return api.UrlOut(url=await billing.create_checkout(db, user.id, body.product))


@router.post("/v1/billing/portal", response_model=api.UrlOut, responses=ERR)
async def portal(user: AuthUser = Depends(current_user)) -> api.UrlOut:
    async with transaction() as db:
        return api.UrlOut(url=await billing.create_portal(db, user.id))


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    event = billing.verify_webhook(payload, request.headers.get("stripe-signature"))
    async with transaction() as db:  # the dedupe row and the effects commit together
        outcome = await billing.handle_event(db, event)
    return {"status": outcome}


@router.post("/webhooks/clerk", include_in_schema=False)
async def clerk_webhook(request: Request) -> dict[str, str]:
    """Clerk user lifecycle (Svix-signed). user.deleted triggers full data deletion."""
    s = get_settings()
    if s.clerk_webhook_secret is None:
        raise not_found("Webhook")
    payload = await request.body()
    try:
        event = Webhook(s.clerk_webhook_secret.get_secret_value()).verify(payload, dict(request.headers))
    except WebhookVerificationError as exc:
        metrics.webhook_events.labels("clerk", "bad_signature").inc()
        raise AppError(ErrorCode.FORBIDDEN, "Invalid webhook signature.") from exc
    type_, data = event.get("type"), event.get("data") or {}
    if type_ == "user.deleted" and data.get("id"):
        async with transaction() as db:
            uid = (
                await db.execute(select(User.id).where(User.auth_subject == data["id"]))
            ).scalar_one_or_none()
        if uid:
            await maintenance.purge_user(uid)
    metrics.webhook_events.labels(f"clerk.{type_}", "ok").inc()
    return {"status": "ok"}


@router.post("/v1/dev/login", response_model=api.TokenOut, include_in_schema=False)
async def dev_login(body: api.DevLogin, request: Request) -> api.TokenOut:
    """Local/test only. Not registered when SP_AUTH_MODE=clerk, and Settings validation
    refuses dev auth in staging and production."""
    s = get_settings()
    if s.auth_mode != "dev" or s.is_deployed:
        raise not_found("Route")
    await rate_limit(f"devlogin:{request.client.host if request.client else 'x'}", 30, 60)
    subject = "dev_" + uuid.uuid5(uuid.NAMESPACE_DNS, body.email.lower()).hex[:24]
    return api.TokenOut(token=issue_dev_token(subject, body.email.lower(), s), expires_in=12 * 3600)


@router.get("/healthz", response_model=api.HealthOut, include_in_schema=False)
async def liveness() -> api.HealthOut:
    return api.HealthOut(status="ok", checks={}, release=get_settings().release)


@router.get("/readyz", response_model=api.HealthOut, include_in_schema=False)
async def readiness(request: Request) -> api.HealthOut:
    checks: dict[str, bool] = {}
    try:
        async with transaction() as db:
            await db.execute(text("select 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    try:
        checks["redis"] = bool(await events.redis_client().ping())
    except Exception:
        checks["redis"] = False
    checks["storage"] = await storage.healthy()
    checks["temporal"] = getattr(request.app.state, "temporal", None) is not None
    for name, ok in checks.items():
        metrics.dependency_up.labels(name).set(1 if ok else 0)
    ok = all(checks.values()) and not request.app.state.shutting_down
    if not ok:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={  # type: ignore[return-value]
            "status": "unavailable", "checks": checks, "release": get_settings().release})  # fmt: skip
    return api.HealthOut(status="ok", checks=checks, release=get_settings().release)


__all__ = ["router", "credits"]
