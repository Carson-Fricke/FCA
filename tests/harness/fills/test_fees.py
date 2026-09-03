from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types.accounting import Fill
from harness.core.types.orders import Side
from harness.fills.fees import (
    AssetClass,
    FeeContext,
    LiquidityRole,
    aggregate_fees,
    load_fee_schedule,
)

ROOT = Path(__file__).parents[3]
STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def fill(
    fill_id: str,
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal("1"),
    price: Decimal = Decimal("100"),
    liquidity: LiquidityRole | None = None,
) -> Fill:
    return Fill(
        event_id=f"event-{fill_id}",
        timestamp=STAMP,
        sequence=0,
        fill_id=fill_id,
        order_id=f"order-{fill_id}",
        symbol=Symbol(symbol),
        side=side,
        quantity=quantity,
        price=price,
        liquidity=None if liquidity is None else liquidity.value,
    )


def context(
    asset_class: AssetClass,
    *,
    run_id: str = "run-1",
    strategy_id: str = "strategy-a",
) -> FeeContext:
    return FeeContext(
        run_id=run_id,
        strategy_id=strategy_id,
        asset_class=asset_class,
    )


def test_equity_fixture_calculates_commission_and_regulatory_fees_to_cent() -> None:
    schedule = load_fee_schedule("us-retail", ROOT)

    buy = schedule.calculate(
        fill("equity-buy", quantity=Decimal("100"), price=Decimal("50")),
        context(AssetClass.EQUITY),
    )
    sell = schedule.calculate(
        fill(
            "equity-sell",
            side=Side.SELL,
            quantity=Decimal("100"),
            price=Decimal("50"),
        ),
        context(AssetClass.EQUITY),
    )

    assert buy.total == Decimal("1.00")
    assert sell.components == (
        schedule.equity.calculate(
            fill(
                "equity-components",
                side=Side.SELL,
                quantity=Decimal("100"),
                price=Decimal("50"),
            )
        )
    )
    assert tuple(component.amount for component in sell.components) == (
        Decimal("1.00"),
        Decimal("0.14"),
        Decimal("0.02"),
    )
    assert sell.total == Decimal("1.16")


def test_option_fixture_calculates_per_contract_fees_to_cent() -> None:
    schedule = load_fee_schedule("us-retail", ROOT)
    assessment = schedule.calculate(
        fill(
            "option",
            symbol="AAPL240119C00150000",
            quantity=Decimal("3"),
            price=Decimal("2.50"),
        ),
        context(AssetClass.OPTION),
    )

    assert tuple(component.amount for component in assessment.components) == (
        Decimal("1.95"),
        Decimal("0.09"),
    )
    assert assessment.total == Decimal("2.04")

    with pytest.raises(ValueError, match="whole number"):
        schedule.calculate(
            fill("fractional-option", quantity=Decimal("1.5")),
            context(AssetClass.OPTION),
        )


@pytest.mark.parametrize(
    ("liquidity", "expected"),
    [
        (LiquidityRole.MAKER, Decimal("15.00")),
        (LiquidityRole.TAKER, Decimal("25.00")),
    ],
)
def test_crypto_fixture_selects_maker_taker_rate_to_cent(
    liquidity: LiquidityRole, expected: Decimal
) -> None:
    schedule = load_fee_schedule("us-retail", ROOT)
    assessment = schedule.calculate(
        fill(
            f"crypto-{liquidity.value}",
            symbol="BTC/USD",
            quantity=Decimal("0.2"),
            price=Decimal("50000"),
            liquidity=liquidity,
        ),
        context(AssetClass.CRYPTO),
    )

    assert assessment.total == expected


def test_apply_returns_new_fill_with_exact_calculated_fee() -> None:
    schedule = load_fee_schedule("us-retail", ROOT)
    original = fill(
        "applied",
        symbol="AAPL240119P00150000",
        quantity=Decimal("2"),
        price=Decimal("1.25"),
    )

    charged, assessment = schedule.apply(original, context(AssetClass.OPTION))

    assert original.fee == Decimal("0")
    assert charged.fee == Decimal("1.36")
    assert charged.fee == assessment.total


