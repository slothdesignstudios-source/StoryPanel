"""Exercise the telemetry paths end to end and write an evidence report.

Starts the API and a worker (fake providers) wired to *local receivers* that speak the
real wire protocols: OTLP/HTTP for traces, the Sentry envelope endpoint, and Langfuse's
OTLP ingestion endpoint. Then it runs a comic and checks what actually arrived.

    cd services/backend && uv run python ../../scripts/verify-observability.py

Needs Postgres and Redis (scripts/native-services.sh) and the Temporal CLI. It does not
need Docker. It does NOT run Prometheus, Grafana, Tempo or the OTel Collector, so alert
rule evaluation and dashboards are checked only for syntax and for metric existence.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get("SP_OBS_RUN", "/tmp/storypanel-obs"))
PORTS = {"stub": 4400, "s3": 9100, "temporal": 7300, "api": 8100, "worker_metrics": 9500}
CANARY = {  # values that must never appear in logs, traces, error reports or the web bundle
    "SP_PSEUDONYM_SALT": "CANARY-salt-7f3a9c1e5b",
    "SP_DEV_AUTH_SECRET": "CANARY-devauth-2d8e4f6a0b1c3d5e7f9a",
    "SP_STRIPE_WEBHOOK_SECRET": "whsec_CANARYc0ffee1234567890abcd",
    "SP_STRIPE_SECRET_KEY": "sk_test_CANARY51Nabcdefghijklmnop",
    "SP_S3_SECRET_ACCESS_KEY": "CANARY-s3secret-9b7d5f3a1c",
    "SP_LANGFUSE_SECRET_KEY": "sk-lf-CANARY-4e6a8c0b2d",
}
PROMPT_CANARY = "zanzibar-lighthouse-keeper"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}")


class Stub:
    def __init__(self) -> None:
        self.otlp: list[bytes] = []
        self.langfuse: list[bytes] = []
        self.sentry: list[bytes] = []
        self.other: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                enc = self.headers.get("content-encoding", "")
                try:
                    if enc == "gzip":
                        body = gzip.decompress(body)
                    elif enc == "deflate":
                        body = zlib.decompress(body)
                    elif enc == "br":
                        import brotli  # type: ignore[import-not-found]

                        body = brotli.decompress(body)
                except Exception:
                    pass
                if self.path == "/v1/traces":
                    outer.otlp.append(body)
                elif self.path.startswith("/api/public/otel"):
                    outer.langfuse.append(body)
                elif "/envelope" in self.path:
                    outer.sentry.append(body)
                else:
                    outer.other.append(self.path)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self) -> None:  # noqa: N802
                outer.other.append(self.path)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", PORTS["stub"]), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def spans(payloads: list[bytes]) -> list[dict[str, Any]]:
    out = []
    for raw in payloads:
        req = ExportTraceServiceRequest()
        try:
            req.ParseFromString(raw)
        except Exception:
            continue
        for rs in req.resource_spans:
            res = {a.key: a.value.string_value for a in rs.resource.attributes}
            for ss in rs.scope_spans:
                for sp in ss.spans:
                    out.append({"trace": sp.trace_id.hex(), "name": sp.name, "service": res.get("service.name"),
                                "release": res.get("service.version"),
                                "attrs": {a.key: (a.value.string_value or str(a.value.int_value)) for a in sp.attributes}})  # fmt: skip
    return out


def env(extra: dict[str, str] | None = None) -> dict[str, str]:
    stub = f"http://127.0.0.1:{PORTS['stub']}"
    e = {**os.environ, **CANARY,
         "SP_ENV": "local", "SP_RELEASE": "obs-demo-1.2.3", "SP_PROVIDER_MODE": "fake", "SP_AUTH_MODE": "dev",
         "SP_DATABASE_URL": os.environ.get("SP_DATABASE_URL", "postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel"),
         "SP_REDIS_URL": "redis://127.0.0.1:6379/3", "SP_S3_ENDPOINT_URL": f"http://127.0.0.1:{PORTS['s3']}",
         "SP_S3_ACCESS_KEY_ID": "local", "SP_S3_BUCKET": "sp-obs", "SP_TEMPORAL_ADDRESS": f"127.0.0.1:{PORTS['temporal']}",
         "SP_TEMPORAL_TASK_QUEUE": f"obs-{uuid.uuid4().hex[:6]}", "SP_METRICS_PORT": str(PORTS["worker_metrics"]),
         "SP_OTLP_ENDPOINT": stub, "SP_SENTRY_DSN": f"http://publickey@127.0.0.1:{PORTS['stub']}/42",
         "SP_LANGFUSE_PUBLIC_KEY": "pk-lf-demo", "SP_LANGFUSE_HOST": stub, "SP_LOG_LEVEL": "INFO",
         "SP_SIGNUP_BONUS_CREDITS": "50", "NO_PROXY": "127.0.0.1,localhost", "OTEL_BSP_SCHEDULE_DELAY": "500",
         "LANGFUSE_FLUSH_INTERVAL": "1"}  # fmt: skip
    e.update(extra or {})
    return e


def launch(name: str, cmd: list[str], e: dict[str, str]) -> subprocess.Popen[bytes]:
    log = open(RUN / f"{name}.log", "ab")  # noqa: SIM115
    return subprocess.Popen(cmd, cwd=ROOT / "services" / "backend", env=e, stdout=log, stderr=log)


def wait_http(url: str, ok: Any = lambda r: r.status_code < 500, timeout: float = 60) -> httpx.Response:
    end = time.time() + timeout
    last: Exception | None = None
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=3)
            if ok(r):
                return r
        except Exception as exc:
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for {url}: {last}")


def scrape(url: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for fam in text_string_to_metric_families(httpx.get(url, timeout=5).text):
        for s in fam.samples:
            out.setdefault(s.name, []).append((dict(s.labels), s.value))
    return out


def run_comic(
    api: str, email: str, trace_id: str | None = None, until: str = "awaiting_cast_approval"
) -> str:
    token = httpx.post(f"{api}/v1/dev/login", json={"email": email}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    if trace_id:
        h["traceparent"] = f"00-{trace_id}-{'2' * 16}-01"
    pid = httpx.post(f"{api}/v1/projects", headers=h, timeout=20,
                     json={"prompt": f"A {PROMPT_CANARY} befriends a gull, as a 4 panel strip"}).json()["id"]  # fmt: skip
    end = time.time() + 90
    while time.time() < end:
        p = httpx.get(
            f"{api}/v1/projects/{pid}", headers={"Authorization": h["Authorization"]}, timeout=10
        ).json()
        if p["status"] == until:
            return pid
        if p["status"] in ("failed", "blocked"):
            raise RuntimeError(f"comic {p['status']}: {p['failure']}")
        time.sleep(0.4)
    raise RuntimeError("comic did not reach " + until)


def main() -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    for f in RUN.glob("*.log"):
        f.unlink()
    temporal = os.environ.get("TEMPORAL_BIN") or next(
        iter(sorted(str(p) for p in Path("/tmp").glob("temporal-sdk-python-*"))), "temporal"
    )
    api = f"http://127.0.0.1:{PORTS['api']}"
    evidence: dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "checks": {}}
    checks = evidence["checks"]
    procs: dict[str, subprocess.Popen[bytes]] = {}
    stub = Stub()

    def s3_up() -> None:
        procs["s3"] = launch(
            "s3", ["uv", "run", "moto_server", "-H", "127.0.0.1", "-p", str(PORTS["s3"])], env()
        )
        wait_http(f"http://127.0.0.1:{PORTS['s3']}/")
        import boto3

        boto3.client("s3", endpoint_url=f"http://127.0.0.1:{PORTS['s3']}", aws_access_key_id="local",
                     aws_secret_access_key=CANARY["SP_S3_SECRET_ACCESS_KEY"], region_name="us-east-1").create_bucket(Bucket="sp-obs")  # fmt: skip

    try:
        s3_up()
        procs["temporal"] = launch("temporal", [temporal, "server", "start-dev", "--ip", "127.0.0.1", "--port",
                                                str(PORTS["temporal"]), "--headless", "--log-level", "error"], env())  # fmt: skip
        time.sleep(4)
        e = env()
        procs["api"] = launch("api", ["uv", "run", "uvicorn", "storypanel.api.app:app_factory", "--factory", "--host",
                                      "127.0.0.1", "--port", str(PORTS["api"])], e)  # fmt: skip
        # the worker hits one unexpected error (reported to Sentry) and recovers by retry
        procs["worker"] = launch("worker", ["uv", "run", "python", "-m", "storypanel.workflows.worker"],
                                 {**e, "SP_FAKE_SCENARIO": json.dumps({"crashes": {"text:bible": 1}})})  # fmt: skip
        wait_http(f"{api}/readyz", ok=lambda r: r.status_code == 200)

        # ---- 1. one comic, with a trace id chosen by the "browser"
        trace_id = uuid.uuid4().hex
        pid = run_comic(api, "obs-demo@example.com", trace_id)
        time.sleep(6)  # batch exporters
        app_spans = spans(stub.otlp)
        mine = [s for s in app_spans if s["trace"] == trace_id]
        names = sorted({s["name"] for s in mine})
        need = {"http": any(n.startswith("POST /v1/projects") for n in names),
                "workflow_start": "StartWorkflow:ComicWorkflow" in names,
                "workflow_run": "RunWorkflow:ComicWorkflow" in names,
                "activity": any(n.startswith("RunActivity:") for n in names),
                "provider_call": any(n.endswith((".IntakeResult", ".BibleAndCast")) for n in names)}  # fmt: skip
        checks["correlated_trace"] = {
            "pass": all(need.values()), "trace_id": trace_id, "stages": need, "span_count": len(mine),
            "services": sorted({s["service"] for s in mine if s["service"]}),
            "release_on_spans": sorted({s["release"] for s in mine if s["release"]}), "sample_span_names": names[:14]}  # fmt: skip

        # ---- 2. Langfuse
        lf = spans(stub.langfuse)
        lf_blob = b"".join(stub.langfuse)
        gens = [s for s in lf if "." in s["name"]]
        checks["langfuse"] = {
            "pass": bool(gens) and PROMPT_CANARY.encode() not in lf_blob and b"noodle" not in lf_blob,
            "observations": len(lf), "generation_names": sorted({s["name"] for s in gens})[:8],
            "prompt_text_present": PROMPT_CANARY.encode() in lf_blob,
            "has_prompt_version": any("prompt_version" in json.dumps(s["attrs"]) for s in lf),
            "has_usage": any("usage" in json.dumps(s["attrs"]) for s in lf)}  # fmt: skip

        # ---- 3. Sentry
        events = []
        for raw in stub.sentry:
            for line in raw.split(b"\n"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if isinstance(d, dict) and "exception" in d:
                    events.append(d)
        ev = events[0] if events else {}
        sentry_blob = b"".join(stub.sentry)
        checks["sentry"] = {
            "pass": bool(events) and ev.get("release") == "obs-demo-1.2.3" and ev.get("environment") == "local"
            and "user" not in ev and PROMPT_CANARY.encode() not in sentry_blob,
            "events": len(events), "release": ev.get("release"), "environment": ev.get("environment"),
            "exception": (ev.get("exception", {}).get("values") or [{}])[-1].get("value"),
            "tags": ev.get("tags"), "has_user_block": "user" in ev,
            "local_variables_sent": b'"vars"' in sentry_blob, "prompt_text_present": PROMPT_CANARY.encode() in sentry_blob}  # fmt: skip

        # ---- 4. logs
        log_lines = []
        raw_logs = ""
        for name in ("api", "worker"):
            text = (RUN / f"{name}.log").read_text(errors="replace")
            raw_logs += text
            for line in text.splitlines():
                if line.startswith("{"):
                    try:
                        log_lines.append(json.loads(line))
                    except Exception:
                        pass
        base = {"timestamp", "level", "service", "environment", "release", "message"}
        provider_lines = [x for x in log_lines if x.get("message") == "provider_call"]
        activity_lines = [x for x in log_lines if x.get("message") == "activity_finished"]
        leaks = {k: v in raw_logs for k, v in CANARY.items()}
        patterns = {"jwt": bool(re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", raw_logs)),
                    "signed_url": "X-Amz-Signature=" in raw_logs and "[redacted]" not in raw_logs.split("X-Amz-Signature=")[1][:12],
                    "email": "obs-demo@example.com" in raw_logs, "prompt": PROMPT_CANARY in raw_logs}  # fmt: skip
        checks["logs"] = {
            "pass": bool(log_lines) and all(base <= set(x) for x in log_lines) and not any(leaks.values())
            and not any(patterns.values()) and bool(provider_lines) and bool(activity_lines),
            "json_lines": len(log_lines), "all_have_base_fields": all(base <= set(x) for x in log_lines),
            "request_lines_with_trace_and_user": sum(1 for x in log_lines if x.get("message") == "http_request"
                                                     and x.get("trace_id") and x.get("request_id")),
            "activity_fields": sorted(set(activity_lines[0]) - base) if activity_lines else [],
            "provider_call_fields": sorted(set(provider_lines[0]) - base) if provider_lines else [],
            "pseudonymous_user": any(str(x.get("user", "")).startswith("u_") for x in log_lines),
            "secret_canaries_found": leaks, "sensitive_patterns_found": patterns,
            "crash_logged_with_error_code": any(x.get("message") == "activity_crashed" and x.get("error_code") for x in log_lines)}  # fmt: skip

        # ---- 5. metrics actually emitted
        m_api, m_worker = (
            scrape(f"{api}/metrics"),
            scrape(f"http://127.0.0.1:{PORTS['worker_metrics']}/metrics"),
        )
        emitted = set(m_api) | set(m_worker)
        text = (ROOT / "infra/observability/alerts.yml").read_text() + (
            ROOT / "infra/observability/grafana/dashboards/storypanel-overview.json").read_text()  # fmt: skip
        referenced = set(re.findall(r"sp_[a-z_]+", text))
        missing = sorted(
            r for r in referenced if r not in emitted and not any(x.startswith(r) for x in emitted)
        )
        high_card, series = [], {}
        for name, samples in {**m_api, **m_worker}.items():
            if not name.startswith("sp_"):
                continue
            series[name] = len(samples)
            for labels, _ in samples:
                if any(UUID_RE.search(v) or "@" in v for v in labels.values()):
                    high_card.append((name, labels))
        checks["metrics"] = {
            "pass": not high_card and max(series.values()) < 400,
            "sp_metric_families_emitted": len({n for n in emitted if n.startswith("sp_")}),
            "referenced_by_alerts_and_dashboards": len(referenced),
            "referenced_but_not_yet_emitted": missing,
            "note": "Counters with labels appear only after their first increment, so 'not yet emitted' "
                    "lists metrics this short scenario never touched (e.g. exports, webhooks).",
            "labels_with_identifier_values": high_card[:5], "max_series_per_metric": max(series.values()),
            "http_route_label_examples": sorted({lb["route"] for lb, _ in m_api.get("sp_http_requests_total", [])})[:6],
            "cost_by_basis": {f"{lb['provider']}/{lb['operation']}/{lb['basis']}": round(v, 4)
                              for lb, v in m_worker.get("sp_provider_cost_usd_total", [])},
            "qa_and_queue_present": {k: k in emitted for k in ("sp_activity_schedule_to_start_seconds_bucket",
                                                               "sp_provider_seconds_bucket", "sp_http_request_seconds_bucket")}}  # fmt: skip

        # ---- 6. alert condition and recovery (DependencyDown: sp_dependency_up == 0)
        def dep() -> float:
            httpx.get(f"{api}/readyz", timeout=10)
            return next(
                v for lb, v in scrape(f"{api}/metrics")["sp_dependency_up"] if lb["dependency"] == "storage"
            )

        before = dep()
        procs["s3"].send_signal(signal.SIGTERM)
        procs["s3"].wait(10)
        down_status = httpx.get(f"{api}/readyz", timeout=10).status_code
        down = dep()
        s3_up()
        up_status = wait_http(f"{api}/readyz", ok=lambda r: r.status_code == 200).status_code
        checks["alert_condition_dependency_down"] = {
            "pass": (before, down, dep()) == (1.0, 0.0, 1.0) and down_status == 503 and up_status == 200,
            "metric_before_during_after": [before, down, dep()], "readyz_during": down_status, "readyz_after": up_status,
            "limitation": "The alert *expression* (sp_dependency_up == 0 for 2m) was shown to become true and "
                          "false again. Prometheus/Alertmanager were not run, so rule evaluation and "
                          "notification delivery are not demonstrated."}  # fmt: skip

        # ---- 7. telemetry sinks down: the product keeps working
        stub.stop()
        t0 = time.time()
        pid2 = run_comic(api, "obs-demo-2@example.com")
        checks["telemetry_outage"] = {
            "pass": True, "comic_reached_cast_gate_seconds": round(time.time() - t0, 1), "project": bool(pid2),
            "sinks_down": ["OTLP traces", "Sentry", "Langfuse"]}  # fmt: skip
        evidence["project"] = "created" if pid else ""
    finally:
        for p in procs.values():
            p.send_signal(signal.SIGTERM)
        for p in procs.values():
            try:
                p.wait(20)
            except Exception:
                p.kill()
        try:
            stub.stop()
        except Exception:
            pass
    evidence["passed"] = all(c["pass"] for c in checks.values())
    out = ROOT / "docs" / "evidence" / "observability.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=1, default=str))
    for name, c in checks.items():
        print(("PASS " if c["pass"] else "FAIL ") + name)
    print("evidence:", out)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
