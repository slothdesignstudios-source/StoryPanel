"""OpenAI Images adapter (GPT Image models): generation and reference-conditioned edits.

Contract (OpenAI Python SDK 3.51 types, `images.generate` / `images.edit`; docs/providers.md):
- one synchronous call per image; no request id to fetch again, no cancellation;
- `images.edit` accepts up to 16 input images (png/webp/jpg); the app sends the base image (for
  edits/repairs) first, then the approved character references, capped by
  `SP_IMAGE_MAX_REFERENCES`;
- sizes are explicit `WIDTHxHEIGHT`, both divisible by 16, aspect between 1:3 and 3:1;
- GPT Image models always return base64 and report token usage (text/image input, image output).

Not used: the `mask` parameter. Repairs are instruction-based edits of the whole panel; the app
then keeps only the repaired region by compositing locally (pipeline/repair.py). This is not
provider-side masked inpainting and is not described as such.

The SDK's own retries are disabled (`max_retries=0`): every retry is a visible, separately
reserved attempt in the ledger. A timeout or a dropped connection after the request was sent is
ambiguous (the image may have been generated and billed) and is never re-sent automatically.
"""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Any

import openai
from PIL import Image

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import (
    ASPECTS,
    CallContext,
    ImageCapabilities,
    ImageRequest,
    SyncImageResult,
)  # fmt: skip
from storypanel.providers.pricing import ImageUsage

API_REFERENCE_LIMIT = 16  # images.edit input images (SDK docstring)
LONG_EDGE = 1536  # the GPT Image standard large size; above 2560x1440 is experimental
_REFUSAL_CODES = frozenset(
    {"moderation_blocked", "content_policy_violation", "image_content_policy_violation"}
)


def size_for(aspect: str, long_edge: int = LONG_EDGE) -> str:
    """`WIDTHxHEIGHT` for an aspect: long edge `long_edge`, both sides divisible by 16, ratio
    clamped to the API's 1:3 .. 3:1."""
    ratio = min(3.0, max(1 / 3, ASPECTS.get(aspect, 1.0)))
    w, h = (float(long_edge), long_edge / ratio) if ratio >= 1 else (long_edge * ratio, float(long_edge))
    return f"{max(16, round(w / 16) * 16)}x{max(16, round(h / 16) * 16)}"


def build_arguments(req: ImageRequest, *, model: str, quality: str, max_refs: int,
                    safety_identifier: str | None) -> dict[str, Any]:  # fmt: skip
    """The exact request body (files excluded). Pure, so tests check it field by field."""
    if req.mode == "inpaint":
        raise AppError(ErrorCode.VALIDATION, "OpenAI images are edited by instruction, not by mask.",
                       retryable=False)  # fmt: skip
    args: dict[str, Any] = {"model": model, "prompt": req.prompt, "n": 1, "size": size_for(req.aspect),
                            "quality": quality, "output_format": "png"}  # fmt: skip
    if safety_identifier:
        args["safety_identifier"] = safety_identifier
    images = _inputs(req, max_refs)
    if images:
        args["_images"] = images  # sent as files by `generate`
    return args


def _inputs(req: ImageRequest, max_refs: int) -> list[bytes]:
    """Base image first (edits and repairs), then references, never more than the cap."""
    out = [req.image] if req.image is not None else []
    out += [r.data for r in req.references]
    return out[: max(1, max_refs)]


def safety_id(user_id: str | None) -> str | None:
    """A stable, non-identifying end-user id for OpenAI abuse monitoring (hash of our user id)."""
    return hashlib.sha256(f"storypanel:{user_id}".encode()).hexdigest()[:32] if user_id else None


def map_error(exc: Exception) -> AppError:
    if isinstance(exc, openai.APITimeoutError):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, "The image service timed out.", retryable=True,
                        meta={"provider": "openai", "kind": "timeout"})  # fmt: skip
    if isinstance(exc, openai.APIConnectionError):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, "The connection to the image service was lost.",
                        retryable=True, meta={"provider": "openai", "kind": "connection"})  # fmt: skip
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        code = str(getattr(exc, "code", "") or "")
        meta = {"provider": "openai", "status": status, "provider_code": code[:60]}
        if status == 429:
            return AppError(ErrorCode.PROVIDER_THROTTLED, "The image service is busy.", meta=meta)
        if status == 400 and (code in _REFUSAL_CODES or "safety" in str(exc).lower()):
            return AppError(ErrorCode.MODERATION_BLOCKED, "The image service refused this content.",
                            retryable=False, meta={**meta, "source": "provider_refusal"})  # fmt: skip
        if status >= 500:
            return AppError(ErrorCode.PROVIDER_ERROR, "The image service is having problems.", retryable=True,
                            meta=meta)  # fmt: skip
        return AppError(ErrorCode.PROVIDER_ERROR, "The image service rejected the request.", retryable=False,
                        meta=meta)  # fmt: skip
    return AppError(ErrorCode.PROVIDER_ERROR, "The image service failed.", meta={"provider": "openai"})


def usage_from(resp: Any) -> ImageUsage:
    u = getattr(resp, "usage", None)
    if u is None:
        return ImageUsage()
    ind = getattr(u, "input_tokens_details", None)
    outd = getattr(u, "output_tokens_details", None)
    out_img = getattr(outd, "image_tokens", None) if outd is not None else None
    out_txt = getattr(outd, "text_tokens", None) if outd is not None else None
    if outd is None:  # no breakdown: every output token is an image token for GPT Image
        out_img, out_txt = u.output_tokens, 0
    return ImageUsage(input_text_tokens=getattr(ind, "text_tokens", None),
                      input_image_tokens=getattr(ind, "image_tokens", None),
                      output_text_tokens=out_txt, output_image_tokens=out_img)  # fmt: skip


class OpenAIImageProvider:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        assert settings.openai_api_key is not None
        self._s = settings
        self.capabilities = ImageCapabilities(
            max_reference_images=min(API_REFERENCE_LIMIT, settings.image_max_references + 1),
            supports_masked_inpainting=False, instruction_edits=True, max_long_edge=LONG_EDGE)  # fmt: skip
        self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0,
                                          timeout=settings.image_request_timeout_seconds)  # fmt: skip

    async def generate(self, req: ImageRequest, ctx: CallContext, *, model: str) -> SyncImageResult:
        args = build_arguments(req, model=model, quality=self._s.openai_image_quality,
                               max_refs=self.capabilities.max_reference_images,
                               safety_identifier=safety_id(ctx.user_id))  # fmt: skip
        images = args.pop("_images", None)
        try:
            if images:
                files = [(f"input-{n}.png", data, "image/png") for n, data in enumerate(images)]
                raw = await self._client.images.with_raw_response.edit(image=files, **args)
            else:
                raw = await self._client.images.with_raw_response.generate(**args)
        except openai.OpenAIError as exc:
            raise map_error(exc) from exc
        request_id = raw.headers.get("x-request-id")
        resp = raw.parse()
        data = (resp.data or [None])[0]
        b64 = getattr(data, "b64_json", None) if data is not None else None
        if not b64:
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "The image service returned no image.",
                           meta={"provider": "openai", "usage": usage_from(resp).as_dict()})  # fmt: skip
        png = base64.b64decode(b64)
        with Image.open(io.BytesIO(png)) as im:
            w, h = im.size
        return SyncImageResult(png, self.name, model, request_id, usage_from(resp), w, h)
