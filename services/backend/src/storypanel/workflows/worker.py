"""Worker process: Temporal worker + outbox dispatcher + maintenance loop + /metrics."""

from __future__ import annotations

import asyncio
import signal
from datetime import timedelta

from prometheus_client import start_http_server
from temporalio.worker import Worker

from storypanel import storage
from storypanel.config import get_settings
from storypanel.db.session import dispose_engine
from storypanel.obs import tracing
from storypanel.obs.logging import configure_logging, get_logger
from storypanel.providers.registry import get_providers
from storypanel.services import events, maintenance, outbox
from storypanel.workflows.activities import ACTIVITIES
from storypanel.workflows.client import get_temporal_client
from storypanel.workflows.definitions import WORKFLOWS

log = get_logger("worker")


def build_worker(client: object, task_queue: str) -> Worker:
    return Worker(
        client,  # type: ignore[arg-type]
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        max_concurrent_activities=40,
        max_concurrent_workflow_tasks=40,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )


async def main() -> None:
    s = get_settings()
    configure_logging("storypanel-worker")
    tracing.configure_tracing("storypanel-worker")
    get_providers()  # fail fast on provider misconfiguration
    if not s.is_deployed:
        await storage.ensure_bucket()
    start_http_server(s.metrics_port)
    client = await get_temporal_client()
    worker = build_worker(client, s.temporal_task_queue)
    stop, wake = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    background = [asyncio.create_task(outbox.run_dispatcher(client, stop, wake)),
                  asyncio.create_task(maintenance.run_loop(stop))]  # fmt: skip
    log.info("worker_started", task_queue=s.temporal_task_queue, provider_mode=s.provider_mode)
    async with worker:
        await stop.wait()
        log.info("worker_stopping")  # leaving the context drains in-flight activities
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    tracing.flush()
    await events.close_redis()
    await dispose_engine()
    log.info("worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
