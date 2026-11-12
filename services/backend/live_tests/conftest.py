"""Live integration tests against real third-party services. Never part of the default run
(`testpaths = ["tests"]`); each module skips unless its own switch and credentials are set.

    SP_LIVE=storage uv run pytest live_tests/test_storage_live.py -v
    SP_LIVE=stripe  uv run pytest live_tests/test_stripe_live.py -v

They use the normal SP_* configuration of the target environment (from the shell or .env), never
the simulated test settings, and they write evidence to docs/evidence/live/<service>-<date>.json.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

EVIDENCE = Path(__file__).resolve().parents[3] / "docs/evidence/live"


def require(service: str, *names: str) -> None:
    if service not in os.environ.get("SP_LIVE", "").split(","):
        pytest.skip(f"set SP_LIVE={service} to run live {service} tests")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"missing configuration: {', '.join(missing)}")


def record(service: str, name: str, data: dict[str, Any]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"{service}-{datetime.now(UTC).date().isoformat()}.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing[name] = {**data, "at": datetime.now(UTC).isoformat()}
    path.write_text(json.dumps(existing, indent=1, default=str))
