"""Create a demo account with credits (local/test only). Safe to run repeatedly.

cd services/backend && uv run python ../../scripts/seed-demo.py demo@example.com
"""

import asyncio
import sys
import uuid

from sqlalchemy import select

from storypanel.config import get_settings
from storypanel.db.models import User
from storypanel.db.session import dispose_engine, transaction
from storypanel.services import credits


async def main(email: str) -> None:
    if get_settings().is_deployed:
        raise SystemExit("refusing to seed a deployed environment")
    subject = "dev_" + uuid.uuid5(uuid.NAMESPACE_DNS, email.lower()).hex[:24]
    async with transaction() as db:
        user = (await db.execute(select(User).where(User.auth_subject == subject))).scalar_one_or_none()
        if user is None:
            user = User(id=uuid.uuid4(), auth_subject=subject, email=email.lower())
            db.add(user)
            await db.flush()
        granted = await credits.grant(
            db, user.id, 200, kind="grant", reason="Demo credits", dedupe_key=f"demo:{user.id}"
        )
    print(f"{email}: {'granted 200 demo credits' if granted else 'already seeded'}. Sign in with this email.")
    await dispose_engine()


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "demo@example.com"))
