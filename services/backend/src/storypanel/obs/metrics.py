"""Prometheus metrics. Labels are low-cardinality by construction (no ids)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LAT = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 90, 180, 360, 720)

http_requests = Counter("sp_http_requests_total", "HTTP requests", ["method", "route", "status"])
http_latency = Histogram("sp_http_request_seconds", "HTTP latency", ["method", "route"], buckets=_LAT)
errors = Counter("sp_errors_total", "Typed application errors", ["code", "surface"])

workflow_started = Counter("sp_workflows_started_total", "Workflows started", ["type"])
workflow_finished = Counter("sp_workflows_finished_total", "Workflows finished", ["type", "outcome"])
activity_runs = Counter("sp_activity_runs_total", "Activity executions", ["activity", "outcome"])
activity_latency = Histogram("sp_activity_seconds", "Activity latency", ["activity"], buckets=_LAT)
activity_retries = Counter("sp_activity_retries_total", "Activity attempts beyond the first", ["activity"])
schedule_to_start = Histogram(
    "sp_activity_schedule_to_start_seconds", "Queue age before an activity starts", ["activity"],
    buckets=_LAT,
)  # fmt: skip
outbox_pending = Gauge("sp_outbox_pending", "Undelivered outbox rows")
outbox_oldest_age = Gauge("sp_outbox_oldest_pending_seconds", "Age of the oldest pending outbox row")
stuck_projects = Gauge("sp_projects_stuck", "Active projects with no event for 15 minutes")

provider_calls = Counter("sp_provider_calls_total", "Provider calls", ["provider", "operation", "outcome"])
provider_latency = Histogram(
    "sp_provider_seconds", "Provider latency", ["provider", "operation"], buckets=_LAT
)
provider_tokens = Counter("sp_provider_tokens_total", "LLM tokens", ["provider", "direction"])
provider_images = Counter("sp_provider_images_total", "Images produced", ["provider", "operation"])
provider_cost = Counter("sp_provider_cost_usd_total", "Provider spend in USD", ["provider", "operation"])
spend_limit_hits = Counter("sp_spend_limit_hits_total", "Spend-limit rejections", ["scope"])

time_to_first_page = Histogram(
    "sp_time_to_first_page_seconds", "Script approval to first lettered page", buckets=_LAT
)
time_to_complete = Histogram("sp_time_to_complete_seconds", "Script approval to finished comic", buckets=_LAT)
qa_results = Counter("sp_panel_qa_total", "Panel QA outcomes", ["check", "outcome"])
identity_score = Histogram(
    "sp_identity_similarity", "Embedding similarity to references",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)  # fmt: skip
panel_outcomes = Counter("sp_panel_outcomes_total", "Final panel status", ["status"])
manual_regenerations = Counter("sp_manual_regenerations_total", "User-driven panel work", ["kind"])
moderation_results = Counter("sp_moderation_total", "Moderation outcomes", ["surface", "outcome"])
lettering_flags = Counter("sp_lettering_flags_total", "Lettering problems", ["flag"])
exports = Counter("sp_exports_total", "Exports", ["format", "outcome"])
credit_reconciliation_drift = Gauge(
    "sp_credit_reconciliation_drift", "Users whose ledger sum differs from their balance"
)
credit_events = Counter("sp_credit_events_total", "Ledger and reservation events", ["kind"])
webhook_events = Counter("sp_webhook_events_total", "Payment webhooks", ["type", "outcome"])
sse_connections = Gauge("sp_sse_connections", "Open event streams")
dependency_up = Gauge("sp_dependency_up", "Dependency health (1 = up)", ["dependency"])
telemetry_failures = Counter("sp_telemetry_failures_total", "Telemetry export failures", ["sink"])
