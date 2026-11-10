"""Claude adapter for story writing, light extraction, moderation and vision QA.

Uses the official SDK's structured-output helper (`messages.parse` with a Pydantic
`output_format`) so every stage returns schema-valid JSON. User text and images are data
in the user turn, never the system prompt, and no tools are exposed, so prompt content
cannot acquire capabilities.

Refusals: a model-level decline (`stop_reason == "refusal"`) is a policy outcome, not an
outage, and is never retried. For models that support it, the request opts into the
server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`)
so a false positive on benign story text is re-run on Anthropic's recommended fallback
model inside the same call. The model that actually served the response is recorded and
priced; if it has no price entry the call's cost stays at its (conservative) estimate.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import anthropic
from pydantic import ValidationError

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import CallContext, ImageInput, T, TextRole, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models documented as accepting `fallbacks: "default"`. Haiku-class models have no
# server-side fallback (Anthropic model-migration guide), so they are not listed.
FALLBACK_CAPABLE = frozenset({"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-2",
                              "claude-fable-5-1"})  # fmt: skip


class AnthropicTextProvider:
    name = "anthropic"
    max_tokens = 16000

    def __init__(self, settings: Settings) -> None:
        assert settings.anthropic_api_key is not None
        self._s = settings
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=180.0, max_retries=0
        )  # retries are owned by Temporal so attempts are visible and bounded

    def model_for(self, role: TextRole) -> str:
        model = {"story": self._s.text_model_story, "light": self._s.text_model_light,
                 "vision": self._s.vision_model}[role]  # fmt: skip
        assert model is not None  # guaranteed by Settings validation in live mode
        return model

    def request_args(self, role: TextRole, model: str) -> dict[str, Any]:
        """Per-call options. Pure, so the shape is unit-tested without network access."""
        args: dict[str, Any] = {"output_config": {"effort": "medium" if role == "story" else "low"}}
        if self._s.text_refusal_fallback and model in FALLBACK_CAPABLE:
            args["betas"] = [FALLBACK_BETA]
            args["fallbacks"] = "default"
        return args

    async def structured(
        self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
        images: tuple[ImageInput, ...] = (),
    ) -> tuple[T, Usage]:  # fmt: skip
        model = self.model_for(role)
        content: list[dict[str, Any]] = []
        for img in images:
            if img.label:
                content.append({"type": "text", "text": img.label})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": img.media_type,
                "data": base64.standard_b64encode(img.data).decode()}})  # fmt: skip
        content.append({"type": "text", "text": user})
        extra = self.request_args(role, model)
        api = self._client.beta.messages if "betas" in extra else self._client.messages
        started = time.monotonic()
        try:
            response = await api.parse(  # type: ignore[call-overload]
                model=model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": content}],  # type: ignore[list-item,misc]
                output_format=schema, **extra,
            )  # fmt: skip
        except anthropic.RateLimitError as exc:
            raise AppError(ErrorCode.PROVIDER_THROTTLED, "The writing service is busy.") from exc
        except anthropic.APITimeoutError as exc:
            raise AppError(ErrorCode.TIMEOUT, "The writing service timed out.") from exc
        except anthropic.APIConnectionError as exc:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The writing service is unreachable.") from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500 or exc.status_code in (408, 409)
            raise AppError(ErrorCode.PROVIDER_ERROR, "The writing service returned an error.",
                           retryable=retryable, meta={"status": exc.status_code}) from exc  # fmt: skip
        except ValidationError as exc:
            raise AppError(
                ErrorCode.PROVIDER_BAD_OUTPUT, "The writing service returned an unusable answer."
            ) from exc
        return self.interpret(response, model=model, started=started)

    def interpret(self, response: Any, *, model: str, started: float) -> tuple[Any, Usage]:
        if response.stop_reason == "refusal":
            raise AppError(ErrorCode.MODERATION_BLOCKED,
                           "This idea can't be turned into a comic. Try a different premise.",
                           retryable=False, meta={"source": "model_refusal"})  # fmt: skip
        parsed = getattr(response, "parsed_output", None)
        if parsed is None or response.stop_reason == "max_tokens":
            raise AppError(
                ErrorCode.PROVIDER_BAD_OUTPUT, "The writing service returned an incomplete answer."
            )
        u = response.usage
        tokens_in = u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0) + (
            getattr(u, "cache_creation_input_tokens", 0) or 0)  # fmt: skip
        served = getattr(response, "model", None) or model
        usage = Usage(provider=self.name, model=served, operation="text", input_tokens=tokens_in,
                      output_tokens=u.output_tokens, latency_ms=int((time.monotonic() - started) * 1000),
                      request_id=getattr(response, "_request_id", None))  # fmt: skip
        return parsed, usage
