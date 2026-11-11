"""Release-focused verification with fake providers: hard worker kill mid-generation,
a load run, and a backup/restore drill. Writes docs/evidence/release-verification.json.

    cd services/backend && uv run python ../../scripts/verify-release.py

Workload (documented so the numbers mean something):
- 1 API process (uvicorn, 1 worker), 1 Temporal worker process, Temporal dev server,
  PostgreSQL 16 and Redis on the same machine, S3 emulated by moto.
- LOAD_USERS concurrent users; each creates a 4-panel strip, approves cast and script,
  waits for completion, then issues 10 project reads. Fake providers return in
  milliseconds, so this measures the application plane (API, database, outbox, workflow
  engine, lettering, rendering), NOT provider throughput or real generation latency.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get("SP_REL_RUN", "/tmp/storypanel-release"))
P = {"s3": 9200, "temporal": 7400, "api": 8200, "metrics": 9600}
DB = "storypanel_release"
PG = ["-h", "127.0.0.1", "-U", "storypanel"]
LOAD_USERS = int(os.environ.get("LOAD_USERS", "20"))
API = f"http://127.0.0.1:{P['api']}"


def env(db: str = DB, **extra: str) -> dict[str, str]:
    return {**os.environ, "SP_ENV": "local", "SP_PROVIDER_MODE": "fake", "SP_AUTH_MODE": "dev",
            "SP_DATABASE_URL": f"postgresql+asyncpg://storypanel@127.0.0.1:5432/{db}",
            "SP_REDIS_URL": "redis://127.0.0.1:6379/4", "SP_S3_ENDPOINT_URL": f"http://127.0.0.1:{P['s3']}",
            "SP_S3_ACCESS_KEY_ID": "local", "SP_S3_SECRET_ACCESS_KEY": "local", "SP_S3_BUCKET": "sp-release",
            "SP_TEMPORAL_ADDRESS": f"127.0.0.1:{P['temporal']}", "SP_TEMPORAL_TASK_QUEUE": "release",
            "SP_METRICS_PORT": str(P["metrics"]), "SP_LOG_LEVEL": "WARNING", "SP_SIGNUP_BONUS_CREDITS": "50",
            "SP_RATE_LIMIT_PER_MINUTE": "100000", "SP_RATE_LIMIT_GENERATION_PER_HOUR": "100000",
            "SP_SPEND_LIMIT_GLOBAL_DAILY_USD": "100000", "NO_PROXY": "127.0.0.1,localhost", **extra}  # fmt: skip


def launch(name: str, cmd: list[str], e: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(cmd, cwd=ROOT / "services/backend", env=e, stdout=open(RUN / f"{name}.log", "ab"),  # noqa: SIM115
                            stderr=subprocess.STDOUT)  # fmt: skip


def sh(*cmd: str, **kw: Any) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def psql(db: str, sql: str) -> str:
    return sh("psql", *PG, "-d", db, "-Atc", sql).strip()


def wait(url: str, timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"timeout: {url}")


def worker(**extra: str) -> subprocess.Popen[bytes]:
    # Run the interpreter directly (not through `uv run`) so signals reach the worker itself.
    return launch("worker", [str(ROOT / "services/backend/.venv/bin/python"), "-m", "storypanel.workflows.worker"],
                  env(**extra))  # fmt: skip


class User:
    def __init__(self, http: httpx.AsyncClient, email: str) -> None:
        self.http, self.email, self.h, self.lat = http, email, {}, []

    async def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        t = time.perf_counter()
        r = await self.http.request(method, API + path, headers=self.h, **kw)
        self.lat.append((time.perf_counter() - t, r.status_code))
        return r

    async def login(self) -> None:
        r = await self.http.post(API + "/v1/dev/login", json={"email": self.email})
        self.h = {"Authorization": "Bearer " + r.json()["token"]}

    async def until(self, pid: str, ok: Any, timeout: float = 240) -> dict[str, Any]:
        end = time.time() + timeout
        while time.time() < end:
            p = (await self.call("GET", f"/v1/projects/{pid}")).json()
            if ok(p):
                return p
            if p.get("status") in ("failed", "blocked", "cancelled"):
                raise RuntimeError(f"{p['status']}: {p.get('failure')}")
            await asyncio.sleep(0.5)
        raise RuntimeError("timeout")

    async def comic(self, to_gate_only: bool = False) -> dict[str, Any]:
        await self.login()
        t0 = time.time()
        pid = (await self.call("POST", "/v1/projects", json={"prompt": "Two pigeons argue over a chip, as a 4 panel strip"})).json()["id"]
        p = await self.until(pid, lambda p: p["status"] == "awaiting_cast_approval")
        if to_gate_only:
            return p
        await self.call("POST", f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]})
        p = await self.until(pid, lambda p: p["status"] == "awaiting_script_approval")
        t_approve = time.time()
        await self.call("POST", f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]})
        p = await self.until(pid, lambda p: p["status"] in ("completed", "partially_completed"))
        for _ in range(10):
            await self.call("GET", f"/v1/projects/{pid}")
        return {"id": pid, "status": p["status"], "total_s": time.time() - t0, "draw_s": time.time() - t_approve}


async def kill_test(procs: dict[str, subprocess.Popen[bytes]]) -> dict[str, Any]:
    """SIGKILL the only worker while an image request is in flight at the provider."""
    procs["worker"] = worker(SP_FAKE_SCENARIO=json.dumps({"hang_fetches": 1}))
    wait(f"http://127.0.0.1:{P['metrics']}/metrics")
    async with httpx.AsyncClient(timeout=30) as http:
        u = User(http, f"kill-{uuid.uuid4().hex[:6]}@example.com")
        task = asyncio.create_task(u.comic(to_gate_only=True))
        end = time.time() + 60
        while time.time() < end and psql(DB, "select count(*) from provider_usage where status='started' and provider_request_id is not null") == "0":
            await asyncio.sleep(0.3)
        in_flight = psql(DB, "select idempotency_key from provider_usage where status='started' limit 1")
        t_kill = time.time()
        procs["worker"].send_signal(signal.SIGKILL)  # no graceful shutdown, no cleanup
        procs["worker"].wait()
        await asyncio.sleep(2)
        still = psql(DB, f"select status from provider_usage where idempotency_key='{in_flight}'")
        procs["worker"] = worker()
        p = await task  # heartbeat timeout (60 s) hands the activity to the new worker
        recovered = time.time() - t_kill
        row = psql(DB, f"select status, submissions, cost_basis from provider_usage where idempotency_key='{in_flight}'")
        dup = psql(DB, "select count(*) from (select idempotency_key from provider_usage group by 1 having count(*) > 1) x")
        refs = psql(DB, "select count(*), count(distinct (character_id, view)) from character_refs")
        chars = psql(DB, f"select count(*) from characters where project_id='{p['id']}' and sheet_status='ready'")
    return {"pass": p["status"] == "awaiting_cast_approval" and still == "started" and row.startswith("succeeded|2")
            and dup == "0" and refs.split("|")[0] == refs.split("|")[1],
            "killed_while_request_in_flight": in_flight, "row_after_kill_before_restart": still,
            "row_after_recovery(status|submissions|basis)": row, "seconds_from_kill_to_cast_gate": round(recovered, 1),
            "characters_ready": chars, "duplicate_usage_rows": dup, "character_refs(total|distinct)": refs,
            "explanation": "The in-flight request was resumed by id on the new worker. The fake provider keeps "
                           "requests in process memory, so the id was unknown after the kill (as with a provider "
                           "that lost the request) and exactly one new submission was made. With a real provider "
                           "the same code path fetches the finished result instead of resubmitting."}  # fmt: skip


async def load_test() -> dict[str, Any]:
    t0 = time.time()
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=200)) as http:
        users = [User(http, f"load-{n}-{uuid.uuid4().hex[:5]}@example.com") for n in range(LOAD_USERS)]
        results = await asyncio.gather(*[u.comic() for u in users], return_exceptions=True)
    wall = time.time() - t0
    ok = [r for r in results if isinstance(r, dict)]
    lat = sorted(t for u in users for t, _ in u.lat)
    codes: dict[int, int] = {}
    for u in users:
        for _, c in u.lat:
            codes[c] = codes.get(c, 0) + 1
    q = lambda xs, f: round(xs[min(len(xs) - 1, int(f * len(xs)))], 3)  # noqa: E731
    # balance must equal the ledger sum, and held credits must equal open reservations
    drift = psql(DB, """select count(*) from users u
        left join (select user_id, sum(delta) t from credit_ledger group by 1) l on l.user_id = u.id
        left join (select user_id, sum(amount - settled) h from credit_reservations where status = 'open' group by 1) r
               on r.user_id = u.id
        where u.credit_balance <> coalesce(l.t, 0) or u.credit_reserved <> coalesce(r.h, 0)""")
    load_holds = psql(DB, "select coalesce(sum(credit_reserved), 0) from users where email like 'load-%'")
    open_rows = psql(DB, "select count(*) from provider_usage where status in ('reserved','submitting','started')")
    return {"pass": len(ok) == LOAD_USERS and all(r["status"] == "completed" for r in ok) and drift == "0"
            and load_holds == "0" and not any(c >= 500 for c in codes) and open_rows == "0",
            "users": LOAD_USERS, "comics_completed": len(ok), "errors": [str(r)[:120] for r in results if not isinstance(r, dict)][:3],
            "wall_seconds": round(wall, 1), "api_requests": len(lat), "status_codes": codes,
            "api_latency_s": {"p50": q(lat, 0.5), "p95": q(lat, 0.95), "p99": q(lat, 0.99), "max": round(lat[-1], 3)},
            "prompt_to_finished_s": {"p50": round(statistics.median(r["total_s"] for r in ok), 1),
                                     "max": round(max(r["total_s"] for r in ok), 1)},
            "script_approval_to_finished_s": {"p50": round(statistics.median(r["draw_s"] for r in ok), 1),
                                              "max": round(max(r["draw_s"] for r in ok), 1)},
            "panels_drawn": int(psql(DB, "select count(*) from panels where status='ok'")),
            "provider_calls": int(psql(DB, "select count(*) from provider_usage")),
            "accounts_with_ledger_or_hold_drift": drift, "credits_still_held_by_finished_load_users": load_holds, "provider_rows_left_open": open_rows}  # fmt: skip


def restore_drill(procs: dict[str, subprocess.Popen[bytes]]) -> dict[str, Any]:
    """pg_dump the loaded database, restore into a fresh one, and compare."""
    tables = psql(DB, "select tablename from pg_tables where schemaname='public' order by 1").split("\n")
    counts = lambda db: {t: int(psql(db, f'select count(*) from "{t}"')) for t in tables}  # noqa: E731
    digest = lambda db: psql(db, "select md5(string_agg(id::text || delta::text || dedupe_key || balance_after::text, ',' order by id)) from credit_ledger")  # noqa: E731
    for name in ("api", "worker"):  # stop writers first, as the runbook says
        procs[name].send_signal(signal.SIGTERM)
        procs[name].wait(40)
    before, ledger = counts(DB), digest(DB)
    dump = RUN / "backup.dump"
    t0 = time.time()
    sh("pg_dump", *PG, "--format=custom", "--no-owner", "-f", str(dump), DB)
    t_dump = time.time() - t0
    target = DB + "_restored"
    sh("psql", *PG, "-d", "postgres", "-c", f"drop database if exists {target}")
    sh("psql", *PG, "-d", "postgres", "-c", f"create database {target}")
    t0 = time.time()
    sh("pg_restore", *PG, "--no-owner", "-d", target, str(dump))
    t_restore = time.time() - t0
    after = counts(target)
    head = psql(target, "select version_num from alembic_version")
    check = subprocess.run(["uv", "run", "alembic", "check"], cwd=ROOT / "services/backend", env=env(target),
                           capture_output=True, text=True)  # fmt: skip
    drift = psql(target, """select count(*) from users u left join (select user_id, sum(delta) t from credit_ledger group by 1) l
                            on l.user_id=u.id where u.credit_balance <> coalesce(l.t,0)""")
    trigger = subprocess.run(["psql", *PG, "-d", target, "-c", "update credit_ledger set delta = 0"], capture_output=True, text=True)
    orphans = psql(target, "select count(*) from panel_versions v left join assets a on a.id=v.image_asset_id where a.id is null")
    # serve from the restored database: a finished comic loads with its images
    procs["api"] = launch("api", ["uv", "run", "uvicorn", "storypanel.api.app:app_factory", "--factory", "--host",
                                  "127.0.0.1", "--port", str(P["api"])], env(target))  # fmt: skip
    wait(f"{API}/readyz")
    email = psql(target, "select email from users where email like 'load-%' limit 1")
    tok = httpx.post(API + "/v1/dev/login", json={"email": email}).json()["token"]
    projects = httpx.get(API + "/v1/projects", headers={"Authorization": f"Bearer {tok}"}).json()["projects"]
    full = httpx.get(f"{API}/v1/projects/{projects[0]['id']}", headers={"Authorization": f"Bearer {tok}"}).json()
    img = httpx.get(full["pages"][0]["panels"][0]["image_url"]).status_code
    return {"pass": before == after and ledger == digest(target) and drift == "0" and check.returncode == 0
            and "append-only" in trigger.stderr and orphans == "0" and img == 200 and full["status"] == "completed",
            "tables": len(tables), "rows_before": sum(before.values()), "rows_after": sum(after.values()),
            "tables_with_different_counts": [t for t in tables if before[t] != after[t]],
            "ledger_digest_identical": ledger == digest(target), "ledger_drift_accounts": drift,
            "migration_head": head, "schema_matches_models": check.returncode == 0,
            "append_only_trigger_restored": "append-only" in trigger.stderr, "orphaned_panel_versions": orphans,
            "restored_api_serves_comic": full["status"], "image_fetch_status": img,
            "dump_seconds": round(t_dump, 2), "restore_seconds": round(t_restore, 2),
            "dump_bytes": dump.stat().st_size,
            "limitation": "Logical dump/restore on one machine with a small dataset. Managed point-in-time recovery, "
                          "object-storage version restore and a restore under production data volume were not exercised."}  # fmt: skip


async def main() -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    for f in RUN.glob("*.log"):
        f.unlink()
    sh("psql", *PG, "-d", "postgres", "-c", f"drop database if exists {DB}")
    sh("psql", *PG, "-d", "postgres", "-c", f"create database {DB}")
    sh("psql", *PG, "-d", DB, "-c", "create extension vector")
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT / "services/backend", env=env(), check=True,
                   capture_output=True)  # fmt: skip
    sh("redis-cli", "-n", "4", "flushdb")
    temporal = os.environ.get("TEMPORAL_BIN") or next(iter(sorted(str(p) for p in Path("/tmp").glob("temporal-sdk-python-*"))), "temporal")
    procs: dict[str, subprocess.Popen[bytes]] = {}
    evidence: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "providers": "fake",
                                "machine": {"cpus": os.cpu_count()}, "checks": {}}  # fmt: skip
    try:
        procs["s3"] = launch("s3", ["uv", "run", "moto_server", "-H", "127.0.0.1", "-p", str(P["s3"])], env())
        procs["temporal"] = launch("temporal", [temporal, "server", "start-dev", "--ip", "127.0.0.1", "--port",
                                                str(P["temporal"]), "--headless", "--log-level", "error"], env())  # fmt: skip
        time.sleep(5)
        procs["api"] = launch("api", ["uv", "run", "uvicorn", "storypanel.api.app:app_factory", "--factory", "--host",
                                      "127.0.0.1", "--port", str(P["api"])], env())  # fmt: skip
        wait(f"{API}/readyz")
        evidence["checks"]["worker_sigkill_mid_generation"] = await kill_test(procs)
        evidence["checks"]["load"] = await load_test()
        evidence["checks"]["backup_restore"] = restore_drill(procs)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(20)
            except Exception:
                p.kill()
    evidence["passed"] = all(c["pass"] for c in evidence["checks"].values())
    out = ROOT / "docs/evidence/release-verification.json"
    out.write_text(json.dumps(evidence, indent=1, default=str))
    for name, c in evidence["checks"].items():
        print(("PASS " if c["pass"] else "FAIL ") + name)
    print("evidence:", out)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
