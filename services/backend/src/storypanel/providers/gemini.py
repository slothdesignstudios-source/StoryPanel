"""Google Gemini API adapters (google-genai SDK 3.3, called directly; not through fal).

1. `GeminiImageProvider`: image generation and reference-conditioned edits with a Gemini image
   model (`generate_content`, response modality IMAGE). Input images are sent inline; the app
   sends the base image (edits/repairs) first, then the approved character references, capped by
   `SP_IMAGE_MAX_REFERENCES`. Aspect ratio is one of the SDK-documented values; size is
   `SP_GEMINI_IMAGE_SIZE` (1K default). No pixel masks: repairs are instruction edits that the
   app composites locally (pipeline/repair.py), the same as for OpenAI.

2. `GeminiVisionProvider`: structured visual judgments (identity QA) with a Gemini text model,
   JSON output constrained by the schema (`response_json_schema`) and validated with Pydantic.
   An invalid or empty answer is an error, never a pass.

Both are one synchronous call with no request id to fetch again and no cancellation. SDK
retries are off (`HttpRetryOptions(attempts=1)`): every retry is a separately reserved attempt.
A timeout or dropped connection is ambiguous and never re-sent automatically.
"""

from __future__ import annotations

import io
import time
from typing import Any

import httpx
from google import genai
from google.genai import errors as gerrors
from google.genai import types as gt
from PIL import Image
from pydantic import ValidationError

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import (
    CallContext,
    ImageCapabilities,
    ImageInput,
    ImageRequest,
    SyncImageResult,
    T,
    TextRole,
    Usage,
    nearest_aspect,
)  # fmt: skip
from storypanel.providers.pricing import ImageUsage

GEMINI_ASPECTS = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9")  # SDK ImageConfig docstring
_REFUSALS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
                       "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "RECITATION", "JAILBREAK", "MODEL_ARMOR"})  # fmt: skip
LONG_EDGE = {"1K": 1024, "2K": 2048}


def _client(settings: Settings) -> genai.Client:
    assert settings.gemini_api_key is not None
    return genai.Client(api_key=settings.gemini_api_key.get_secret_value(), http_options=gt.HttpOptions(
        timeout=int(settings.image_request_timeout_seconds * 1000),
        retry_options=gt.HttpRetryOptions(attempts=1)))  # fmt: skip


def map_error(exc: Exception, what: str) -> AppError:
    if isinstance(exc, httpx.TimeoutException):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, f"The {what} service timed out.", retryable=True,
                        meta={"provider": "gemini", "kind": "timeout"})  # fmt: skip
    if isinstance(exc, httpx.TransportError):
        return AppError(ErrorCode.PROVIDER_AMBIGUOUS, f"The connection to the {what} service was lost.",
                        retryable=True, meta={"provider": "gemini", "kind": "connection"})  # fmt: skip
    if isinstance(exc, gerrors.APIError):
        code = int(getattr(exc, "code", 0) or 0)
        meta = {"provider": "gemini", "status": code, "provider_code": str(getattr(exc, "status", ""))[:60]}
        if code == 429:
            return AppError(ErrorCode.PROVIDER_THROTTLED, f"The {what} service is busy.", meta=meta)
        if isinstance(exc, gerrors.ServerError) or code >= 500:
            return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service is having problems.", retryable=True,
                            meta=meta)  # fmt: skip
        return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service rejected the request.", retryable=False,
                        meta=meta)  # fmt: skip
    return AppError(ErrorCode.PROVIDER_ERROR, f"The {what} service failed.", meta={"provider": "gemini"})


def _modality_tokens(details: list[Any] | None, modality: str) -> int | None:
    if details is None:
        return None
    return sum(
        int(d.token_count or 0) for d in details if str(getattr(d.modality, "value", d.modality)) == modality
    )


def usage_from(resp: Any) -> ImageUsage:
    """Gemini reports prompt tokens by modality, candidate tokens by modality, and thinking tokens
    separately (billed as text output)."""
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return ImageUsage()
    thoughts = int(u.thoughts_token_count or 0)
    out_text = _modality_tokens(u.candidates_tokens_details, "TEXT")
    out_img = _modality_tokens(u.candidates_tokens_details, "IMAGE")
    if u.candidates_tokens_details is None and u.candidates_token_count is not None:
        out_text, out_img = 0, int(u.candidates_token_count)  # image-only response without a breakdown
    return ImageUsage(input_text_tokens=_modality_tokens(u.prompt_tokens_details, "TEXT"),
                      input_image_tokens=_modality_tokens(u.prompt_tokens_details, "IMAGE"),
                      output_text_tokens=None if out_text is None else out_text + thoughts,
                      output_image_tokens=out_img)  # fmt: skip


def refusal_reason(resp: Any) -> str | None:
    fb = getattr(resp, "prompt_feedback", None)
    block = getattr(getattr(fb, "block_reason", None), "value", None) if fb is not None else None
    if block and block in _REFUSALS:
        return str(block)
    for cand in getattr(resp, "candidates", None) or []:
        reason = getattr(getattr(cand, "finish_reason", None), "value", None)
        if reason in _REFUSALS:
            return str(reason)
    return None


