"""Claude adapter for story writing, light extraction, moderation and vision QA.

Uses the official SDK's structured-output helper so every stage returns schema-valid
JSON. User text and images are passed as data in the user turn, never in the system
prompt, and no tools are exposed, so prompt content cannot acquire capabilities.
"""

from __future__ import annotations

import base64
import time
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import ValidationError

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import CallContext, ImageInput, T, TextRole, Usage

_MTOK = Decimal(1_000_000)


class AnthropicTextProvider:
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        assert settings.anthropic_api_key is not None
        self._s = settings
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=180.0, max_retries=0
        )  # retries are owned by Temporal so attempts are visible and bounded

    def _model(self, role: TextRole) -> str:
        model = {"story": self._s.text_model_story, "light": self._s.text_model_light,
                 "vision": self._s.vision_model}[role]  # fmt: skip
        assert model is not None  # guaranteed by Settings validation in live mode
        return model

    def _price(self, role: TextRole) -> tuple[Decimal, Decimal]:
        if role == "story":
            return self._s.price_text_story_in_per_mtok, self._s.price_text_story_out_per_mtok
        return self._s.price_text_light_in_per_mtok, self._s.price_text_light_out_per_mtok

    async def structured(
        self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
        images: tuple[ImageInput, ...] = (),
    ) -> tuple[T, Usage]:  # fmt: skip
        model = self._model(role)
        content: list[dict[str, Any]] = []
        for img in images:
            if img.label:
                content.append({"type": "text", "text": img.label})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": img.media_type,
                "data": base64.standard_b64encode(img.data).decode()}})  # fmt: skip
        content.append({"type": "text", "text": user})
        started = time.monotonic()
        try:
            response = await self._client.messages.parse(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": content}],  # type: ignore[list-item,misc,typeddict-item]
                output_format=schema,
                output_config={"effort": "medium" if role == "story" else "low"},
            )
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
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT,
                           "The writing service returned an unusable answer.") from exc  # fmt: skip

        if response.stop_reason == "refusal":
            # A model-level decline is a policy outcome, not an outage: do not retry.
            raise AppError(ErrorCode.MODERATION_BLOCKED,
                           "This idea can't be turned into a comic. Try a different premise.",
                           meta={"source": "model_refusal"})  # fmt: skip
        parsed = response.parsed_output
        if parsed is None or response.stop_reason == "max_tokens":
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT,
                           "The writing service returned an incomplete answer.")  # fmt: skip
        pin, pout = self._price(role)
        u = response.usage
        tokens_in = u.input_tokens + (u.cache_read_input_tokens or 0) + (
            u.cache_creation_input_tokens or 0)  # fmt: skip
        cost = (Decimal(tokens_in) * pin + Decimal(u.output_tokens) * pout) / _MTOK
        usage = Usage(provider=self.name, model=model, operation=f"text_{role}",
                      input_tokens=tokens_in, output_tokens=u.output_tokens, cost_usd=cost,
                      latency_ms=int((time.monotonic() - started) * 1000),
                      request_id=getattr(response, "_request_id", None))  # fmt: skip
        return parsed, usage
