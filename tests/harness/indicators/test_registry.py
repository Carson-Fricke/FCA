"""Behavior of the typed indicator registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import TradeEvent
from harness.indicators import (
    DEFAULT_REGISTRY,
    DollarVolume,
    IndicatorConfigurationError,
    IndicatorParam,
    IndicatorParamKind,
    IndicatorRegistry,
    IndicatorSpec,
    SimpleMovingAverage,
    TickIndicator,
    create_indicator,
    indicator_names,
    indicator_spec,
)

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def test_builtin_names_cover_the_required_set() -> None:
    assert set(indicator_names()) == {
        "atr",
        "dollar_volume",
        "ema",
        "mid_sma",
        "obv",
        "realized_vol",
        "rsi",
        "sma",
        "vwap",
    }


def test_create_indicator_by_name_binds_canonical_symbol() -> None:
    indicator = create_indicator("sma", {"period": 2}, symbol="aapl")
    assert isinstance(indicator, SimpleMovingAverage)
    assert str(indicator.symbol) == "AAPL"
    assert indicator.interval == timedelta(minutes=1)


def test_create_validates_required_integer_parameters() -> None:
    with pytest.raises(IndicatorConfigurationError, match="requires parameter"):
        create_indicator("sma", {}, symbol="AAPL")
    with pytest.raises(IndicatorConfigurationError, match="must be an integer"):
        create_indicator("sma", {"period": "2"}, symbol="AAPL")
    with pytest.raises(IndicatorConfigurationError, match="must be an integer"):
        create_indicator("sma", {"period": True}, symbol="AAPL")
    with pytest.raises(IndicatorConfigurationError, match="must be positive"):
        create_indicator("sma", {"period": 0}, symbol="AAPL")


def test_create_validates_unknown_names_and_parameters() -> None:
    with pytest.raises(IndicatorConfigurationError, match="unknown indicator"):
        create_indicator("macd", {}, symbol="AAPL")
    with pytest.raises(IndicatorConfigurationError, match="does not accept"):
        create_indicator("sma", {"period": 2, "span": 3}, symbol="AAPL")


def test_create_validates_duration_parameters() -> None:
    with pytest.raises(IndicatorConfigurationError, match="duration"):
        create_indicator("realized_vol", {"window": "abc"}, symbol="AAPL")
    with pytest.raises(IndicatorConfigurationError, match="duration"):
        create_indicator("realized_vol", {"window": 5}, symbol="AAPL")
    indicator = create_indicator("realized_vol", {"window": "5min"}, symbol="AAPL")
    assert indicator.value is None


def test_bucket_parameter_is_shared_and_typed() -> None:
    indicator = create_indicator("sma", {"period": 1, "bucket": "30s"}, symbol="AAPL")
    assert indicator.interval == timedelta(seconds=30)
    with pytest.raises(IndicatorConfigurationError, match="duration"):
        create_indicator("sma", {"period": 1, "bucket": "nan"}, symbol="AAPL")


def test_bucket_parameter_changes_emission_grid() -> None:
    indicator = create_indicator("sma", {"period": 1, "bucket": "30s"}, symbol="AAPL")
    indicator.on_event(
        TradeEvent(
            "t1",
            STAMP + timedelta(seconds=10),
            1,
            Symbol("AAPL"),
            Decimal("100"),
            Decimal("1"),
        )
    )
    indicator.on_event(
        TradeEvent(
            "t2",
            STAMP + timedelta(seconds=40),
            2,
            Symbol("AAPL"),
            Decimal("102"),
            Decimal("1"),
        )
    )
    indicator.flush()
    assert [sample.end for sample in indicator.samples()] == [
        STAMP + timedelta(seconds=30),
        STAMP + timedelta(seconds=60),
    ]


def test_dollar_volume_window_is_optional_and_selects_mode() -> None:
    cumulative = create_indicator("dollar_volume", {}, symbol="AAPL")
    assert isinstance(cumulative, DollarVolume)
    assert cumulative.window is None
    rolling = create_indicator("dollar_volume", {"window": "5min"}, symbol="AAPL")
    assert isinstance(rolling, DollarVolume)
    assert rolling.window == timedelta(minutes=5)


def test_spec_describes_the_typed_schema() -> None:
    spec = indicator_spec("sma")
    assert [param.name for param in spec.params] == ["period", "bucket"]
    assert spec.params[0].kind is IndicatorParamKind.INTEGER
    assert spec.params[0].required is True
    assert spec.params[1].required is False
    assert spec.params[1].default == "1min"
    assert "Rolling simple mean" in spec.description


class _CustomIndicator(TickIndicator):
    NAME = "custom_test"
    __slots__ = ()

    def _compute_value(self):
        return None


def test_registry_rejects_duplicates_and_reserved_params() -> None:
    registry = IndicatorRegistry()
    registry.register(indicator_spec("sma"))
    with pytest.raises(IndicatorConfigurationError, match="already registered"):
        registry.register(indicator_spec("sma"))
    with pytest.raises(ValueError, match="reserved"):
        IndicatorSpec(
            "custom",
            "duplicate bucket parameter",
            (IndicatorParam("bucket", IndicatorParamKind.DURATION),),
            _CustomIndicator,
        )


def test_custom_registry_accepts_private_extensions() -> None:
    registry = IndicatorRegistry()
    spec = IndicatorSpec(
        "custom_test",
        "Private extension indicator.",
        (),
        _CustomIndicator,
    )
    registry.register(spec)
    indicator = registry.create("custom_test", {}, symbol="AAPL")
    assert isinstance(indicator, _CustomIndicator)
    assert registry.names() == ("custom_test",)
    assert "custom_test" not in DEFAULT_REGISTRY.names()
