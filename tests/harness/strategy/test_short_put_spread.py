"""Focused tests for the reference short-put-spread strategy.

Each scenario replays deterministic synthetic option quotes through the
packaged :mod:`harness.testing.options_fixture` kernel -- a point-in-time
option universe, a read-only clock, an atomic multi-leg matcher, a
cash/position ledger, and option expiry settlement -- because the Rust host
owns the live engine and the Python engine was deleted.  Quote prices are
per-contract notional values (per-share bid/ask scaled by the contract
multiplier), so every golden money value is exact contract dollars.

The suite covers the full chain the strategy contract exercises: entry,
profit/stop/managed/expiry exits, cancellation of an unfilled entry,
collateral reporting, and option settlement (worthless, assigned, and
exercised) with referentially consistent underlying fills.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path

from strategies.harness_examples.short_put_spread import ShortPutSpreadStrategy

from harness.data.options_universe import OptionUniverseSnapshot
from harness.strategy import assert_sandbox_clean, lint_strategy_directory
from harness.testing import options_fixture
from harness.testing.options_fixture import (
    SPS_ENTRY_DAY,
    SPS_EXPIRY,
    SPS_FEB_OSI,
    SPS_LONG_OSI,
    SPS_MANAGED_DAY,
    SPS_SHORT_OSI,
    SPS_UNDERLYING,
    FixtureQuote,
    FixtureRun,
    SettlementKind,
    sps_market,
    sps_round,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
INITIAL_CASH = Decimal("100000.00")


def entry_round() -> tuple[FixtureQuote, FixtureQuote]:
    """Quotes that fill the entry at a 50.00 per-contract credit."""
    return sps_round(SPS_ENTRY_DAY, 0, short=("0.98", "1.02"), long=("0.48", "0.52"))


def profit_round() -> tuple[FixtureQuote, FixtureQuote]:
    """Quotes whose compressed mark trips the profit exit."""
    return sps_round(
        SPS_ENTRY_DAY,
        2,
        short=("0.28", "0.32"),
        long=("0.04", "0.06"),
        offset_us=1_000_001,
    )


def run_spread(
    quotes: tuple[FixtureQuote, ...], *, cancel_dte: int = 0, exit_dte: int = 0
) -> FixtureRun[ShortPutSpreadStrategy]:
    """Replay one canonical short-put-spread market over ``quotes``."""
    strategy = ShortPutSpreadStrategy(cancel_dte=cancel_dte, exit_dte=exit_dte)
    return sps_market(quotes).replay(strategy)


def test_short_put_spread_enters_and_takes_profit() -> None:
    run = run_spread(entry_round() + profit_round())

    assert run.strategy.phase == "flat"
    assert tuple(run.strategy.callbacks) == (
        "on_start",
        "on_quote",
        "on_quote",
        "on_fill",
        "on_fill",
        "on_quote",
        "on_quote",
        "on_fill",
        "on_fill",
        "on_stop",
    )
    assert [
        (fill.order_id, str(fill.symbol), fill.side.value, fill.price)
        for fill in run.fills
    ] == [
        ("sps-0001", SPS_SHORT_OSI, "sell", Decimal("100.00")),
        ("sps-0001", SPS_LONG_OSI, "buy", Decimal("50.00")),
        ("sps-0002", SPS_SHORT_OSI, "buy", Decimal("30.00")),
        ("sps-0002", SPS_LONG_OSI, "sell", Decimal("5.00")),
    ]
    assert all(fill.liquidity == "maker" for fill in run.fills)

    assert run.order("sps-0001").status == "filled"
    assert run.order("sps-0002").status == "filled"
    assert [group.order_type.value for group in run.groups] == ["limit", "limit"]

    assert run.book.cash == Decimal("100025.00")
    assert run.book.position(SPS_SHORT_OSI) == Decimal("0")
    assert run.book.position(SPS_LONG_OSI) == Decimal("0")
    assert run.book.realized_pnl == Decimal("25.00")

    assert [record.name for record in run.emissions.records] == [
        "short_put_spread_entry",
        "short_put_spread_exit",
        "short_put_spread_summary",
    ]
    entry = run.emissions.fields("short_put_spread_entry")
    assert entry["short_symbol"] == SPS_SHORT_OSI
    assert entry["long_symbol"] == SPS_LONG_OSI
    assert entry["contracts"] == Decimal("1")
    assert entry["entry_credit"] == Decimal("50.00")
    assert entry["collateral_bound"] == Decimal("450.00")
    exit_fields = run.emissions.fields("short_put_spread_exit")
    assert exit_fields["exit_debit"] == Decimal("25.00")
    assert exit_fields["realized_pnl"] == Decimal("25.00")
    summary = run.emissions.fields("short_put_spread_summary")
    assert summary["phase"] == "flat"
    assert summary["filled_groups"] == 2


def test_short_put_spread_selection_is_point_in_time() -> None:
    run = run_spread(entry_round() + profit_round())

    snapshot = run.data.read(SPS_UNDERLYING)
    assert isinstance(snapshot, OptionUniverseSnapshot)
    assert snapshot.symbols == (SPS_LONG_OSI, SPS_SHORT_OSI)
    assert SPS_FEB_OSI not in snapshot.symbols
    assert snapshot.dte(SPS_EXPIRY) == 3
    assert run.data.read("QQQ") is None


def test_short_put_spread_unfilled_entry_cancels() -> None:
    quotes = sps_round(
        SPS_ENTRY_DAY, 0, short=("0.92", "0.96"), long=("0.49", "0.51")
    ) + sps_round(SPS_MANAGED_DAY, 2, short=("0.92", "0.96"), long=("0.49", "0.51"))
    run = run_spread(quotes, cancel_dte=1)

    assert run.strategy.phase == "canceled_no_fill"
    assert tuple(run.strategy.callbacks) == (
        "on_start",
        "on_quote",
        "on_quote",
        "on_quote",
        "on_quote",
        "on_stop",
    )
    assert run.fills == ()
    assert run.order("sps-0001").status == "canceled"
    assert run.book.cash == INITIAL_CASH
    assert run.strategy.entry_credit is None


def test_short_put_spread_stop_exit_redeems_at_the_stop_multiple() -> None:
    quotes = entry_round() + sps_round(
        SPS_ENTRY_DAY,
        2,
        short=("1.80", "1.82"),
        long=("0.78", "0.80"),
        offset_us=1_000_001,
    )
    run = run_spread(quotes)

    assert run.strategy.phase == "flat"
    assert run.strategy.realized_pnl == Decimal("-52.00")
    assert run.order("sps-0002").filled
    assert [fill.price for fill in run.fills[2:]] == [
        Decimal("181.00"),
        Decimal("79.00"),
    ]
    assert run.book.cash == Decimal("99948.00")


def test_short_put_spread_managed_exit_redeems_at_the_configured_dte() -> None:
    quotes = entry_round() + sps_round(
        SPS_MANAGED_DAY, 2, short=("0.58", "0.62"), long=("0.18", "0.22")
    )
    run = run_spread(quotes, exit_dte=1)

    assert run.strategy.phase == "flat"
    assert run.strategy.realized_pnl == Decimal("10.00")
    assert run.order("sps-0002").filled
    assert run.book.cash == Decimal("100010.00")


def test_short_put_spread_expiry_residual_liquidates_at_market() -> None:
    quotes = entry_round() + sps_round(
        SPS_EXPIRY, 2, short=("0.55", "0.59"), long=("0.05", "0.07")
    )
    run = run_spread(quotes)

    assert run.strategy.phase == "flat"
    assert [(fill.order_id, fill.side.value, fill.price) for fill in run.fills[2:]] == [
        ("sps-0002", "buy", Decimal("59.00")),
        ("sps-0002", "sell", Decimal("5.00")),
    ]
    assert all(fill.liquidity == "taker" for fill in run.fills[2:])
    assert run.strategy.realized_pnl == Decimal("-4.00")
    assert run.book.cash == Decimal("99996.00")


def test_short_put_spread_zero_mark_awaits_worthless_settlement() -> None:
    quotes = entry_round() + sps_round(
        SPS_EXPIRY, 2, short=("0.04", "0.06"), long=("0.04", "0.06")
    )
    run = run_spread(quotes)

    assert run.strategy.phase == "awaiting_settlement"
    assert len(run.fills) == 2
    assert run.book.cash == Decimal("100050.00")
    assert run.emissions.fields("short_put_spread_expiry") == {
        "disposition": "awaiting_settlement"
    }

    settlements = run.settle(Decimal("520.00"))

    assert [settlement.kind for settlement in settlements] == [
        SettlementKind.WORTHLESS,
        SettlementKind.WORTHLESS,
    ]
    assert all(settlement.contracts == Decimal("0") for settlement in settlements)
    assert all(settlement.underlying_fill is None for settlement in settlements)
    assert run.book.position(SPS_SHORT_OSI) == Decimal("0")
    assert run.book.position(SPS_LONG_OSI) == Decimal("0")
    assert run.book.position(SPS_UNDERLYING) == Decimal("0")
    assert run.book.cash == Decimal("100050.00")


def test_short_put_spread_assignment_assigns_the_itm_short_put() -> None:
    run = run_spread(entry_round())
    assert run.strategy.phase == "open"

    settlements = run.settle(Decimal("497.00"))

    by_symbol = {settlement.symbol: settlement for settlement in settlements}
    assert by_symbol[SPS_LONG_OSI].kind is SettlementKind.WORTHLESS
    assigned = by_symbol[SPS_SHORT_OSI]
    assert assigned.kind is SettlementKind.ASSIGNED
    assert assigned.contracts == Decimal("1")
    assert assigned.underlying_fill is not None
    assert assigned.underlying_fill.side.value == "buy"
    assert assigned.underlying_fill.quantity == Decimal("100")
    assert assigned.underlying_fill.price == Decimal("500")
    assert run.book.position(SPS_UNDERLYING) == Decimal("100")
    assert run.book.position(SPS_SHORT_OSI) == Decimal("0")
    assert run.book.position(SPS_LONG_OSI) == Decimal("0")
    assert run.book.cash == Decimal("50050.00")


def test_short_put_spread_full_itm_assignment_nets_the_width_loss() -> None:
    run = run_spread(entry_round())

    settlements = run.settle(Decimal("490.00"))

    assert [settlement.kind for settlement in settlements] == [
        SettlementKind.EXERCISED,
        SettlementKind.ASSIGNED,
    ]
    assert run.book.position(SPS_UNDERLYING) == Decimal("0")
    assert run.book.position(SPS_SHORT_OSI) == Decimal("0")
    assert run.book.position(SPS_LONG_OSI) == Decimal("0")
    assert run.book.cash == Decimal("99550.00")


def test_short_put_spread_replays_identically() -> None:
    first = run_spread(entry_round() + profit_round())
    second = run_spread(entry_round() + profit_round())

    assert first.strategy.callbacks == second.strategy.callbacks
    assert first.fills == second.fills
    assert first.emissions.records == second.emissions.records
    assert first.book.cash == second.book.cash
    assert first.book.realized_pnl == second.book.realized_pnl
    assert first.strategy.phase == second.strategy.phase


def test_short_put_spread_source_is_sandbox_clean() -> None:
    directory_report = lint_strategy_directory(REPO_ROOT / "strategies")
    assert directory_report.ok, directory_report.render()

    report = assert_sandbox_clean(
        REPO_ROOT / "strategies" / "harness_examples" / "short_put_spread.py"
    )
    assert report.ok


def test_options_fixture_uses_no_wall_clock_random_or_network_source() -> None:
    source = inspect.getsource(options_fixture)
    forbidden = (
        "datetime.now",
        "utcnow",
        "time.time",
        "monotonic",
        "perf_counter",
        "import time",
        "import socket",
        "import urllib",
        "import httpx",
        "import requests",
        "os.urandom",
    )

    assert tuple(token for token in forbidden if token in source) == ()
    assert "Random(" in source


def test_short_put_spread_strategy_is_structurally_a_strategy() -> None:
    from harness.strategy import Strategy

    assert issubclass(ShortPutSpreadStrategy, Strategy)
