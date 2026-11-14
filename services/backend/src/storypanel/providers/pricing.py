"""Provider price table with provenance, and conservative cost estimates.

Prices are operator-supplied data (SP_PRICING_FILE or SP_PRICING_JSON), one entry per
(provider, model). Each entry must say where the number came from and when it was last
checked. With live providers:

- a billable call whose model has no entry is refused;
- a price of zero is refused unless the entry is explicitly marked "free": true;
- an entry older than SP_PRICING_MAX_AGE_DAYS is refused (stale);
- a malformed table stops the process at startup.

Fake providers use a built-in *simulated* table with non-zero prices, so budget and
reservation logic is exercised in tests. Customer credits are never derived from these.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from storypanel.config import Settings, get_settings
from storypanel.errors import AppError, ErrorCode

# per_compute_second: billed by the provider's compute time (e.g. fal's ESRGAN endpoint), which
# we cannot observe. Reservations use rate x the request's wait budget; successful requests stay
# "unresolved" at that upper bound until a provider-reported charge is imported.
# per_mtok_multimodal: token-billed image models (OpenAI GPT Image, Gemini image models): separate
# rates for text input, image input, text/thinking output and image output, all per million tokens.
# Image output token counts are not knowable before the call, so the entry also states the
# *assumptions* the reservation uses (`reserve_*`); settlement uses the provider-reported usage.
Unit = Literal["per_image", "per_megapixel", "per_mtok", "per_billion_pixels", "per_compute_second",
               "per_mtok_multimodal"]  # fmt: skip
UNITS: tuple[str, ...] = (
    "per_image",
    "per_megapixel",
    "per_mtok",
    "per_billion_pixels",
    "per_compute_second",
    "per_mtok_multimodal",
)
RESERVE_FIELDS = (
    "reserve_input_tokens_per_image",
    "reserve_output_image_tokens",
    "reserve_output_text_tokens",
)


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    unit: Unit
    usd: Decimal  # per unit (for per_mtok: input tokens)
    output_usd: Decimal  # per_mtok only
    free: bool
    source: str
    verified_at: date
    # per_mtok_multimodal only: image input / image output rates and reservation assumptions
    image_input_usd: Decimal = Decimal(0)
    image_output_usd: Decimal = Decimal(0)
    reserve: tuple[tuple[str, int], ...] = ()

    def ref(self) -> dict[str, Any]:
        out = {"unit": self.unit, "usd": str(self.usd), "output_usd": str(self.output_usd),
               "free": self.free, "source": self.source, "verified_at": self.verified_at.isoformat()}  # fmt: skip
        if self.unit == "per_mtok_multimodal":
            out.update(image_input_usd=str(self.image_input_usd), image_output_usd=str(self.image_output_usd),
                       reserve=dict(self.reserve))  # fmt: skip
        return out

    def assumption(self, name: str) -> int:
        return dict(self.reserve)[name]

    @classmethod
    def from_ref(cls, provider: str, model: str, ref: dict[str, Any]) -> Price:
        """The price an attempt was reserved under. Settlement uses this, not today's table,
        so a resumed request is priced by the model and price that accepted it."""
        return cls(provider, model, ref["unit"], Decimal(ref["usd"]), Decimal(ref.get("output_usd", "0")),
                   bool(ref.get("free")), str(ref.get("source", "")), date.fromisoformat(ref["verified_at"]),
                   Decimal(ref.get("image_input_usd", "0")), Decimal(ref.get("image_output_usd", "0")),
                   tuple(sorted((ref.get("reserve") or {}).items())))  # fmt: skip


class PricingError(ValueError):
    pass


def _dec(value: Any, what: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PricingError(f"{what}: not a number") from exc
    if not d.is_finite() or d < 0:
        raise PricingError(f"{what}: must be a finite, non-negative number")
    return d


def parse_table(raw: dict[str, Any]) -> dict[tuple[str, str], Price]:
    table: dict[tuple[str, str], Price] = {}
    for n, e in enumerate(raw.get("entries", [])):
        where = f"pricing entry {n} ({e.get('provider')}/{e.get('model')})"
        for key in ("provider", "model", "unit", "source", "verified_at"):
            if not e.get(key):
                raise PricingError(f"{where}: '{key}' is required")
        if e["unit"] not in UNITS:
            raise PricingError(f"{where}: unit must be one of {', '.join(UNITS)}")
        try:
            verified = date.fromisoformat(str(e["verified_at"]))
        except ValueError as exc:
            raise PricingError(f"{where}: verified_at must be an ISO date") from exc
        token_unit = e["unit"] in ("per_mtok", "per_mtok_multimodal")
        usd = _dec(e.get("input_usd" if token_unit else "usd"), where)
        out = _dec(e.get("output_usd", 0), where) if token_unit else Decimal(0)
        free = e.get("free") is True
        img_in = img_out = Decimal(0)
        reserve: tuple[tuple[str, int], ...] = ()
        if e["unit"] == "per_mtok_multimodal":
            img_in = _dec(e.get("image_input_usd", e.get("input_usd")), where)
            img_out = _dec(e.get("image_output_usd"), where)
            try:
                reserve = tuple(sorted((k, int(e[k])) for k in RESERVE_FIELDS))
            except (KeyError, TypeError, ValueError) as exc:
                raise PricingError(
                    f"{where}: {', '.join(RESERVE_FIELDS)} are required reservation assumptions"
                ) from exc
            if not free and img_out == 0:
                raise PricingError(f'{where}: image_output_usd is zero; set "free": true if that is intended')
        if not free and (usd == 0 or (e["unit"] == "per_mtok" and out == 0)):
            raise PricingError(f'{where}: price is zero; set "free": true if that is intended')
        ident = (str(e["provider"]), str(e["model"]))
        if ident in table:
            raise PricingError(f"{where}: duplicate entry")
        table[ident] = Price(ident[0], ident[1], e["unit"], usd, out, free, str(e["source"]), verified,
                             img_in, img_out, reserve)  # fmt: skip
    return table


SIMULATED = {
    "entries": [
        {"provider": "fake", "model": m, "unit": u, "usd": p, "input_usd": p, "output_usd": o,
         "source": "simulated (fake providers)", "verified_at": "2099-01-01"}
        for m, u, p, o in (
            ("fake-story", "per_mtok", "2", "10"), ("fake-light", "per_mtok", "0.1", "0.5"),
            ("fake-vision", "per_mtok", "0.1", "0.5"), ("fake-image", "per_image", "0.04", "0"),
            ("fake-edit", "per_image", "0.08", "0"), ("fake-fallback", "per_image", "0.035", "0"),
            ("fake-inpaint", "per_image", "0.05", "0"), ("fake-upscale", "per_image", "0.01", "0"),
            ("fake-embed", "per_billion_pixels", "0.6", "0"),
            ("fake-gemini-qa", "per_mtok", "1.5", "7.5"),
        )
    ] + [
        {"provider": "fake", "model": m, "unit": "per_mtok_multimodal", "input_usd": ti, "image_input_usd": ii,
         "output_usd": to, "image_output_usd": io_, "reserve_input_tokens_per_image": ri,
         "reserve_output_image_tokens": ro, "reserve_output_text_tokens": rt,
         "source": "simulated (fake providers)", "verified_at": "2099-01-01"}
        for m, ti, ii, to, io_, ri, ro, rt in (
            ("fake-openai-image", "5", "8", "10", "30", 2000, 8000, 0),
            ("fake-gemini-image", "0.5", "0.5", "3", "60", 1120, 1120, 4000),
        )
    ]
}  # fmt: skip


@lru_cache
def _load(provider_mode: str, file: str | None, inline: str | None) -> dict[tuple[str, str], Price]:
    if provider_mode == "fake":
        return parse_table(SIMULATED)
    if inline:
        return parse_table(json.loads(inline))
    if file:
        path = Path(file)
        if not path.is_absolute() and not path.exists():
            # relative to the repository root (the parent of the packages catalog), not the cwd
            from storypanel.catalog import catalog_root

            path = catalog_root().parent / file
        return parse_table(json.loads(path.read_text()))
    return {}


def table(settings: Settings | None = None) -> dict[tuple[str, str], Price]:
    s = settings or get_settings()
    return _load(
        s.provider_mode, s.pricing_file, s.pricing_json.get_secret_value() if s.pricing_json else None
    )


def reset_cache() -> None:
    _load.cache_clear()


def configured_models(s: Settings) -> list[tuple[str, str | None]]:
    if s.provider_mode == "fake":
        return []
    """Every model an enabled capability can bill: disabled providers need no price."""
    out: list[tuple[str, str | None]] = [
        ("anthropic", s.text_model_story), ("anthropic", s.text_model_light), ("anthropic", s.vision_model)]  # fmt: skip
    if "openai" in s.image_providers:
        out.append(("openai", s.openai_image_model))
    if "gemini" in s.image_providers:
        out.append(("gemini", s.gemini_image_model))
    if s.identity_qa == "gemini_visual":
        out.append(("gemini", s.gemini_qa_model))
    if s.fal_enabled:
        out += [("fal", s.image_model_generate), ("fal", s.image_model_edit), ("fal", s.image_model_fallback),
                ("fal", s.image_model_fallback_generate), ("fal", s.inpaint_model)]  # fmt: skip
        if s.export_upscale == "fal":
            out.append(("fal", s.upscale_model))
    if s.voyage_enabled and s.identity_qa == "voyage_embedding":
        out.append(("voyage", s.embedding_model))
    return out


def startup_problems(s: Settings) -> list[str]:
    """Problems that must stop a live deployment from starting."""
    if s.provider_mode != "live":
        return []
    try:
        table(s)
    except (PricingError, json.JSONDecodeError, OSError) as exc:
        return [f"pricing table is invalid: {exc}"]
    problems = []
    for provider, model in configured_models(s):
        if model is None:
            continue
        try:
            require(provider, model, settings=s)
        except AppError as exc:
            problems.append(exc.message)
    return problems


def require(provider: str, model: str, *, settings: Settings | None = None) -> Price:
    """The price for a billable call, or a non-retryable refusal."""
    s = settings or get_settings()
    price = table(s).get((provider, model))
    if price is None:
        raise AppError(ErrorCode.PRICING_UNAVAILABLE,
                       f"No verified price is configured for {provider}/{model}.")  # fmt: skip
    age = (datetime.now(UTC).date() - price.verified_at).days
    if s.provider_mode == "live" and age > s.pricing_max_age_days:
        raise AppError(ErrorCode.PRICING_UNAVAILABLE,
                       f"The price for {provider}/{model} was last verified {age} days ago "
                       f"(limit {s.pricing_max_age_days}). Re-check it before spending.")  # fmt: skip
    return price


# ------------------------------------------------------------------ estimates
# Fixed allowance for input the API adds that we cannot see: message framing, the system prompt
# that enables thinking, structured-output support. Not published as a number; an assumption,
# checked against `usage.input_tokens` after every call (the calculated cost replaces the hold).
TEXT_OVERHEAD_TOKENS = 4000
# Claude 4.7+ (high-resolution tier, includes Haiku 5.5): an image costs ceil(w/28) x ceil(h/28)
# visual tokens and is downscaled to at most 4,784 (platform.claude.com/docs/en/build-with-claude/vision).
IMAGE_TOKEN_CAP = 4784


def text_input_bound(*, input_bytes: int, images_wh: list[tuple[int, int]]) -> int:
    """Input-token upper bound used for reservations. Text: one token per UTF-8 byte at most
    (byte-level tokens are at least one byte). Images: the documented visual-token rule."""
    visual = sum(min(math.ceil(w / 28) * math.ceil(h / 28), IMAGE_TOKEN_CAP) for w, h in images_wh)
    return input_bytes + visual + TEXT_OVERHEAD_TOKENS


def text_estimate(
    price: Price, *, input_bytes: int, images_wh: list[tuple[int, int]], max_tokens: int
) -> Decimal:
    """Reservation for one call: the input bound plus the full output cap. `max_tokens` is a
    provider-enforced ceiling on output, thinking included."""
    return text_cost(price, text_input_bound(input_bytes=input_bytes, images_wh=images_wh), max_tokens)


def text_cost(price: Price, input_tokens: int, output_tokens: int) -> Decimal:
    if price.free:
        return Decimal(0)
    return (Decimal(input_tokens) * price.usd + Decimal(output_tokens) * price.output_usd) / Decimal(
        1_000_000
    )


def image_cost(
    price: Price, *, images: int = 1, pixels: int = 1_048_576, seconds: float | None = None
) -> Decimal:
    if price.free:
        return Decimal(0)
    if price.unit == "per_compute_second":
        if seconds is None:
            raise AppError(ErrorCode.PRICING_UNAVAILABLE, f"{price.model} is billed by compute time; "
                           "a time budget is needed to estimate it.")  # fmt: skip
        return price.usd * Decimal(str(seconds)) * images
    if price.unit == "per_image":
        return price.usd * images
    if price.unit == "per_megapixel":
        return price.usd * Decimal(pixels * images) / Decimal(1_000_000)
    if price.unit == "per_billion_pixels":
        return price.usd * Decimal(pixels * images) / Decimal(1_000_000_000)
    raise AppError(ErrorCode.PRICING_UNAVAILABLE, f"Unit {price.unit} cannot price an image call.")


# ------------------------------------------------------------------ token-billed image models
@dataclass(frozen=True)
class ImageUsage:
    """Usage a token-billed image model reported for one call (None fields: not reported)."""

    input_text_tokens: int | None = None
    input_image_tokens: int | None = None
    output_text_tokens: int | None = None  # includes thinking where the provider bills it as output
    output_image_tokens: int | None = None

    @property
    def complete(self) -> bool:
        return None not in (self.input_text_tokens, self.input_image_tokens, self.output_text_tokens,
                            self.output_image_tokens)  # fmt: skip

    def as_dict(self) -> dict[str, int | None]:
        return {"input_text_tokens": self.input_text_tokens, "input_image_tokens": self.input_image_tokens,
                "output_text_tokens": self.output_text_tokens, "output_image_tokens": self.output_image_tokens}  # fmt: skip


def multimodal_cost(price: Price, u: ImageUsage) -> Decimal | None:
    """Usage x operator price table; None when the provider did not report every component."""
    if price.free:
        return Decimal(0)
    if price.unit != "per_mtok_multimodal" or not u.complete:
        return None
    assert u.input_text_tokens is not None and u.input_image_tokens is not None
    assert u.output_text_tokens is not None and u.output_image_tokens is not None
    return (Decimal(u.input_text_tokens) * price.usd + Decimal(u.input_image_tokens) * price.image_input_usd
            + Decimal(u.output_text_tokens) * price.output_usd
            + Decimal(u.output_image_tokens) * price.image_output_usd) / Decimal(1_000_000)  # fmt: skip


def image_reservation(price: Price, *, prompt_bytes: int, input_images: int) -> Decimal:
    """Hold for one token-billed image call: the prompt (one token per UTF-8 byte at most, plus
    the text overhead), every input image at the entry's assumed token count, and the assumed
    image + text/thinking output. These are ASSUMPTIONS from the price entry, not provider limits:
    neither provider lets a caller cap image output tokens. The settled cost replaces the hold."""
    if price.free:
        return Decimal(0)
    if price.unit != "per_mtok_multimodal":
        raise AppError(ErrorCode.PRICING_UNAVAILABLE, f"{price.model} is not priced per token for images.")
    u = ImageUsage(input_text_tokens=prompt_bytes + TEXT_OVERHEAD_TOKENS,
                   input_image_tokens=input_images * price.assumption("reserve_input_tokens_per_image"),
                   output_text_tokens=price.assumption("reserve_output_text_tokens"),
                   output_image_tokens=price.assumption("reserve_output_image_tokens"))  # fmt: skip
    cost = multimodal_cost(price, u)
    assert cost is not None
    return cost