def build_contents(req: ImageRequest, max_refs: int) -> list[Any]:
    """Prompt first, then the base image (edits/repairs), then references, never more than the cap."""
    if req.mode == "inpaint":
        raise AppError(ErrorCode.VALIDATION, "Gemini images are edited by instruction, not by mask.",
                       retryable=False)  # fmt: skip
    images = ([req.image] if req.image is not None else []) + [r.data for r in req.references]
    parts: list[Any] = [req.prompt]
    parts += [gt.Part.from_bytes(data=d, mime_type="image/png") for d in images[: max(1, max_refs)]]
    return parts


def image_config(req: ImageRequest, size: str) -> gt.GenerateContentConfig:
    return gt.GenerateContentConfig(
        response_modalities=["IMAGE"], candidate_count=1,
        image_config=gt.ImageConfig(aspect_ratio=nearest_aspect(*_ratio(req.aspect), GEMINI_ASPECTS),
                                    image_size=size))  # fmt: skip


def _ratio(aspect: str) -> tuple[float, float]:
    a, b = aspect.split(":")
    return float(a), float(b)


class GeminiImageProvider:
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.capabilities = ImageCapabilities(
            max_reference_images=settings.image_max_references + 1, supports_masked_inpainting=False,
            instruction_edits=True, max_long_edge=LONG_EDGE[settings.gemini_image_size])  # fmt: skip
        self._client = _client(settings)

    async def generate(self, req: ImageRequest, ctx: CallContext, *, model: str) -> SyncImageResult:
        try:
            resp = await self._client.aio.models.generate_content(
                model=model, contents=build_contents(req, self.capabilities.max_reference_images),
                config=image_config(req, self._s.gemini_image_size))  # fmt: skip
        except (gerrors.APIError, httpx.HTTPError) as exc:
            raise map_error(exc, "image") from exc
        usage = usage_from(resp)
        refused = refusal_reason(resp)
        if refused:
            raise AppError(ErrorCode.MODERATION_BLOCKED, "The image service refused this content.",
                           retryable=False, meta={"provider": "gemini", "source": "provider_refusal",
                                                  "reason": refused, "usage": usage.as_dict()})  # fmt: skip
        data = None
        for cand in resp.candidates or []:
            for part in (cand.content.parts if cand.content else None) or []:
                if part.inline_data is not None and part.inline_data.data:
                    data = part.inline_data.data
                    break
            if data:
                break
        if not data:
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "The image service returned no image.",
                           meta={"provider": "gemini", "usage": usage.as_dict()})  # fmt: skip
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if im.format != "PNG":
                out = io.BytesIO()
                im.convert("RGB").save(out, format="PNG")
                data = out.getvalue()
        return SyncImageResult(data, self.name, model, getattr(resp, "response_id", None), usage, w, h)


class GeminiVisionProvider:
    """Structured visual judgments; used through metering.structured like the text provider."""

    name = "gemini"
    max_tokens = 4096  # output cap requested; thinking is kept minimal for a judging task

    def __init__(self, settings: Settings) -> None:
        assert settings.gemini_qa_model is not None
        self._model = settings.gemini_qa_model
        self._client = _client(settings)

    def model_for(self, role: TextRole) -> str:
        return self._model

    async def structured(self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
                         images: tuple[ImageInput, ...] = ()) -> tuple[T, Usage]:  # fmt: skip
        parts: list[Any] = []
        for img in images:
            if img.label:
                parts.append(img.label)
            parts.append(gt.Part.from_bytes(data=img.data, mime_type=img.media_type))
        parts.append(user)
        cfg = gt.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(), max_output_tokens=self.max_tokens,
            candidate_count=1, thinking_config=gt.ThinkingConfig(thinking_level=gt.ThinkingLevel.MINIMAL))  # fmt: skip
        started = time.monotonic()
        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model, contents=parts, config=cfg
            )
        except (gerrors.APIError, httpx.HTTPError) as exc:
            raise map_error(exc, "consistency check") from exc
        u = getattr(resp, "usage_metadata", None)
        usage = Usage(provider=self.name, model=self._model, operation=f"text_{role}",
                      input_tokens=int(getattr(u, "prompt_token_count", 0) or 0),
                      output_tokens=int(getattr(u, "candidates_token_count", 0) or 0)
                      + int(getattr(u, "thoughts_token_count", 0) or 0),
                      latency_ms=int((time.monotonic() - started) * 1000),
                      request_id=getattr(resp, "response_id", None))  # fmt: skip
        refused = refusal_reason(resp)
        if refused:
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "The consistency check was declined.",
                           meta={"provider": "gemini", "reason": refused})  # fmt: skip
        try:
            return schema.model_validate_json(resp.text or ""), usage
        except (ValidationError, ValueError) as exc:
            raise AppError(
                ErrorCode.PROVIDER_BAD_OUTPUT,
                "The consistency check returned an unusable answer.",
                meta={"provider": "gemini"},
            ) from exc
