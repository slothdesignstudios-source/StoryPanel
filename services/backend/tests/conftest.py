"""Test harness: real Postgres + Redis, in-process S3 (moto), Temporal dev server, fake providers.

Nothing here talks to a paid provider. Tests marked `workflow` need the Temporal test
server binary, which the SDK downloads on first use.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

os.environ.update({
    "SP_ENV": "test", "SP_PROVIDER_MODE": "fake", "SP_AUTH_MODE": "dev",
    "SP_DATABASE_URL": os.environ.get(
        "SP_TEST_DATABASE_URL", "postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel_test"),
    "SP_REDIS_URL": os.environ.get("SP_TEST_REDIS_URL", "redis://127.0.0.1:6379/9"),
    "SP_S3_ENDPOINT_URL": "http://127.0.0.1:5057", "SP_S3_ACCESS_KEY_ID": "test",
    "SP_S3_SECRET_ACCESS_KEY": "test", "SP_S3_BUCKET": "sp-test",
    "SP_TEMPORAL_TASK_QUEUE": "sp-test", "SP_LOG_LEVEL": "WARNING",
    "SP_STRIPE_WEBHOOK_SECRET": "whsec_test_secret", "SP_STRIPE_PRICE_CREATOR": "price_creator",
    "SP_STRIPE_PRICE_PRO": "price_pro", "SP_RATE_LIMIT_PER_MINUTE": "100000",
    "SP_RATE_LIMIT_GENERATION_PER_HOUR": "100000", "SP_SIGNUP_BONUS_CREDITS": "200",
    "SP_CORS_ORIGINS": '["http://localhost:3000"]', "AWS_EC2_METADATA_DISABLED": "true",
})  # fmt: skip
for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from sqlalchemy import text  # noqa: E402

from storypanel import storage  # noqa: E402
from storypanel.api.app import create_app  # noqa: E402
from storypanel.api.deps import issue_dev_token  # noqa: E402
from storypanel.config import get_settings  # noqa: E402
from storypanel.db.models import Base  # noqa: E402
from storypanel.db.session import dispose_engine, transaction  # noqa: E402
from storypanel.obs.logging import configure_logging  # noqa: E402
from storypanel.providers import fake  # noqa: E402
from storypanel.providers.registry import set_providers  # noqa: E402
from storypanel.services import events, outbox  # noqa: E402

SPANS = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def _s3() -> Iterator[None]:
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=5057, verbose=False)
    server.start()
    yield
    server.stop()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _bootstrap(_s3: None) -> AsyncIterator[None]:
    configure_logging("test")
    from storypanel.obs import tracing

    tracing.configure_tracing("test", exporter=SPANS)
    await storage.ensure_bucket()
    yield
    await events.close_redis()
    await dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with transaction() as db:
        await db.execute(text(f"truncate {tables} restart identity cascade"))
    await events.redis_client().flushdb()
    fake.control.reset()
    set_providers(None)
    yield


@pytest_asyncio.fixture
async def app() -> Any:
    application = create_app(start_dispatcher=False)
    application.state.shutting_down = False
    application.state.outbox_wake = asyncio.Event()
    application.state.temporal = None
    return application


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth(email: str = "ada@example.com") -> dict[str, str]:
    subject = "dev_" + uuid.uuid5(uuid.NAMESPACE_DNS, email).hex[:24]
    return {"Authorization": f"Bearer {issue_dev_token(subject, email, get_settings())}"}


@pytest.fixture
def headers() -> dict[str, str]:
    return auth()


# ------------------------------------------------------------------ temporal
@pytest_asyncio.fixture(scope="session")
async def temporal_env() -> AsyncIterator[Any]:
    from temporalio.contrib.opentelemetry import TracingInterceptor
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_local(interceptors=[TracingInterceptor()])
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def worker(temporal_env: Any, app: Any) -> AsyncIterator[Any]:
    """A real worker on a per-test task queue, plus the outbox dispatcher."""
    from storypanel.workflows.client import set_temporal_client
    from storypanel.workflows.worker import build_worker

    queue = f"sp-test-{uuid.uuid4().hex[:8]}"
    os.environ["SP_TEMPORAL_TASK_QUEUE"] = queue
    get_settings.cache_clear()
    set_temporal_client(temporal_env.client)
    app.state.temporal = temporal_env.client
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(outbox.run_dispatcher(temporal_env.client, stop, app.state.outbox_wake))
    w = build_worker(temporal_env.client, queue)
    async with w:
        yield w
        stop.set()
        app.state.outbox_wake.set()
        await dispatcher
    get_settings.cache_clear()


async def wait_for(
    client: httpx.AsyncClient, project_id: str, headers: dict[str, str], predicate: Any, timeout: float = 60.0
) -> dict[str, Any]:
    """Poll project state until `predicate(project)` holds."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/v1/projects/{project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if predicate(last):
            return last
        if last["status"] in ("failed", "blocked", "cancelled", "expired"):
            break  # terminal and not what the test wanted: fail fast
        await asyncio.sleep(0.15)
    raise AssertionError(f"timed out; status={last.get('status')} failure={last.get('failure')}")


PROMPT = "A shy robot who runs a night-time noodle stall learns to make friends. 2 pages, manga style."


async def run_to_completion(
    client: httpx.AsyncClient, headers: dict[str, str], prompt: str = PROMPT
) -> dict[str, Any]:
    """Drive the whole happy path through the public API."""
    resp = await client.post("/v1/projects", json={"prompt": prompt}, headers=headers)
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_cast_approval")
    r = await client.post(
        f"/v1/projects/{pid}/characters/approve", json={"cast_version": p["cast_version"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    p = await wait_for(client, pid, headers, lambda p: p["status"] == "awaiting_script_approval")
    r = await client.post(
        f"/v1/projects/{pid}/script/approve", json={"script_version": p["script_version"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    return await wait_for(
        client, pid, headers, lambda p: p["status"] in ("completed", "partially_completed", "failed")
    )
