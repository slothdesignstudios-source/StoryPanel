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
    mode: Literal["generate", "edit", "inpaint"] = "generate"
    seed: int | None = None
    model: str | None = None  # override (fallback model)
    inputs: dict[str, Any] = field(default_factory=dict)
    image: bytes | None = None  # inpaint: the image to repair
    mask: bytes | None = None  # inpaint: white = repaint, black = keep; same size as image
    audience: str = "all_ages"

    @property
    def role(self) -> str:
        if self.mode == "inpaint":
            return "inpaint"
        return "edit" if (self.mode == "edit" or self.references) else "generate"


@dataclass(frozen=True)
class ImageResult:
    data: bytes
    provider: str
    model: str
    request_id: str | None
    seed: int | None = None


class RequestPending(Exception):
    """The provider still holds the request; it has neither succeeded nor failed."""


class RequestUnknown(Exception):
    """The provider cannot produce this request's result any more: the id is unknown to the
    endpoint (never accepted, or past retention) or the result file has expired.

    This is NOT proof that nothing was produced or billed. Callers keep the attempt's
    estimated cost counted as an unresolved potential charge."""

    def __init__(self, request_id: str, reason: str = "unknown_request") -> None:
        super().__init__(request_id)
        self.request_id = request_id
        self.reason = reason


class RetrievalFailed(Exception):
    """A transient failure while asking about, or downloading, an accepted request (5xx,
    429, connection reset, timeout). The request id stays valid: retry retrieval, never
    generation."""

    def __init__(self, request_id: str, reason: str) -> None:
        super().__init__(f"{request_id}: {reason}")
        self.request_id = request_id
        self.reason = reason


class JobFailed(Exception):
    """The provider definitively reported that this accepted request failed and produced
    no output. A new attempt is permitted; whether the failure was billed is policy."""

    def __init__(self, request_id: str, error_type: str | None = None, *, moderation: bool = False) -> None:
        super().__init__(f"{request_id}: {error_type or 'failed'}")
        self.request_id = request_id
        self.error_type = error_type
        self.moderation = moderation


class TextProvider(Protocol):
    name: str
    max_tokens: int

    def model_for(self, role: TextRole) -> str: ...

    async def structured(
        self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
        images: tuple[ImageInput, ...] = (),
    ) -> tuple[T, Usage]: ...  # fmt: skip


class ImageProvider(Protocol):
    """Two-step so an ambiguous timeout can be reconciled by request id, not re-paid."""

    name: str

    def model_for(self, req: ImageRequest) -> str: ...

    def fallback_for(self, req: ImageRequest) -> str | None: ...

    async def submit(self, req: ImageRequest, ctx: CallContext, *, model: str) -> str: ...

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult: ...

    async def cancel(self, model: str, request_id: str) -> None: ...


class EmbeddingProvider(Protocol):
    name: str
    model: str

    async def embed_images(self, images: list[bytes], ctx: CallContext) -> list[list[float]]: ...


class UpscaleProvider(Protocol):
    name: str
    model: str

    async def submit(self, image: bytes, factor: int, ctx: CallContext, *, model: str) -> str: ...

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult: ...

    async def cancel(self, model: str, request_id: str) -> None: ...


ASPECTS: dict[str, float] = {
    "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 1.5, "2:3": 2 / 3, "16:9": 16 / 9,
    "9:16": 9 / 16, "21:9": 21 / 9,
}  # fmt: skip


def nearest_aspect(width: float, height: float, supported: tuple[str, ...] | None = None) -> str:
    """Map a panel rectangle to the closest aspect ratio the image model supports."""
    ratio = width / height
    names = supported or tuple(ASPECTS)
    return min(names, key=lambda n: abs(ASPECTS[n] - ratio) / ASPECTS[n])


# ------------------------------------------------------------------ synchronous image providers
@dataclass(frozen=True)
class ImageCapabilities:
    """What a synchronous image provider can actually do; the pipeline adapts to it."""

    max_reference_images: int  # input images per request the app will send (<= the API limit)
    supports_masked_inpainting: bool  # pixel-mask edits honoured by the provider
    instruction_edits: bool  # edits an input image from a text instruction
    max_long_edge: int  # longest side the app requests, in pixels
    output_format: str = "png"


@dataclass(frozen=True)
class SyncImageResult:
    data: bytes
    provider: str
    model: str
    request_id: str | None  # the provider's id for the call, when it reports one (support/billing)
    usage: Any  # pricing.ImageUsage (provider-reported; fields None when not reported)
    width: int = 0
    height: int = 0


class SyncImageProvider(Protocol):
    """One request, one answer (OpenAI Images API, Gemini generateContent). There is no request
    id to resume by and no cancellation: an ambiguous outcome cannot be reconciled with the
    provider, so it is never re-sent automatically (see providers/metering.py)."""

    name: str  # adapter / billing provider name (e.g. "openai")
    capabilities: ImageCapabilities

    async def generate(self, req: ImageRequest, ctx: CallContext, *, model: str) -> SyncImageResult: ...
