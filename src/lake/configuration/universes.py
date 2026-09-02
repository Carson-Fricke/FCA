"""Typed and canonical market-lake universe loaders."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lake.configuration.loader import (
    load_yaml_mapping,
    market_lake_config_dir,
    validate_yaml_settings,
)
from lake.contracts import DataType, symbol_filename


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


_DATA_TYPE_ALIASES = {
    "trade": DataType.TRADE,
    "trades": DataType.TRADE,
    "quote": DataType.QUOTE,
    "quotes": DataType.QUOTE,
}
_EQUITY_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]*", re.ASCII)
_CRYPTO_PAIR = re.compile(r"[A-Z0-9]+/[A-Z0-9]+", re.ASCII)


def _unique_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a sequence")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} entries must be strings")
        normalized = item.strip().upper()
        if not normalized:
            raise ValueError(f"{field_name} entries must not be empty")
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return tuple(result)


def _data_types(value: object) -> tuple[DataType, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("data_types must be a sequence")
    result: list[DataType] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError("data_types entries must be strings")
        try:
            normalized = _DATA_TYPE_ALIASES[item.strip().lower()]
        except KeyError as error:
            raise ValueError(f"unsupported data type: {item!r}") from error
        if normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("data_types must not be empty")
    return tuple(result)


def _symbols(value: object, field_name: str) -> tuple[str, ...]:
    result = _unique_sequence(value, field_name)
    for symbol in result:
        if _EQUITY_SYMBOL.fullmatch(symbol) is None:
            raise ValueError(f"invalid equity symbol in {field_name}: {symbol!r}")
        symbol_filename(symbol)
    return result


def _pairs(value: object) -> tuple[str, ...]:
    result = _unique_sequence(value, "crypto.pairs")
    for pair in result:
        if _CRYPTO_PAIR.fullmatch(pair) is None:
            raise ValueError(f"invalid crypto pair: {pair!r}")
        symbol_filename(pair)
    return result


class EquityHistory(_StrictModel):
    policy: Literal["all_available"]
    maximum_liquidity_rank: int | None = Field(default=None, ge=1)


class EquityUniverse(_StrictModel):
    symbols: tuple[str, ...]
    data_types: tuple[DataType, ...]
    feed: Literal["sip"]
    adjustment: Literal["raw"]
    history: dict[DataType, EquityHistory]
    backfill_order: Literal["newest_first"]

    _normalize_symbols = field_validator("symbols", mode="before")(_symbols)
    _normalize_data_types = field_validator("data_types", mode="before")(_data_types)

    @field_validator("history", mode="before")
    @classmethod
    def _normalize_history_keys(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise TypeError("equities.*.history must be a mapping")
        normalized: dict[DataType, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or key.strip().lower() not in _DATA_TYPE_ALIASES
            ):
                raise ValueError(f"unsupported history data type: {key!r}")
            normalized[_DATA_TYPE_ALIASES[key.strip().lower()]] = item
        return normalized

    @model_validator(mode="after")
    def _require_history_policies(self) -> EquityUniverse:
        required = {DataType.TRADE, DataType.QUOTE}
        if set(self.history) != required:
            raise ValueError("equity history must define both trades and quotes")
        if not set(self.data_types).issubset(required):
            raise ValueError("equity data_types must contain only trades or quotes")
        return self


class EquityUniverses(_StrictModel):
    soak: EquityUniverse


class OptionSelection(_StrictModel):
    frequency: Literal["daily"]
    persist_front_set: bool
    expiration_order: Literal["nearest"]
    expiration_count: int = Field(ge=1, le=12)
    strike_window: StrikeWindow


class StrikeWindow(_StrictModel):
    basis: Literal["underlying_spot"]
    percent_below: float = Field(gt=0, le=100)
    percent_above: float = Field(gt=0, le=100)


class OptionSoak(_StrictModel):
    underlyings: tuple[str, ...]
    lookback_years: float = Field(gt=0, le=100)

    _normalize_underlyings = field_validator("underlyings", mode="before")(_symbols)


class OptionExpansion(_StrictModel):
    enabled: bool
    maximum_underlyings: int = Field(ge=1, le=100_000)


class OptionsUniverse(_StrictModel):
    feed: Literal["opra"]
    data_types: tuple[DataType, ...]
    selection: OptionSelection
    soak: OptionSoak
    expansion: OptionExpansion

    _normalize_data_types = field_validator("data_types", mode="before")(_data_types)


class CryptoRollout(_StrictModel):
    enabled: bool
    target_pair_count: int = Field(ge=1, le=100_000)
    scheduling_priority: Literal["after_equities_and_options"]
    quota_policy: Literal["shared_backfill_quota"]


class CryptoUniverse(_StrictModel):
    pairs: tuple[str, ...]
    data_types: tuple[DataType, ...]
    rollout: CryptoRollout

    _normalize_pairs = field_validator("pairs", mode="before")(_pairs)
    _normalize_data_types = field_validator("data_types", mode="before")(_data_types)


class UniverseSettings(_EnvironmentSettings):
    """Complete typed representation of ``universes.yaml``."""

    schema_version: Literal[1]
    equities: EquityUniverses
    options: OptionsUniverse
    crypto: CryptoUniverse


def load_universes(
    root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> UniverseSettings:
    """Load, validate, and canonicalize the configured asset universes."""
    path = market_lake_config_dir(root) / "universes.yaml"
    defaults = load_yaml_mapping(path)
    return validate_yaml_settings(UniverseSettings, defaults, environ)
