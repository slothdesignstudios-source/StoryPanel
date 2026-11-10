"""fal.ai adapter for multi-reference image generation, editing and upscaling.

Built on the official `fal-client` queue API (submit / status / result), which gives a
request id immediately. That id is persisted before waiting, so a worker crash or
timeout is reconciled by asking fal about the original request instead of paying twice.

The request/response field names differ per hosted model. They are configuration
(`ModelProfile`), not code. The built-in profile follows fal's published examples for
multi-reference edit endpoints (`prompt` + `image_urls`); confirm it against the model's
schema page before enabling a new model. See docs/providers.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import fal_client
import httpx
from fal_client.client import Completed, FalClientHTTPError, FalClientTimeoutError

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import CallContext, ImageRequest, ImageResult, RequestPending

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True)
class ModelProfile:
    prompt_field: str = "prompt"
    references_field: str = "image_urls"
    aspect_field: str | None = "aspect_ratio"
    seed_field: str | None = "seed"
    supported_aspects: tuple[str, ...] = ("1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "21:9")
    extra: dict[str, Any] = field(default_factory=lambda: {"num_images": 1, "output_format": "png"})
    result_path: tuple[str | int, ...] = ("images", 0, "url")


@dataclass(frozen=True)
class UpscaleProfile:
    image_field: str = "image_url"
    scale_field: str = "scale"
    result_path: tuple[str | int, ...] = ("image", "url")


def _map_error(exc: Exception, what: str) -> AppError:
    if isinstance(exc, FalClientTimeoutError | httpx.TimeoutException | asyncio.TimeoutError):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, f"The {what} service timed out.", retryable=True)
    if isinstance(exc, FalClientHTTPError):
        if exc.status_code == 429:
            return AppError(ErrorCode.PROVIDER_THROTTLED, f"The {what} service is busy.")
        if exc.status_code >= 500:
            return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service is having problems.")
        return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service rejected the request.",
                        retryable=False, meta={"status": exc.status_code})  # fmt: skip
    if isinstance(exc, httpx.HTTPError):
        return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service is unreachable.")
    return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service failed.")


def assert_public_https_url(url: str) -> None:
    """SSRF guard for provider-supplied result URLs."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Provider returned an unusable image link.",
                       retryable=False)  # fmt: skip
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AppError(ErrorCode.PROVIDER_ERROR, "Provider image host did not resolve.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT,
                           "Provider returned an unusable image link.", retryable=False)  # fmt: skip


async def download_image(url: str) -> bytes:
    await asyncio.to_thread(assert_public_https_url, url)
    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as http:
        async with http.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise AppError(ErrorCode.PROVIDER_ERROR, "Could not download the generated image.")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Generated image is too large.",
                                   retryable=False)  # fmt: skip
                chunks.append(chunk)
    return b"".join(chunks)


def _dig(data: Any, path: tuple[str | int, ...]) -> Any:
    try:
        for key in path:
            data = data[key]
        return data
    except (KeyError, IndexError, TypeError) as exc:
        raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT,
                       "The image service returned an unexpected response.") from exc  # fmt: skip


class _FalBase:
    name = "fal"

    def __init__(self, settings: Settings) -> None:
        assert settings.fal_key is not None
        self._s = settings
        self._client = fal_client.AsyncClient(key=settings.fal_key.get_secret_value(),
                                              default_timeout=120.0)  # fmt: skip
        self._uploads: dict[str, tuple[float, str]] = {}

    async def _upload(self, data: bytes, media_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        cached = self._uploads.get(digest)
        if cached and time.monotonic() - cached[0] < 900:
            return cached[1]
        url = await self._client.upload(data, media_type)
        self._uploads[digest] = (time.monotonic(), url)
        return url  # type: ignore[no-any-return]

    async def _wait(self, app: str, request_id: str, wait_seconds: float) -> Any:
        deadline = time.monotonic() + wait_seconds
        try:
            while True:
                status = await self._client.status(app, request_id)
                if isinstance(status, Completed):
                    return await self._client.result(app, request_id)
                if time.monotonic() >= deadline:
                    raise RequestPending(request_id)
                await asyncio.sleep(1.0)
        except RequestPending:
            raise
        except Exception as exc:
            raise _map_error(exc, "illustration") from exc


class FalImageProvider(_FalBase):
    def __init__(self, settings: Settings, profile: ModelProfile | None = None) -> None:
        super().__init__(settings)
        self.profile = profile or ModelProfile()

    def model_for(self, req: ImageRequest) -> str:
        if req.model:
            return req.model
        use_edit = req.mode == "edit" or bool(req.references)
        model = self._s.image_model_edit if use_edit else self._s.image_model_generate
        assert model is not None
        return model

    async def submit(self, req: ImageRequest, ctx: CallContext) -> str:
        p = self.profile
        args: dict[str, Any] = {**p.extra, p.prompt_field: req.prompt}
        try:
            if req.references:
                refs = req.references[: self._s.image_max_references]
                args[p.references_field] = [await self._upload(r.data, r.media_type) for r in refs]
            if p.aspect_field and req.aspect in p.supported_aspects:
                args[p.aspect_field] = req.aspect
            if p.seed_field and req.seed is not None:
                args[p.seed_field] = req.seed
            handle = await self._client.submit(self.model_for(req), args)
        except Exception as exc:
            raise _map_error(exc, "illustration") from exc
        return str(handle.request_id)

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult:
        result = await self._wait(model, request_id, wait_seconds)
        url = _dig(result, self.profile.result_path)
        try:
            data = await download_image(str(url))
        except AppError:
            raise
        except Exception as exc:
            raise _map_error(exc, "illustration") from exc
        seed = result.get("seed") if isinstance(result, dict) else None
        return ImageResult(data=data, provider=self.name, model=model, request_id=request_id,
                           seed=seed if isinstance(seed, int) else None)  # fmt: skip


class FalUpscaleProvider(_FalBase):
    def __init__(self, settings: Settings, profile: UpscaleProfile | None = None) -> None:
        super().__init__(settings)
        assert settings.upscale_model is not None
        self.model = settings.upscale_model
        self.profile = profile or UpscaleProfile()

    async def submit(self, image: bytes, factor: int, ctx: CallContext) -> str:
        try:
            url = await self._upload(image, "image/png")
            handle = await self._client.submit(
                self.model, {self.profile.image_field: url, self.profile.scale_field: factor}
            )
        except Exception as exc:
            raise _map_error(exc, "upscaling") from exc
        return str(handle.request_id)

    async def fetch(self, request_id: str, *, wait_seconds: float) -> ImageResult:
        result = await self._wait(self.model, request_id, wait_seconds)
        data = await download_image(str(_dig(result, self.profile.result_path)))
        return ImageResult(data=data, provider=self.name, model=self.model, request_id=request_id)
