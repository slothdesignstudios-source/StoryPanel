"""Runtime configuration, validated at startup.

Everything that the product document only *estimates* (model names, provider prices,
plan entitlements, credit prices) lives here as configuration rather than in code, so it
can be corrected without a deploy once verified against the provider.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
ProviderMode = Literal["fake", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SP_", env_file=".env", extra="ignore")

    # --- service identity
    env: Environment = "local"
    release: str = "dev"
    service_name: str = "storypanel-api"
    log_level: str = "INFO"

    # --- data stores
    database_url: str = "postgresql+asyncpg://storypanel@127.0.0.1:5432/storypanel"
    redis_url: str = "redis://127.0.0.1:6379/0"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "storypanel-assets"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    signed_url_ttl_seconds: int = 600

    # --- temporal
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "storypanel"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False

    # --- auth (Clerk in deployed environments; dev tokens only in local/test)
    auth_mode: Literal["clerk", "dev"] = "dev"
    clerk_issuer: str | None = None
    clerk_jwks_url: str | None = None
    clerk_authorized_parties: list[str] = Field(default_factory=list)
    clerk_webhook_secret: SecretStr | None = None
    dev_auth_secret: SecretStr = SecretStr("local-dev-only-secret-change-me-32bytes")

    # --- http security
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_request_bytes: int = 256 * 1024
    rate_limit_per_minute: int = 120
    rate_limit_generation_per_hour: int = 30
    pseudonym_salt: SecretStr = SecretStr("local-salt")

    # --- providers
    provider_mode: ProviderMode = "fake"
    anthropic_api_key: SecretStr | None = None
    # Model identifiers are configuration. Defaults are None so a live deployment must
    # state them explicitly after checking them against the provider's model list.
    text_model_story: str | None = None
    text_model_light: str | None = None
    vision_model: str | None = None
    fal_key: SecretStr | None = None
    image_model_generate: str | None = None
    image_model_edit: str | None = None
    image_model_fallback: str | None = None
    image_max_references: int = 4
    upscale_model: str | None = None
    upscale_factor: int = 2
    voyage_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dimensions: int = 1024
    provider_concurrency_text: int = 8
    provider_concurrency_image: int = 6
    provider_concurrency_vision: int = 8

    # Provider prices in USD, used for cost accounting only. Unverified estimates must be
    # replaced with the provider's published rates before budgets are trusted.
    price_text_story_in_per_mtok: Decimal = Decimal("0")
    price_text_story_out_per_mtok: Decimal = Decimal("0")
    price_text_light_in_per_mtok: Decimal = Decimal("0")
    price_text_light_out_per_mtok: Decimal = Decimal("0")
    price_image_generate: Decimal = Decimal("0")
    price_image_edit: Decimal = Decimal("0")
    price_upscale: Decimal = Decimal("0")
    price_embedding_per_image: Decimal = Decimal("0")

    # --- product limits (document constraints; see docs/requirements-matrix.md)
    default_pages: int = 4
    max_pages: int = 8
    max_characters: int = 4
    max_characters_per_panel: int = 3
    max_words_per_balloon: int = 25
    max_balloons_per_panel: int = 3
    max_script_repairs: int = 2
    max_panel_qa_retries: int = 2
    identity_similarity_threshold: float = 0.55  # uncalibrated default; tune on golden set
    approval_ttl_days: int = 14
    answers_ttl_days: int = 7
    event_retention_days: int = 14
    export_retention_days: int = 30
    deleted_project_purge_days: int = 7

    # --- credits (customer-facing units, deliberately separate from provider cost)
    credits_cast: int = 6
    credits_script: int = 2
    credits_per_panel: int = 1
    credits_regenerate: int = 1
    credits_edit: int = 1
    credits_character_revision: int = 1
    estimated_panels_per_page: int = 5
    plan_monthly_credits: dict[str, int] = Field(
        default_factory=lambda: {"free": 12, "creator": 80, "pro": 240}
    )
    plan_presets: dict[str, int] = Field(default_factory=lambda: {"free": 2, "creator": 4, "pro": 4})
    signup_bonus_credits: int = 12

    # --- spend limits in provider USD
    spend_limit_user_daily_usd: Decimal = Decimal("10")
    spend_limit_project_usd: Decimal = Decimal("8")
    spend_limit_global_daily_usd: Decimal = Decimal("500")

    # --- stripe
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_price_creator: str | None = None
    stripe_price_pro: str | None = None
    stripe_price_credit_pack: str | None = None
    credit_pack_size: int = 40
    web_base_url: str = "http://localhost:3000"

    # --- observability
    otlp_endpoint: str | None = None
    trace_sample_ratio: float = 1.0
    sentry_dsn: SecretStr | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None
    log_prompts: bool = False  # raw user prompts stay out of logs/traces unless enabled
    metrics_port: int = 9464

    @property
    def is_deployed(self) -> bool:
        return self.env in ("staging", "production")

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []
        if self.default_pages > self.max_pages or self.max_pages > 8:
            problems.append("max_pages must be <= 8 and >= default_pages (MVP scope)")
        if self.is_deployed:
            if self.provider_mode != "live":
                problems.append("fake providers are not permitted in staging/production")
            if self.auth_mode != "clerk":
                problems.append("dev auth is not permitted in staging/production")
            if self.pseudonym_salt.get_secret_value() == "local-salt":
                problems.append("SP_PSEUDONYM_SALT must be set")
            if "*" in self.cors_origins:
                problems.append("wildcard CORS origin is not permitted")
            for name in ("stripe_secret_key", "stripe_webhook_secret", "s3_access_key_id"):
                if getattr(self, name) is None and name.startswith("stripe"):
                    problems.append(f"SP_{name.upper()} must be set")
        if self.auth_mode == "clerk" and not (self.clerk_issuer and self.clerk_jwks_url):
            problems.append("clerk auth requires SP_CLERK_ISSUER and SP_CLERK_JWKS_URL")
        if self.provider_mode == "live":
            required = (
                "anthropic_api_key", "text_model_story", "text_model_light", "vision_model",
                "fal_key", "image_model_generate", "image_model_edit", "upscale_model",
                "voyage_api_key", "embedding_model",
            )  # fmt: skip
            problems += [f"SP_{n.upper()} must be set for live providers" for n in required
                         if getattr(self, n) is None]  # fmt: skip
        if problems:
            raise ValueError("invalid configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
