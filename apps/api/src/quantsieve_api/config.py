from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="QUANTSIEVE_",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "QuantSieve API"
    build_version: str = "0.1.0"
    source_revision: Annotated[
        str | None,
        Field(
            default=None,
            min_length=7,
            max_length=64,
            pattern=r"^[0-9a-f]+$",
        ),
    ] = None
    dependency_lock_sha256: Annotated[
        str | None,
        Field(default=None, pattern=r"^[0-9a-f]{64}$"),
    ] = None
    environment: str = "development"
    database_path: str = "./data/quantsieve.db"
    cache_path: str = "./data/cache.db"
    factor_research_max_concurrency: Annotated[
        int,
        Field(strict=True, ge=1, le=8),
    ] = 1
    factor_research_provider_max_concurrency: Annotated[
        int,
        Field(strict=True, ge=1, le=32),
    ] = 8
    factor_research_deadline_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=300.0, allow_inf_nan=False),
    ] = 45.0
    sec_user_agent: str = "QuantSieve local-user contact@example.com"
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    llm_api_key: str | None = None
    x_bearer_token: str | None = None
    x_api_base_url: str = "https://api.x.com/2"
    translation_enabled: bool = False
    translation_base_url: str = "http://translate:5000"
    translation_timeout_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=120.0, allow_inf_nan=False),
    ] = 45.0
    translation_batch_limit: Annotated[
        int,
        Field(strict=True, ge=1, le=50),
    ] = 18
    translation_cache_ttl_days: Annotated[
        int,
        Field(strict=True, ge=1, le=365),
    ] = 90
    translation_contract_version: str = "argos-en-zh-v1"
    monitor_scheduler_enabled: bool = False
    monitor_poll_seconds: int = 60
    paper_scheduler_enabled: bool = True
    paper_poll_seconds: int = 60
    portfolio_opening_scheduler_enabled: Annotated[
        bool,
        Field(strict=True),
    ] = False
    portfolio_opening_poll_seconds: Annotated[
        float,
        Field(strict=True, ge=1.0, le=3600.0, allow_inf_nan=False),
    ] = 5.0
    portfolio_opening_quote_deadline_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=3595.0, allow_inf_nan=False),
    ] = 4.0
    portfolio_opening_lease_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=3600.0, allow_inf_nan=False),
    ] = 15.0
    portfolio_settlement_scheduler_enabled: Annotated[
        bool,
        Field(strict=True),
    ] = False
    portfolio_settlement_poll_seconds: Annotated[
        float,
        Field(strict=True, ge=1.0, le=3600.0, allow_inf_nan=False),
    ] = 60.0
    portfolio_settlement_history_deadline_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=3595.0, allow_inf_nan=False),
    ] = 20.0
    portfolio_settlement_lease_seconds: Annotated[
        float,
        Field(strict=True, gt=0.0, le=3600.0, allow_inf_nan=False),
    ] = 30.0

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("portfolio_opening_scheduler_enabled", mode="before")
    @classmethod
    def parse_portfolio_opening_enabled(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return value

    @field_validator(
        "translation_timeout_seconds",
        "factor_research_deadline_seconds",
        "portfolio_opening_poll_seconds",
        "portfolio_opening_quote_deadline_seconds",
        "portfolio_opening_lease_seconds",
        mode="before",
    )
    @classmethod
    def parse_timeout_seconds(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value

    @field_validator(
        "translation_batch_limit",
        "translation_cache_ttl_days",
        "factor_research_max_concurrency",
        "factor_research_provider_max_concurrency",
        mode="before",
    )
    @classmethod
    def parse_translation_integer(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return value
        return value

    @field_validator("portfolio_settlement_scheduler_enabled", mode="before")
    @classmethod
    def parse_portfolio_settlement_enabled(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return value

    @field_validator(
        "portfolio_settlement_poll_seconds",
        "portfolio_settlement_history_deadline_seconds",
        "portfolio_settlement_lease_seconds",
        mode="before",
    )
    @classmethod
    def parse_portfolio_settlement_seconds(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return value
        return value

    @model_validator(mode="after")
    def require_production_build_version(self) -> Self:
        if (
            self.environment.strip().lower() == "production"
            and self.build_version.strip() in {"", "0.1.0"}
        ):
            raise ValueError(
                "Production requires QUANTSIEVE_BUILD_VERSION to identify "
                "the deployed build."
            )
        return self

    @model_validator(mode="after")
    def validate_portfolio_opening_timings(self) -> Self:
        minimum_lease_seconds = (
            self.portfolio_opening_quote_deadline_seconds + 5.0
        )
        if self.portfolio_opening_lease_seconds < minimum_lease_seconds:
            raise ValueError(
                "Portfolio opening lease must leave at least five seconds "
                "beyond its quote deadline."
            )
        return self

    @model_validator(mode="after")
    def validate_portfolio_settlement_rollout(self) -> Self:
        minimum_lease_seconds = (
            self.portfolio_settlement_history_deadline_seconds + 5.0
        )
        if self.portfolio_settlement_lease_seconds < minimum_lease_seconds:
            raise ValueError(
                "Portfolio settlement lease must leave at least five seconds "
                "beyond its history deadline."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
