from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor

from storypanel.config import get_settings

_client: Client | None = None


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = await Client.connect(
            s.temporal_address,
            namespace=s.temporal_namespace,
            api_key=s.temporal_api_key.get_secret_value() if s.temporal_api_key else None,
            tls=s.temporal_tls,
            interceptors=[TracingInterceptor()],  # carries trace context into workflows/activities
        )
    return _client


def set_temporal_client(client: Client | None) -> None:
    global _client
    _client = client
