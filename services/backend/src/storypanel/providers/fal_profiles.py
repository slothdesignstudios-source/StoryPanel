"""Request/response contracts for the fal endpoints StoryPanel may call.

Source of truth: the endpoint types fal publishes in its official JavaScript client,
`@fal-ai/client` 1.10.1, file `src/types/endpoints.d.ts` (generated from fal's OpenAPI
specs; sha256 prefix a071f97905b8a106). Every field below appears in that file for the
named endpoint. Facts that file does not state (e.g. the reference-image limit of the
nano-banana-2 edit endpoint) are marked with their weaker source.

Live mode refuses any configured model id that is not listed here, so an operator cannot
point the adapter at an endpoint whose contract nobody has checked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["generate", "edit", "inpaint", "upscale"]
SCHEMA_SOURCE = "@fal-ai/client 1.10.1 src/types/endpoints.d.ts"


@dataclass(frozen=True)
class FalProfile:
    endpoint: str
    roles: frozenset[Role]
    # how to express the frame shape
    aspect_mode: Literal["aspect_ratio", "image_size_wh", "none"]
    aspects: tuple[str, ...] = ()
    max_references: int = 0
    reference_field: str | None = None
    seed_field: str | None = None
    image_field: str | None = None  # single-image input (upscale/inpaint)
    mask_field: str | None = None
    fixed: dict[str, Any] = field(default_factory=dict)
    result: Literal["images0", "image"] = "images0"
    nsfw_flags_field: str | None = None
    min_pixels: int = 0
    max_pixels: int = 0
    notes: str = ""

    def size_args(self, aspect: str) -> dict[str, Any]:
        if self.aspect_mode == "aspect_ratio":
            return {"aspect_ratio": aspect if aspect in self.aspects else "auto"}
        if self.aspect_mode == "image_size_wh":
            w, h = (int(x) for x in aspect.split(":"))
            # aim for the middle of the documented pixel range, multiples of 16
            target = (self.min_pixels + self.max_pixels) / 2
            scale = math.sqrt(target / (w * h))
            width, height = int(w * scale) // 16 * 16, int(h * scale) // 16 * 16
            while width * height < self.min_pixels:
                width, height = width + 16, height + int(16 * h / w)
            return {"image_size": {"width": width, "height": height}}
        return {}


_NB2_ASPECTS = ("21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "4:1", "1:4",
                "8:1", "1:8")  # fmt: skip

PROFILES: dict[str, FalProfile] = {
    p.endpoint: p
    for p in (
        FalProfile(
            "fal-ai/nano-banana-2",
            frozenset({"generate"}),
            "aspect_ratio",
            _NB2_ASPECTS,
            seed_field="seed",
            # resolution fixed at 1K so the per-image price applies (draft quality, spec 7.8)
            fixed={
                "num_images": 1,
                "output_format": "png",
                "resolution": "1K",
                "limit_generations": True,
                # paid add-ons (fal: web search +$0.015, thinking +$0.002 per request) explicitly off
                "enable_web_search": False,
                "thinking_level": "minimal",
            },
            notes="NanoBanana2Input; output NanoBananaOutput.images[].url",
        ),
        FalProfile(
            "fal-ai/nano-banana-2/edit",
            frozenset({"edit"}),
            "aspect_ratio",
            _NB2_ASPECTS,
            # 14: fal model comparison page and RunPod's listing of the same model (not in the
            # schema file). fal recommends few references for stability; we send at most 4.
            max_references=14,
            reference_field="image_urls",
            seed_field="seed",
            fixed={
                "num_images": 1,
                "output_format": "png",
                "resolution": "1K",
                "limit_generations": True,
                # paid add-ons (fal: web search +$0.015, thinking +$0.002 per request) explicitly off
                "enable_web_search": False,
                "thinking_level": "minimal",
            },
            notes="NanoBanana2EditInput; output NanoBananaOutput.images[].url",
        ),
        FalProfile(
            "fal-ai/bytedance/seedream/v5/lite/edit",
            frozenset({"edit"}),
            "image_size_wh",
            max_references=10,
            reference_field="image_urls",  # schema: "up to 10 image inputs"
            fixed={"num_images": 1, "max_images": 1, "enable_safety_checker": True},
            min_pixels=2560 * 1440,
            max_pixels=3072 * 3072,
            notes="SeedDream50LiteEditInput (no seed field); output pulidOutput.images[].url",
        ),
        FalProfile(
            "fal-ai/bytedance/seedream/v5/lite/text-to-image",
            frozenset({"generate"}),
            "image_size_wh",
            fixed={"num_images": 1, "max_images": 1, "enable_safety_checker": True},
            min_pixels=2560 * 1440,
            max_pixels=3072 * 3072,
            notes="SeedDream50LiteT2IInput; output pulidOutput.images[].url",
        ),
        FalProfile(
            "fal-ai/flux-pro/v1/fill",
            frozenset({"inpaint"}),
            "none",
            image_field="image_url",
            mask_field="mask_url",
            seed_field="seed",
            fixed={"num_images": 1, "output_format": "png", "safety_tolerance": "2"},
            nsfw_flags_field="has_nsfw_concepts",
            notes="FluxProFillInput (mask must match image dimensions); output FluxProNewOutput",
        ),
        FalProfile(
            "fal-ai/esrgan",
            frozenset({"upscale"}),
            "none",
            image_field="image_url",
            # anime_6B is the Real-ESRGAN variant trained for flat-shaded line art
            fixed={"model": "RealESRGAN_x4plus_anime_6B", "output_format": "png"},
            result="image",
            notes="esrganInput (scale default 2); output HEDOutput.image.url",
        ),
    )
}


def profile_for(endpoint: str, role: Role) -> FalProfile | None:
    p = PROFILES.get(endpoint)
    return p if p is not None and role in p.roles else None
