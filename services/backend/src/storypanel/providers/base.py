"""Provider-neutral contracts. Pipeline code depends only on these protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
TextRole = Literal["story", "light", "vision"]


@dataclass(frozen=True)
class CallContext:
    """Who and what a provider call is for: drives logs, cost attribution and limits."""

    stage: str
    user_id: str | None = None
    project_id: str | None = None
    target_id: str | None = None
    attempt: int = 0
    prompt_version: str | None = None
    # Structured inputs the prompt was rendered from. Recorded as trace metadata (ids and
    # enums only) and used by the offline fake to produce coherent output.
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    provider: str
    model: str
    operation: str
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    request_id: str | None = None


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str = "image/png"
    label: str | None = None


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    aspect: str  # e.g. "4:3"; mapped by the adapter to what the model supports
    references: tuple[ImageInput, ...] = ()
    mode: Literal["generate", "edit"] = "generate"
    seed: int | None = None
    model: str | None = None  # override (fallback model)
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    provider: str
    model: str
    request_id: str | None
    seed: int | None = None


class RequestPending(Exception):
    """The provider still holds the request; it has neither succeeded nor failed."""


class TextProvider(Protocol):
    name: str

    async def structured(
        self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
        images: tuple[ImageInput, ...] = (),
    ) -> tuple[T, Usage]: ...  # fmt: skip


class ImageProvider(Protocol):
    """Two-step so an ambiguous timeout can be reconciled by request id, not re-paid."""

    name: str

    def model_for(self, req: ImageRequest) -> str: ...

    async def submit(self, req: ImageRequest, ctx: CallContext) -> str: ...

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult: ...


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed_images(self, images: list[bytes], ctx: CallContext) -> list[list[float]]: ...


class UpscaleProvider(Protocol):
    name: str
    model: str

    async def submit(self, image: bytes, factor: int, ctx: CallContext) -> str: ...

    async def fetch(self, request_id: str, *, wait_seconds: float) -> ImageResult: ...


ASPECTS: dict[str, float] = {
    "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 1.5, "2:3": 2 / 3, "16:9": 16 / 9,
    "9:16": 9 / 16, "21:9": 21 / 9,
}  # fmt: skip


def nearest_aspect(width: float, height: float, supported: tuple[str, ...] | None = None) -> str:
    """Map a panel rectangle to the closest aspect ratio the image model supports."""
    ratio = width / height
    names = supported or tuple(ASPECTS)
    return min(names, key=lambda n: abs(ASPECTS[n] - ratio) / ASPECTS[n])
