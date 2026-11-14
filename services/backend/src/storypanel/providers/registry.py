"""Builds the provider set for the configured mode. There is no live -> fake fallback.

Only enabled capabilities are constructed. With SP_FAL_ENABLED / SP_VOYAGE_ENABLED false (the
default) no fal or Voyage client exists, so nothing can call those services: pipeline code checks
for `None` and uses the configured replacement (OpenAI / Gemini images, Gemini visual identity QA,
local export resampling).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from storypanel.config import Settings, get_settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import (
    EmbeddingProvider,
    ImageProvider,
    SyncImageProvider,
    TextProvider,
    UpscaleProvider,
)  # fmt: skip


@dataclass(frozen=True)
class Providers:
    text: TextProvider
    image: ImageProvider | None  # fal-style queue provider (fal adapter); None when fal is disabled
    embedding: EmbeddingProvider | None  # Voyage; None when disabled
    upscale: UpscaleProvider | None  # fal ESRGAN; None unless SP_EXPORT_UPSCALE=fal
    mode: str
    drawers: dict[str, SyncImageProvider] = field(default_factory=dict)  # "openai" / "gemini"
    identity: TextProvider | None = None  # Gemini visual identity QA

    def drawer(self, choice: str) -> SyncImageProvider:
        d = self.drawers.get(choice)
        if d is None:
            raise AppError(ErrorCode.INVALID_STATE, f"The image provider '{choice}' is not enabled here.",
                           retryable=False, meta={"provider": choice})  # fmt: skip
        return d


_providers: Providers | None = None


def build_providers(settings: Settings) -> Providers:
    if settings.provider_mode == "fake":
        from storypanel.providers import fake

        return Providers(
            fake.FakeTextProvider(), fake.FakeImageProvider(),
            fake.FakeEmbeddingProvider(settings.embedding_dimensions) if settings.voyage_enabled else None,
            fake.FakeUpscaleProvider() if settings.export_upscale == "fal" else None, "fake",
            drawers={c: fake.FakeSyncImageProvider(c, settings.image_max_references + 1)
                     for c in settings.image_providers if c in ("openai", "gemini")},
            identity=fake.FakeIdentityProvider() if settings.identity_qa == "gemini_visual" else None)  # fmt: skip
    from storypanel.providers.anthropic_text import AnthropicTextProvider

    drawers: dict[str, SyncImageProvider] = {}
    if "openai" in settings.image_providers:
        from storypanel.providers.openai_image import OpenAIImageProvider

        drawers["openai"] = OpenAIImageProvider(settings)
    if "gemini" in settings.image_providers:
        from storypanel.providers.gemini import GeminiImageProvider

        drawers["gemini"] = GeminiImageProvider(settings)
    identity = None
    if settings.identity_qa == "gemini_visual":
        from storypanel.providers.gemini import GeminiVisionProvider

        identity = GeminiVisionProvider(settings)
    image = upscale = embedding = None
    if settings.fal_enabled:
        from storypanel.providers.fal_image import FalImageProvider, FalUpscaleProvider

        if "fal" in settings.image_providers or settings.repair_mode == "inpaint":
            image = FalImageProvider(settings)
        if settings.export_upscale == "fal":
            upscale = FalUpscaleProvider(settings)
    if settings.voyage_enabled and settings.identity_qa == "voyage_embedding":
        from storypanel.providers.voyage_embed import VoyageEmbeddingProvider

        embedding = VoyageEmbeddingProvider(settings)
    return Providers(AnthropicTextProvider(settings), image, embedding, upscale, "live", drawers=drawers,
                     identity=identity)  # fmt: skip


def get_providers() -> Providers:
    global _providers
    if _providers is None:
        _providers = build_providers(get_settings())
    return _providers


def set_providers(providers: Providers | None) -> None:
    global _providers
    _providers = providers
