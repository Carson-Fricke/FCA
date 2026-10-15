"""Typed market-lake settings loaded from non-secret YAML and the environment."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from lake.configuration.loader import (
    load_yaml_mapping,
    market_lake_config_dir,
    validate_yaml_settings,
)

PositiveInteger = Annotated[int, Field(gt=0)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_ignore_empty=False,
        env_nested_delimiter="__",
        env_prefix="LAKE_",
        extra="forbid",
    )


class RetrySettings(_StrictModel):
    max_attempts: Annotated[int, Field(ge=1, le=20)]
    honor_retry_after: bool


class ProviderRequestSettings(_StrictModel):
    # Ceiling for the adaptive per-key in-flight bound (Rust lake), matching
    # the Rust settings model: the controller ramps toward it and backs off.
    max_concurrency: Annotated[int, Field(ge=1, le=1024)]
    timeout_seconds: Annotated[int, Field(gt=0, le=3600)]
    retry: RetrySettings


class ProviderSettings(_StrictModel):
    name: Literal["alpaca"]
    request: ProviderRequestSettings
    # These fields have no YAML defaults and can only be populated at runtime.
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_secret: SecretStr | None = Field(default=None, repr=False)

    @field_validator("api_key", "api_secret", mode="before")
    @classmethod
    def _require_non_empty_secret(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            if not value.get_secret_value().strip():
                raise ValueError("runtime secrets cannot be empty")
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runtime secrets must be non-empty strings")
        return value


class ParquetSettings(_StrictModel):
    compression: Literal["zstd", "snappy", "gzip", "brotli", "lz4", "uncompressed"]
    file_granularity: Literal["symbol_session_data_type"]


class LifecycleSettings(_StrictModel):
    enabled: bool
    cool_after_days: Annotated[int, Field(ge=0)]
    cold: ColdStorageSettings


class ColdStorageSettings(_StrictModel):
    enabled: bool
    minimum_age_days: PositiveInteger


class StorageSettings(_StrictModel):
    parquet: ParquetSettings
    capacity_bytes: PositiveInteger
    pause_enqueue_at_capacity: bool
    pause_ingest_at_capacity: bool
    lifecycle: LifecycleSettings


class BackfillSettings(_StrictModel):
    max_replicas: PositiveInteger
    order: Literal["newest_first"]
    liquidity_rank_tiebreaker: bool
    asset_priority: tuple[
        Literal[
            "equity_catch_up",
            "equity_backfill",
            "options_backfill",
            "crypto_backfill",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def _unique_asset_priority(self) -> BackfillSettings:
        if len(set(self.asset_priority)) != len(self.asset_priority):
            raise ValueError(
                "ingest.backfill.asset_priority must not contain duplicates"
            )
        return self


class IngestSettings(_StrictModel):
    backfill: BackfillSettings


class SasSettings(_StrictModel):
    ttl_seconds: Annotated[int, Field(gt=0, le=86_400)]
    read_only: Literal[True]
    object_scoped: Literal[True]


class SqlSettings(_StrictModel):
    max_scan_bytes: PositiveInteger
    max_result_bytes: PositiveInteger
    authorized_force_override_default: bool

    @model_validator(mode="after")
    def _result_must_fit_scan(self) -> SqlSettings:
        if self.max_result_bytes > self.max_scan_bytes:
            raise ValueError("api.sql.max_result_bytes cannot exceed max_scan_bytes")
        return self


class AuthSettings(_StrictModel):
    """Non-secret Entra configuration for the lake API authentication boundary.

    Both ``entra_tenant_id`` and ``entra_audience`` must be present to enable
    Entra managed-identity JWT validation; otherwise the API stays on the
    static bearer token only. These values are configuration, never secrets.
    """

    entra_tenant_id: str | None = None
    entra_audience: str | None = None
    entra_role: str = "Lake.Access"

    @field_validator("entra_tenant_id", "entra_audience", mode="before")
    @classmethod
    def _blank_values_are_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("entra_role", mode="before")
    @classmethod
    def _require_non_empty_role(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("entra_role must be a non-empty string")
        return value


class ApiSettings(_StrictModel):
    sas: SasSettings
    sql: SqlSettings
    auth: AuthSettings = Field(default_factory=AuthSettings)
    # No YAML default; deployment may provide a plain secret or Key Vault ref.
    bearer_token: SecretStr | None = Field(default=None, repr=False)

    @field_validator("bearer_token", mode="before")
    @classmethod
    def _require_non_empty_bearer(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            if not value.get_secret_value().strip():
                raise ValueError("runtime secrets cannot be empty")
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runtime secrets must be non-empty strings")
        return value


class OperationsSettings(_StrictModel):
    monthly_budget_alerts_usd: tuple[PositiveInteger, ...]
    log_retention_days: PositiveInteger
    daily_coverage_report: bool


class LakeSettings(_EnvironmentSettings):
    """Complete typed representation of ``settings.yaml``."""

    schema_version: Literal[1]
    provider: ProviderSettings
    storage: StorageSettings
    ingest: IngestSettings
    api: ApiSettings
    operations: OperationsSettings


def load_settings(
    root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> LakeSettings:
    """Load and validate settings defaults with environment precedence.

    ``root`` is a repository root by default. It may also point directly to a
    ``config/market-lake`` directory, which keeps callers independent of the
    process working directory.
    """
    path = market_lake_config_dir(root) / "settings.yaml"
    defaults = load_yaml_mapping(path)
    return validate_yaml_settings(LakeSettings, defaults, environ)
