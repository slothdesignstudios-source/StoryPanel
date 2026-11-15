#!/usr/bin/env python3
"""Read-only: do cached balances and holds agree with the append-only ledger and open reservations?

    services/backend/.venv/bin/python scripts/check-credits.py

Prints every mismatch (user id, field, cached value, value from the ledger) or "consistent".
Changes nothing. Use it before and after an upgrade; a mismatch is a finding, not something to fix
by hand. (One comic's full credit trail: scripts/sql/reconcile-comic.sql.)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend/src"))


async def main() -> int:
    from sqlalchemy import text

    from storypanel.db.session import transaction
    from storypanel.services import credits

    async with transaction() as db:
        await db.execute(text("set transaction read only"))
        problems = await credits.reconcile(db)
    print(json.dumps(problems, indent=1, default=str) if problems else "consistent: balances and holds match the ledger")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
