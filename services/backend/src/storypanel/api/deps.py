"""Authentication, rate limiting and idempotency dependencies."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from storypanel.config import Settings, get_settings
from storypanel.db.models import IdempotencyKey, User
from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.obs.logging import bind_context, pseudonymize
from storypanel.services import credits
from storypanel.services.events import redis_client

DEV_ISSUER = "storypanel-dev"
_jwks: jwt.PyJWKClient | None = None


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    subject: str
    plan: str
    ref: str  # pseudonymous id for logs and audit


def issue_dev_token(subject: str, email: str, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": subject, "email": email, "iss": DEV_ISSUER, "iat": now, "exp": now + 12 * 3600},
        settings.dev_auth_secret.get_secret_value(), algorithm="HS256",
    )  # fmt: skip


def _verify(token: str, s: Settings) -> dict[str, Any]:
    """Verify a session token. Clerk tokens are RS256 JWTs checked against Clerk's JWKS,
    with issuer, expiry and authorised-party (azp) validation."""
    global _jwks
    try:
        if s.auth_mode == "dev":
            return jwt.decode(token, s.dev_auth_secret.get_secret_value(), algorithms=["HS256"],
                              issuer=DEV_ISSUER, options={"require": ["exp", "sub", "iss"]})  # fmt: skip
        if _jwks is None:
            assert s.clerk_jwks_url
            _jwks = jwt.PyJWKClient(s.clerk_jwks_url, cache_keys=True, lifespan=3600, timeout=5)
        key = _jwks.get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token, key, algorithms=["RS256"], issuer=s.clerk_issuer, leeway=5,
            options={"require": ["exp", "nbf", "sub", "iss"]})  # fmt: skip
        if s.clerk_authorized_parties and claims.get("azp") not in s.clerk_authorized_parties:
            raise jwt.InvalidTokenError("unexpected azp")
        return claims
    except jwt.PyJWTError as exc:
        raise AppError(ErrorCode.UNAUTHENTICATED, "Please sign in again.") from exc


async def _load_user(subject: str, email: str | None) -> User:
    s = get_settings()
    async with transaction() as db:
        user = (await db.execute(select(User).where(User.auth_subject == subject))).scalar_one_or_none()
        if user is None:
            new_id = uuid.uuid4()
            await db.execute(insert(User).values(id=new_id, auth_subject=subject, email=email)
                             .on_conflict_do_nothing(index_elements=["auth_subject"]))  # fmt: skip
            user = (await db.execute(select(User).where(User.auth_subject == subject))).scalar_one()
            if s.signup_bonus_credits:
                await credits.grant(db, user.id, s.signup_bonus_credits, kind="grant",
                                    reason="Welcome credits", dedupe_key=f"signup:{user.id}")  # fmt: skip
        if user.deleted_at is not None:
            raise AppError(ErrorCode.UNAUTHENTICATED, "This account has been deleted.")
        now = datetime.now(UTC)
        if user.plan == "free" and (user.period_granted_until or now) <= now and user.created_at \
                and now - user.created_at > timedelta(days=28):  # fmt: skip
            month = now.strftime("%Y%m")
            await credits.grant(db, user.id, s.plan_monthly_credits.get("free", 0), kind="grant",
                                reason="Monthly free credits", dedupe_key=f"free:{user.id}:{month}")  # fmt: skip
            user.period_granted_until = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        if email and user.email != email:
            user.email = email
        return user


async def current_user(request: Request) -> AuthUser:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise AppError(ErrorCode.UNAUTHENTICATED, "Please sign in.")
    s = get_settings()
    claims = await asyncio.to_thread(_verify, header[7:].strip(), s)
    user = await _load_user(str(claims["sub"]), claims.get("email"))
    auth = AuthUser(user.id, user.auth_subject, user.plan, pseudonymize(user.id, s))
    request.state.user_ref = auth.ref
    bind_context(user=auth.ref)
    await rate_limit(f"user:{auth.id}", s.rate_limit_per_minute, 60)
    return auth


_WINDOW = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


async def rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """Distributed fixed-window limiter. Fails open if Redis is down (logged upstream)."""
    bucket = int(time.time() // window_seconds)
    try:
        count = await redis_client().eval(_WINDOW, 1, f"sp:rl:{key}:{bucket}", window_seconds + 5)  # type: ignore[misc]
    except Exception:
        return
    if int(count) > limit:
        raise AppError(ErrorCode.RATE_LIMITED, "Too many requests. Please slow down and try again.")


async def generation_limit(user: AuthUser = Depends(current_user)) -> AuthUser:
    await rate_limit(f"gen:{user.id}", get_settings().rate_limit_generation_per_hour, 3600)
    return user


def idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "").strip()
    if key and (len(key) > 100 or not all(c.isalnum() or c in "-_" for c in key)):
        raise AppError(ErrorCode.VALIDATION, "Invalid Idempotency-Key header.")
    return key or uuid.uuid4().hex


async def claim_idempotency(db: Any, user_id: uuid.UUID, endpoint: str, key: str,
                            body: bytes | str) -> dict[str, Any] | None:  # fmt: skip
    """Returns the stored response if this key was already used; otherwise claims it.
    The claim is in the caller's transaction, so it commits or rolls back with the work."""
    digest = hashlib.sha256(body if isinstance(body, bytes) else body.encode()).hexdigest()
    claimed = (await db.execute(
        insert(IdempotencyKey).values(user_id=user_id, endpoint=endpoint, key=key,
                                      request_sha256=digest)
        .on_conflict_do_nothing().returning(IdempotencyKey.key))).scalar_one_or_none()  # fmt: skip
    if claimed:
        return None
    row = (await db.execute(select(IdempotencyKey).where(
        IdempotencyKey.user_id == user_id, IdempotencyKey.endpoint == endpoint,
        IdempotencyKey.key == key))).scalar_one()  # fmt: skip
    if row.request_sha256 != digest:
        raise AppError(ErrorCode.CONFLICT, "This Idempotency-Key was used for a different request.")
    return row.response or {}


async def store_idempotent_response(db: Any, user_id: uuid.UUID, endpoint: str, key: str,
                                    response: dict[str, Any]) -> None:  # fmt: skip
    row = (await db.execute(select(IdempotencyKey).where(
        IdempotencyKey.user_id == user_id, IdempotencyKey.endpoint == endpoint,
        IdempotencyKey.key == key))).scalar_one()  # fmt: skip
    row.response = response
