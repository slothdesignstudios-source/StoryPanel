"""Builds the provider set for the configured mode. There is no live -> fake fallback."""

from __future__ import annotations

from dataclasses import dataclass

from storypanel.config import Settings, get_settings
from storypanel.providers.base import EmbeddingProvider, ImageProvider, TextProvider, UpscaleProvider


@dataclass(frozen=True)
class Providers:
    text: TextProvider
    image: ImageProvider
    embedding: EmbeddingProvider
    upscale: UpscaleProvider
    mode: str


_providers: Providers | None = None


def build_providers(settings: Settings) -> Providers:
    if settings.provider_mode == "fake":
        from storypanel.providers import fake

        return Providers(fake.FakeTextProvider(), fake.FakeImageProvider(),
                         fake.FakeEmbeddingProvider(settings.embedding_dimensions),
                         fake.FakeUpscaleProvider(), "fake")  # fmt: skip
    from storypanel.providers.anthropic_text import AnthropicTextProvider
    from storypanel.providers.fal_image import FalImageProvider, FalUpscaleProvider
    from storypanel.providers.voyage_embed import VoyageEmbeddingProvider

    return Providers(AnthropicTextProvider(settings), FalImageProvider(settings),
                     VoyageEmbeddingProvider(settings), FalUpscaleProvider(settings), "live")  # fmt: skip


def get_providers() -> Providers:
    global _providers
    if _providers is None:
        _providers = build_providers(get_settings())
    return _providers


def set_providers(providers: Providers | None) -> None:
    global _providers
    _providers = providers
