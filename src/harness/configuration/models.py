"""Strict, deterministic configuration models for harness execution shells."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from harness.core.symbols import Symbol

type ConfigurationValue = (
    None
    | bool
    | int
    | Decimal
    | str
    | tuple[ConfigurationValue, ...]
    | dict[str, ConfigurationValue]
)


def _yaml_tuple(value: object) -> object:
    """Freeze YAML sequences without otherwise coercing their entries."""
    return tuple(value) if isinstance(value, list) else value


def _configuration_mapping(value: object) -> object:
    if not isinstance(value, dict):
        return value

    def freeze(item: object, path: str) -> ConfigurationValue:
        if isinstance(item, float):
            raise ValueError(f"{path} must use an exact decimal, not a float")
        if isinstance(item, list | tuple):
            return tuple(freeze(entry, f"{path}[]") for entry in item)
        if isinstance(item, dict):
            result: dict[str, ConfigurationValue] = {}
            for key, entry in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} keys must be strings")
                result[key] = freeze(entry, f"{path}.{key}")
            return result
        if item is None or isinstance(item, bool | int | Decimal | str):
            return item
        raise ValueError(f"{path} contains unsupported {type(item).__name__}")

    return freeze(value, "configuration")


def _utc_datetime(value: object) -> object:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("must be an ISO 8601 datetime") from error
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a UTC offset")
        return value.astimezone(UTC)
    return value


def _canonical_symbols(value: object) -> object:
    if not isinstance(value, list | tuple):
        return value
    symbols = tuple(sorted(str(Symbol(item)) for item in value))
    if len(symbols) != len(set(symbols)):
        raise ValueError("symbols must not contain duplicates")
    if not symbols:
        raise ValueError("symbols must not be empty")
    return symbols


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
EnvironmentReference = Annotated[
    str,
    StringConstraints(pattern=r"^\$\{[A-Z_][A-Z0-9_]*\}$"),
]
ExactNonNegativeDecimal = Annotated[
    Decimal,
    Field(ge=Decimal("0"), allow_inf_nan=False),
]
ExactPositiveDecimal = Annotated[
    Decimal,
    Field(gt=Decimal("0"), allow_inf_nan=False),
]
Fraction = Annotated[
    Decimal,
    Field(gt=Decimal("0"), le=Decimal("1"), allow_inf_nan=False),
]
PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]


class StrictConfigurationModel(BaseModel):
    """Base for configuration that rejects coercion and undeclared fields."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class StrategyInstanceConfig(StrictConfigurationModel):
    """One immutable strategy implementation and its deterministic parameters."""

    instance_id: Identifier
    implementation: NonEmptyText
    bundle_hash: Sha256Digest
    capital_fraction: Fraction
    symbols: Annotated[tuple[str, ...], BeforeValidator(_canonical_symbols)]
    parameters: dict[str, ConfigurationValue] = Field(default_factory=dict)

    _freeze_parameters = field_validator("parameters", mode="before")(
        _configuration_mapping
    )


class RiskRuleConfig(StrictConfigurationModel):
    """An ordered risk rule and its typed, canonical parameter tree."""

    rule_id: Identifier
    implementation: NonEmptyText
    parameters: dict[str, ConfigurationValue] = Field(default_factory=dict)

    _freeze_parameters = field_validator("parameters", mode="before")(
        _configuration_mapping
    )


class RiskPipelineConfig(StrictConfigurationModel):
    """Ordered pre-trade risk rules with an explicit rejection policy."""

    rules: Annotated[
        tuple[RiskRuleConfig, ...], BeforeValidator(_yaml_tuple), Field(min_length=1)
    ]
    on_reject: Literal["reject_order", "halt_strategy", "halt_runtime"]

    @model_validator(mode="after")
    def _rule_ids_are_unique(self) -> RiskPipelineConfig:
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("risk rule_id values must be unique")
        return self


class FeeRuleConfig(StrictConfigurationModel):
    """One exact-decimal fee rule evaluated in configured order."""

    fee_id: Identifier
    basis: Literal["flat", "per_unit", "notional_bps"]
    rate: ExactNonNegativeDecimal
    minimum: ExactNonNegativeDecimal
    side: Literal["buy", "sell", "both"]


class FeeConfig(StrictConfigurationModel):
    """Deterministic fee schedule shared by simulation and trader shells."""

    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    rounding: Literal["half_even"]
    rules: Annotated[
        tuple[FeeRuleConfig, ...], BeforeValidator(_yaml_tuple), Field(min_length=1)
    ]

    @model_validator(mode="after")
    def _fee_ids_are_unique(self) -> FeeConfig:
        fee_ids = [rule.fee_id for rule in self.rules]
        if len(fee_ids) != len(set(fee_ids)):
            raise ValueError("fee_id values must be unique")
        return self


class ResourceLimitsConfig(StrictConfigurationModel):
    """Bounded resources declared independently of a host machine."""

    cpu_cores: ExactPositiveDecimal
    memory_mib: PositiveInteger


class DeploymentConfig(StrictConfigurationModel):
    """Portable process deployment settings with no embedded credentials."""

    name: Identifier
    target: Literal["local", "azure_container_app", "azure_container_job"]
    environment: Literal["local", "development", "staging", "production"]
    image: NonEmptyText | None
    replicas: Annotated[int, Field(ge=0, le=100)]
    restart_policy: Literal["never", "on_failure", "always"]
    network_access: Literal["disabled", "runtime_only"]
    resources: ResourceLimitsConfig

    @field_validator("image")
    @classmethod
    def _image_is_immutable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("image must use an immutable sha256 digest")
        return value

    @model_validator(mode="after")
    def _remote_deployment_has_image(self) -> DeploymentConfig:
        if self.target != "local" and self.image is None:
            raise ValueError("non-local deployments require an immutable image")
        return self


