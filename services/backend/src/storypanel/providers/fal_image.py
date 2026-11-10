"""fal.ai adapter: multi-reference generation and editing, region repair, upscaling.

Contracts come from `fal_profiles.PROFILES` (fal's published endpoint types). Transport is
the official `fal-client` queue API: `submit` returns a request id at once, which the
caller persists before waiting. Afterwards the request can be resumed (`status`/`result`)
or cancelled (`cancel`) by that id.

Outcome classification, from the SDK's own status model:
- `Queued` / `InProgress` past our wait budget  -> RequestPending (resume later by id)
- `Completed` with `error`                       -> definitive failure (no image)
- HTTP 404 on status                             -> RequestUnknown (never accepted)
- transport timeout                              -> ambiguous (id kept, resume)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlparse

import fal_client
import httpx
from fal_client.client import Completed, FalClientHTTPError, FalClientTimeoutError

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import CallContext, ImageRequest, ImageResult, RequestPending, RequestUnknown
from storypanel.providers.fal_profiles import FalProfile, profile_for

MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
_SAFETY_WORDS = ("content_policy", "nsfw", "safety", "moderation", "prohibited")
_TOLERANCE = {"all_ages": "2", "teen": "3", "mature": "4"}


def _map_error(exc: Exception, what: str) -> AppError:
    if isinstance(exc, FalClientTimeoutError | httpx.TimeoutException | asyncio.TimeoutError):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, f"The {what} service timed out.", retryable=True)
    if isinstance(exc, FalClientHTTPError):
        detail = f"{getattr(exc, 'error_type', '') or ''} {exc}".lower()
        if exc.status_code == 429:
            return AppError(ErrorCode.PROVIDER_THROTTLED, f"The {what} service is busy.")
        if exc.status_code == 422 and any(w in detail for w in _SAFETY_WORDS):
            return AppError(ErrorCode.MODERATION_BLOCKED, "The illustration service refused this content.")
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


def build_arguments(profile: FalProfile, req: ImageRequest, *, reference_urls: list[str],
                    image_url: str | None, mask_url: str | None) -> dict[str, Any]:  # fmt: skip
    """The exact JSON body for one request. Pure, so tests can check it field by field
    against the published schema without network access."""
    args: dict[str, Any] = {**profile.fixed, "prompt": req.prompt}
    args.update(profile.size_args(req.aspect))
    if profile.reference_field:
        if not reference_urls:
            raise AppError(ErrorCode.VALIDATION, f"{profile.endpoint} needs at least one reference image.",
                           retryable=False)  # fmt: skip
        args[profile.reference_field] = reference_urls[: profile.max_references]
    elif reference_urls:
        raise AppError(
            ErrorCode.VALIDATION, f"{profile.endpoint} takes no reference images.", retryable=False
        )
    if profile.image_field:
        if image_url is None:
            raise AppError(ErrorCode.VALIDATION, f"{profile.endpoint} needs an input image.", retryable=False)
        args[profile.image_field] = image_url
    if profile.mask_field:
        if mask_url is None:
            raise AppError(ErrorCode.VALIDATION, f"{profile.endpoint} needs a mask.", retryable=False)
        args[profile.mask_field] = mask_url
    if profile.seed_field and req.seed is not None:
        args[profile.seed_field] = req.seed
    if "safety_tolerance" in profile.fixed or profile.endpoint.startswith("fal-ai/nano-banana"):
        args["safety_tolerance"] = _TOLERANCE.get(req.audience, "2")
    return args


def extract_result_url(profile: FalProfile, result: Any) -> str:
    try:
        if profile.nsfw_flags_field and any(result.get(profile.nsfw_flags_field) or []):
            raise AppError(ErrorCode.MODERATION_BLOCKED, "The illustration service flagged this image.")
        url = result["image"]["url"] if profile.result == "image" else result["images"][0]["url"]
    except AppError:
        raise
    except (KeyError, IndexError, TypeError) as exc:
        raise AppError(
            ErrorCode.PROVIDER_BAD_OUTPUT, "The image service returned an unexpected response."
        ) from exc
    if not isinstance(url, str):
        raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "The image service returned an unexpected response.")
    return url


class _FalBase:
    name = "fal"

    def __init__(self, settings: Settings) -> None:
        assert settings.fal_key is not None
        self._s = settings
        self._client = fal_client.AsyncClient(key=settings.fal_key.get_secret_value(), default_timeout=120.0)
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
                    if status.error:
                        text = f"{status.error_type or ''} {status.error}".lower()
                        if any(w in text for w in _SAFETY_WORDS):
                            raise AppError(ErrorCode.MODERATION_BLOCKED,
                                           "The illustration service refused this content.")  # fmt: skip
                        # Definitive failure: nothing was produced, a new request is safe.
                        raise AppError(ErrorCode.PROVIDER_ERROR, "The illustration failed at the provider.",
                                       meta={"error_type": status.error_type})  # fmt: skip
                    return await self._client.result(app, request_id)
                if time.monotonic() >= deadline:
                    raise RequestPending(request_id)
                await asyncio.sleep(1.0)
        except (RequestPending, AppError):
            raise
        except FalClientHTTPError as exc:
            if exc.status_code == 404:
                raise RequestUnknown(request_id) from exc
            raise _map_error(exc, "illustration") from exc
        except Exception as exc:
            raise _map_error(exc, "illustration") from exc

    async def _cancel(self, app: str, request_id: str) -> None:
        try:
            await self._client.cancel(app, request_id)
        except Exception as exc:  # best effort: a finished request cannot be cancelled
            raise _map_error(exc, "illustration") from exc


class FalImageProvider(_FalBase):
    def _profile(self, model: str, role: str) -> FalProfile:
        p = profile_for(model, role)  # type: ignore[arg-type]
        if p is None:
            raise AppError(
                ErrorCode.VALIDATION, f"No verified contract for {model} as {role}.", retryable=False
            )
        return p

    def model_for(self, req: ImageRequest) -> str:
        if req.model:
            return req.model
        model = {"generate": self._s.image_model_generate, "edit": self._s.image_model_edit,
                 "inpaint": self._s.inpaint_model}[req.role]  # fmt: skip
        if model is None:
            raise AppError(ErrorCode.VALIDATION, f"No {req.role} model is configured.", retryable=False)
        return model

    def fallback_for(self, req: ImageRequest) -> str | None:
        return {"generate": self._s.image_model_fallback_generate, "edit": self._s.image_model_fallback}.get(
            req.role
        )

    async def submit(self, req: ImageRequest, ctx: CallContext, *, model: str) -> str:
        profile = self._profile(model, req.role)
        try:
            limit = min(self._s.image_max_references, profile.max_references)
            refs = [await self._upload(r.data, r.media_type) for r in req.references[:limit]]
            image_url = await self._upload(req.image, "image/png") if req.image is not None else None
            mask_url = await self._upload(req.mask, "image/png") if req.mask is not None else None
            args = build_arguments(profile, req, reference_urls=refs, image_url=image_url, mask_url=mask_url)
            handle = await self._client.submit(model, args)
        except AppError:
            raise
        except Exception as exc:
            raise _map_error(exc, "illustration") from exc
        return str(handle.request_id)

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult:
        profile = next(p for role in ("generate", "edit", "inpaint")
                       if (p := profile_for(model, role)) is not None)  # type: ignore[arg-type]  # fmt: skip
        result = await self._wait(model, request_id, wait_seconds)
        data = await download_image(extract_result_url(profile, result))
        seed = result.get("seed") if isinstance(result, dict) else None
        return ImageResult(data=data, provider=self.name, model=model, request_id=request_id,
                           seed=seed if isinstance(seed, int) else None)  # fmt: skip

    async def cancel(self, model: str, request_id: str) -> None:
        await self._cancel(model, request_id)


class FalUpscaleProvider(_FalBase):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        assert settings.upscale_model is not None
        self.model = settings.upscale_model
        p = profile_for(self.model, "upscale")
        if p is None:
            raise AppError(
                ErrorCode.VALIDATION, f"No verified contract for upscaler {self.model}.", retryable=False
            )
        self.profile = p

    async def submit(self, image: bytes, factor: int, ctx: CallContext) -> str:
        try:
            url = await self._upload(image, "image/png")
            args = {**self.profile.fixed, "image_url": url, "scale": factor}
            handle = await self._client.submit(self.model, args)
        except Exception as exc:
            raise _map_error(exc, "upscaling") from exc
        return str(handle.request_id)

    async def fetch(self, request_id: str, *, wait_seconds: float) -> ImageResult:
        result = await self._wait(self.model, request_id, wait_seconds)
        data = await download_image(extract_result_url(self.profile, result))
        return ImageResult(data=data, provider=self.name, model=self.model, request_id=request_id)

    async def cancel(self, request_id: str) -> None:
        await self._cancel(self.model, request_id)
