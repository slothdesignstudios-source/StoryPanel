#!/usr/bin/env python3
"""Find comics whose background workflow was lost, and let them be resumed.

    services/backend/.venv/bin/python scripts/recover-lost-comics.py           # list only
    services/backend/.venv/bin/python scripts/recover-lost-comics.py --apply   # mark them

Before scripts/native-stack.sh kept Temporal's state in a file, restarting the local servers dropped
every running workflow: a comic waiting at the cast or script approval then ignored its approval.
This lists running comics whose workflow is gone (makes no paid call, changes nothing). With --apply
each one is marked "stopped" at the stage it was in and its credit hold is released; the Resume button
then restarts it from that stage (an approval gate is shown again; nothing already made is redrawn).
Needs the stack's Postgres and Temporal running (the same settings as the API).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend/src"))


async def main(apply: bool, min_age_minutes: int) -> int:
    from storypanel.services.maintenance import recover_lost_workflows
    from storypanel.workflows.client import get_temporal_client

    lost = await recover_lost_workflows(await get_temporal_client(), apply=apply,
                                        min_age=timedelta(minutes=min_age_minutes))  # fmt: skip
    print(json.dumps(lost, indent=1))
    print(f"{len(lost)} comic(s) with a lost workflow" + ("; marked stopped, press Resume on each" if apply and lost
                                                         else "; rerun with --apply to mark them" if lost else ""))  # fmt: skip
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-age-minutes", type=int, default=10, help="skip comics changed more recently than this")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.apply, a.min_age_minutes)))