class DataSnapshotConfig(StrictConfigurationModel):
    """Point-in-time data identity required by deterministic backtests."""

    snapshot_id: NonEmptyText
    fingerprint: Sha256Digest
    symbols: Annotated[tuple[str, ...], BeforeValidator(_canonical_symbols)]
    bar_interval: Annotated[
        str, StringConstraints(pattern=r"^[1-9][0-9]*(?:s|min|h|d)$")
    ]


class RuntimeAdapterConfig(StrictConfigurationModel):
    """Runtime-only adapter settings and environment credential references."""

    adapter: NonEmptyText
    credentials: dict[Identifier, EnvironmentReference] = Field(default_factory=dict)
    settings: dict[str, ConfigurationValue] = Field(default_factory=dict)

    _freeze_settings = field_validator("settings", mode="before")(
        _configuration_mapping
    )


class ApiServerConfig(StrictConfigurationModel):
    """HTTP serving settings for the API shell."""

    host: NonEmptyText
    port: Annotated[int, Field(ge=1, le=65_535)]
    workers: PositiveInteger
    cors_origins: Annotated[tuple[str, ...], BeforeValidator(_yaml_tuple)]


class ApiAuthConfig(StrictConfigurationModel):
    """API authentication whose value exists only in the runtime environment."""

    bearer_token: EnvironmentReference


class ApiConfig(StrictConfigurationModel):
    """Complete configuration document for the ``api`` execution shell."""

    schema_version: Literal[1]
    shell: Literal["api"]
    server: ApiServerConfig
    auth: ApiAuthConfig
    deployment: DeploymentConfig


class BacktestConfig(StrictConfigurationModel):
    """Complete deterministic configuration for one backtest job."""

    schema_version: Literal[1]
    shell: Literal["backtest"]
    run_id: Identifier
    start: Annotated[datetime, BeforeValidator(_utc_datetime)]
    end: Annotated[datetime, BeforeValidator(_utc_datetime)]
    clock: Literal["event_time"]
    event_ordering: Literal["timestamp_then_sequence_then_class_then_feed"]
    seed: NonNegativeInteger
    base_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    initial_cash: ExactPositiveDecimal
    data: DataSnapshotConfig
    strategy_instances: Annotated[
        tuple[StrategyInstanceConfig, ...],
        BeforeValidator(_yaml_tuple),
        Field(min_length=1),
    ]
    risk_pipeline: RiskPipelineConfig
    fees: FeeConfig
    deployment: DeploymentConfig

    @model_validator(mode="after")
    def _deterministic_run_constraints(self) -> BacktestConfig:
        if self.start >= self.end:
            raise ValueError("backtest start must be before end")
        instance_ids = [strategy.instance_id for strategy in self.strategy_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("strategy instance_id values must be unique")
        if sum(
            (strategy.capital_fraction for strategy in self.strategy_instances),
            start=Decimal("0"),
        ) > Decimal("1"):
            raise ValueError("strategy capital fractions must not exceed one")
        if self.deployment.network_access != "disabled":
            raise ValueError("backtest deployments must disable network access")
        if self.deployment.replicas != 1:
            raise ValueError("backtest deployments must use exactly one replica")
        return self


class TraderConfig(StrictConfigurationModel):
    """Complete configuration for paper, shadow, or live trader execution."""

    schema_version: Literal[1]
    shell: Literal["trader"]
    mode: Literal["paper", "shadow", "live"]
    seed: NonNegativeInteger
    base_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    strategy_instances: Annotated[
        tuple[StrategyInstanceConfig, ...],
        BeforeValidator(_yaml_tuple),
        Field(min_length=1),
    ]
    risk_pipeline: RiskPipelineConfig
    fees: FeeConfig
    market_data: RuntimeAdapterConfig
    broker: RuntimeAdapterConfig
    deployment: DeploymentConfig

    @model_validator(mode="after")
    def _instance_ids_are_unique(self) -> TraderConfig:
        instance_ids = [strategy.instance_id for strategy in self.strategy_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("strategy instance_id values must be unique")
        if sum(
            (strategy.capital_fraction for strategy in self.strategy_instances),
            start=Decimal("0"),
        ) > Decimal("1"):
            raise ValueError("strategy capital fractions must not exceed one")
        return self


ApiConfiguration = ApiConfig
BacktestConfiguration = BacktestConfig
TraderConfiguration = TraderConfig

type HarnessConfig = Annotated[
    ApiConfig | BacktestConfig | TraderConfig,
    Field(discriminator="shell"),
]


__all__ = [
    "ApiAuthConfig",
    "ApiConfig",
    "ApiConfiguration",
    "ApiServerConfig",
    "BacktestConfig",
    "BacktestConfiguration",
    "ConfigurationValue",
    "DataSnapshotConfig",
    "DeploymentConfig",
    "EnvironmentReference",
    "FeeConfig",
    "FeeRuleConfig",
    "HarnessConfig",
    "ResourceLimitsConfig",
    "RiskPipelineConfig",
    "RiskRuleConfig",
    "RuntimeAdapterConfig",
    "StrategyInstanceConfig",
    "StrictConfigurationModel",
    "TraderConfig",
    "TraderConfiguration",
]