def test_aggregation_splits_run_strategy_and_asset_class_without_coupling() -> None:
    schedule = load_fee_schedule("us-retail", ROOT)
    shared_fill = fill("shared-input", quantity=Decimal("100"))
    first = schedule.calculate(
        shared_fill,
        context(AssetClass.EQUITY, strategy_id="strategy-a"),
    )
    second = schedule.calculate(
        fill(
            "second",
            symbol="AAPL240119C00150000",
            quantity=Decimal("1"),
        ),
        context(AssetClass.OPTION, strategy_id="strategy-b"),
    )
    third = schedule.calculate(
        fill(
            "third",
            symbol="BTC/USD",
            quantity=Decimal("0.2"),
            price=Decimal("50000"),
            liquidity=LiquidityRole.MAKER,
        ),
        context(
            AssetClass.CRYPTO,
            run_id="run-2",
            strategy_id="strategy-a",
        ),
    )

    totals = aggregate_fees((third, first, second))

    assert [(item.key, item.amount) for item in totals.by_run] == [
        ("run-1", Decimal("1.68")),
        ("run-2", Decimal("15.00")),
    ]
    assert [(item.key, item.amount) for item in totals.by_strategy] == [
        ("strategy-a", Decimal("16.00")),
        ("strategy-b", Decimal("0.68")),
    ]
    assert [(item.key, item.amount) for item in totals.by_asset_class] == [
        ("crypto", Decimal("15.00")),
        ("equity", Decimal("1.00")),
        ("option", Decimal("0.68")),
    ]
    assert (
        first.total
        == schedule.calculate(
            shared_fill,
            context(AssetClass.EQUITY, strategy_id="another-strategy"),
        ).total
    )


def test_schedule_rates_change_through_yaml_only(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "fees"
    config_dir.mkdir(parents=True)
    source = ROOT / "config" / "fees" / "us-retail.yaml"
    configured = source.read_text(encoding="utf-8").replace(
        "maker_rate_bps: 15.00", "maker_rate_bps: -2.00"
    )
    (config_dir / "us-retail.yaml").write_text(configured, encoding="utf-8")
    schedule = load_fee_schedule("us-retail", tmp_path)

    assessment = schedule.calculate(
        fill(
            "rebate",
            symbol="BTC/USD",
            quantity=Decimal("1"),
            price=Decimal("1000"),
            liquidity=LiquidityRole.MAKER,
        ),
        context(AssetClass.CRYPTO),
    )

    assert assessment.total == Decimal("-0.20")


def test_loader_rejects_traversal_float_inputs_and_unknown_fields(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        load_fee_schedule("../outside", ROOT)

    schedule = load_fee_schedule("us-retail", ROOT)
    with pytest.raises(TypeError, match="exact Decimal"):
        schedule.equity.__class__(
            rounding=schedule.equity.rounding,
            commission_per_share=cast(Decimal, 0.1),
            commission_minimum=Decimal("0"),
            sec_rate_bps=Decimal("0"),
            taf_per_share=Decimal("0"),
            taf_maximum=Decimal("0"),
        )

    config_dir = tmp_path / "config" / "fees"
    config_dir.mkdir(parents=True)
    source = (ROOT / "config" / "fees" / "us-retail.yaml").read_text(encoding="utf-8")
    (config_dir / "us-retail.yaml").write_text(
        f"{source}\nunknown: true\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unexpected"):
        load_fee_schedule("us-retail", tmp_path)


def test_crypto_requires_liquidity_and_aggregation_rejects_duplicate_fill() -> None:
    schedule = load_fee_schedule("us-retail", ROOT)
    without_liquidity = fill("without-liquidity", symbol="BTC/USD")
    with pytest.raises(ValueError, match="maker or taker"):
        schedule.calculate(without_liquidity, context(AssetClass.CRYPTO))

    assessment = schedule.calculate(
        fill("duplicate", quantity=Decimal("10")),
        context(AssetClass.EQUITY),
    )
    with pytest.raises(ValueError, match="duplicate fee assessment"):
        aggregate_fees((assessment, assessment))
