"""Voyage multimodal embeddings for the identity-similarity check."""

from __future__ import annotations

import io

import voyageai
import voyageai.error as verr
from PIL import Image

from storypanel.config import Settings
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import CallContext


class VoyageEmbeddingProvider:
    name = "voyage"

    def __init__(self, settings: Settings) -> None:
        assert settings.voyage_api_key is not None and settings.embedding_model is not None
        self.model = settings.embedding_model
        self._dims = settings.embedding_dimensions
        self._client = voyageai.AsyncClient(
            api_key=settings.voyage_api_key.get_secret_value(), max_retries=0, timeout=60
        )

    async def embed_images(self, images: list[bytes], ctx: CallContext) -> list[list[float]]:
        inputs = [[Image.open(io.BytesIO(b)).convert("RGB")] for b in images]
        try:
            result = await self._client.multimodal_embed(inputs=inputs, model=self.model)
        except verr.RateLimitError as exc:
            raise AppError(ErrorCode.PROVIDER_THROTTLED, "The similarity service is busy.") from exc
        except (verr.InvalidRequestError, verr.AuthenticationError) as exc:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The similarity service rejected the request.",
                           retryable=False) from exc  # fmt: skip
        except verr.VoyageError as exc:
            raise AppError(ErrorCode.PROVIDER_ERROR, "The similarity service failed.") from exc
        vectors = [list(map(float, v)) for v in result.embeddings]
        if any(len(v) != self._dims for v in vectors):
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT,
                           "Embedding size does not match SP_EMBEDDING_DIMENSIONS.",
                           retryable=False)  # fmt: skip
        return vectors
