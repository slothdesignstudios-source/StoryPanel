#!/usr/bin/env python3
"""Find comics whose background workflow was lost, and recover them only as far as you approve.

    services/backend/.venv/bin/python scripts/recover-lost-comics.py                       # 1. dry run
    services/backend/.venv/bin/python scripts/recover-lost-comics.py --apply               # 2. stop them
    services/backend/.venv/bin/python scripts/recover-lost-comics.py --approve-paid-resume <comic-id>  # 3.

1. Dry run (default): lists running comics whose workflow is gone. Changes nothing, makes no call
   that costs money. For each comic it shows, separately:
   - reusable results already saved (Resume reuses them; nothing is paid again);
   - provider requests whose result can still be retrieved by request id;
   - ambiguous requests that may already have been billed (timed out, cut off, never answered),
     up to the amount shown; settle them later with `live-smoke.py reconcile --charges`;
   - images saved but never finished (not reused automatically);
   - what Resume would make again with NEW paid requests, and the possible additional cost.
2. --apply: marks every listed comic as stopped and releases its credit holds. Nothing is drawn,
   sent or charged. A comic waiting at an approval becomes resumable (Resume redoes nothing). A
   comic whose Resume would make paid requests is NOT made resumable.
3. --approve-paid-resume ID (repeatable): your explicit approval for that one comic; it then shows
   a Resume button. Pressing it starts the paid work listed under "would redo".

Needs the stack's Postgres and Temporal running (same settings as the API).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/backend/src"))


def _show(item: dict[str, Any]) -> None:
    print(f"\ncomic {item['project_id']}  ({item['status']}, {item['reason']}; stopped at: {item['resume_from']})")
    print(f"  credits held now: {item['credits_held']} (released by --apply)")
    print("  reusable, already saved:      " + ("; ".join(item["reusable"]) or "nothing"))
    print("  retrievable provider requests: " + (", ".join(f"{r['stage']} ({r['provider']} {r['request_id']})"
                                                         for r in item["retrievable_requests"]) or "none"))
    amb = item["ambiguous_requests"]
    print(f"  ambiguous, may already be billed: {len(amb)}" + "".join(
        f"\n    - {a['stage']} {a['model']} ({a['status']}): up to USD {a['may_already_be_billed_up_to_usd']}"
        for a in amb))
    print("  images saved but unfinished (not reused): " + (", ".join(
        f"panel {u['panel_id'][:8]} (checks {u['checks']})" for u in item["unfinished_images"]) or "none"))
    redo = item["needs_redo"]
    print("  Resume would redo with NEW paid requests: " + ("nothing" if not redo else "; ".join(
        f"{r['what']} (up to USD {r['possible_additional_cost_usd']})" if r["possible_additional_cost_usd"]
        else f"{r['what']} (cost not estimable from this comic)" for r in redo)))
    if item["possible_additional_cost_usd"]:
        print(f"    possible additional cost: USD {item['possible_additional_cost_usd']} "
              f"({item['possible_additional_cost_note'] or 'at the last reservations seen'})")
    print("  needs your approval to resume: " + ("YES (--approve-paid-resume)" if item["resume_needs_approval"]
                                                 else "no (nothing paid is redone)"))


async def main(apply: bool, approve: list[str], min_age_minutes: int, as_json: bool) -> int:
    from storypanel.services.maintenance import approve_paid_resume, recover_lost_workflows
    from storypanel.workflows.client import get_temporal_client

    if approve:
        out = await approve_paid_resume([uuid.UUID(x) for x in approve])
        print(json.dumps(out, indent=1))
        return 0 if all(o["approved"] for o in out) else 1
    lost = await recover_lost_workflows(await get_temporal_client(), apply=apply,
                                        min_age=timedelta(minutes=min_age_minutes))  # fmt: skip
    if as_json:
        print(json.dumps(lost, indent=1))
    else:
        for item in lost:
            _show(item)
    paid = [i for i in lost if i["resume_needs_approval"]]
    print(f"\n{len(lost)} comic(s) with a lost workflow; {len(paid)} would need paid work to resume.")
    if lost and not apply:
        print("Dry run: nothing changed. --apply stops them and releases their holds (no paid call).")
    elif apply and paid:
        print("Stopped. Comics needing paid work are NOT resumable until you run --approve-paid-resume <id>.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve-paid-resume", action="append", default=[], metavar="COMIC_ID")
    ap.add_argument("--min-age-minutes", type=int, default=10, help="skip comics changed more recently than this")
    ap.add_argument("--json", action="store_true", help="full machine-readable listing")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.apply, a.approve_paid_resume, a.min_age_minutes, a.json)))
