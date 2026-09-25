"""Concrete screener context tests over real reference Parquet objects.

The classification and fundamentals fixtures are written as actual Parquet
partitions in the lake's Hive layout and read back through the harness
reference facade (``harness.data.reference``) — the production path — so the
point-in-time semantics (snapshot knowledge dates, filed-date restatements)
are exercised for real rather than stubbed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from harness.data.reference import classification_table, fundamentals_table
from harness.data.security_master import SecurityMasterView
from harness.data.universe_view import EquityListingUniverse, UniverseView
from lake.catalog.models import ListingRecord
from thesis.screening import (
    MarketBar,
    OhlcvBar,
    ScreenerDefinition,
    ScreenerError,
    UniverseEntry,
    run_screener,
)
from thesis.screening.context import (
    LakeClassificationFacts,
    LakeFundamentalsFacts,
    LakeScreenerContext,
    StaticCikIndex,
)

AS_OF = date(2026, 9, 9)
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class FixedClock:
    """Deterministic engine clock pinned to the screening instant."""

    def now(self) -> datetime:
        return NOW


class StaticListings:
    """Order-free listing episodes filtered exactly like the catalog port."""

    def __init__(self, episodes: tuple[ListingRecord, ...]) -> None:
        self._episodes = episodes

    def list(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ListingRecord, ...]:
        visible = self._episodes
        if entity_id is not None:
            visible = tuple(e for e in visible if e.entity_id == entity_id)
        if symbol is not None:
            visible = tuple(e for e in visible if e.symbol == symbol)
        return visible


class StaticOhlcvPort:
    """Canned daily bars keyed by symbol."""

    def __init__(self, bars: dict[str, tuple[OhlcvBar, ...]]) -> None:
        self._bars = bars

    def daily_bars(self, symbol: str, start: date, end: date) -> tuple[OhlcvBar, ...]:
        return tuple(
            bar for bar in self._bars.get(symbol, ()) if start <= bar.bar_date <= end
        )


def _write_classification(
    root: Path, knowledge_date: date, rows: list[dict[str, object]]
) -> None:
    partition = (
        root
        / f"date={knowledge_date.isoformat()}"
        / "asset_class=reference"
        / "data_type=classification"
    )
    partition.mkdir(parents=True, exist_ok=True)
    pq.write_table(classification_table(rows), partition / "SNAPSHOT.parquet")


def _write_fundamentals(
    root: Path, filed: date, cik: int, rows: list[dict[str, object]]
) -> None:
    partition = (
        root
        / f"date={filed.isoformat()}"
        / "asset_class=reference"
        / "data_type=fundamentals"
    )
    partition.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        fundamentals_table([{**row, "cik": cik} for row in rows]),
        partition / f"CIK{cik:010d}.parquet",
    )


def _classification_row(
    symbol: str, *, etf: bool = False, exchange: str = "NASDAQ"
) -> dict[str, object]:
    return {
        "file_creation_time": datetime(2026, 9, 1, tzinfo=UTC),
        "symbol": symbol,
        "security_name": f"{symbol} Common Stock",
        "listing_exchange": exchange,
        "market_category": "Q",
        "etf": etf,
        "round_lot_size": Decimal("100"),
        "test_issue": False,
        "financial_status": None,
    }


def _fact(
    *,
    cik: int,
    taxonomy: str,
    tag: str,
    unit: str,
    val: str,
    filed: date,
    accn: str = "0000000001",
) -> dict[str, object]:
    return {
        "cik": cik,
        "taxonomy": taxonomy,
        "tag": tag,
        "unit": unit,
        "period_end": date(2026, 6, 30),
        "fy": 2026,
        "fp": "Q2",
        "form": "10-Q",
        "accn": accn,
        "filed": filed,
        "frame": None,
        "val": Decimal(val),
    }


def _bars_for(closes: list[str], *, volume: int = 1_000_000) -> tuple[OhlcvBar, ...]:
    return tuple(
        OhlcvBar(
            bar_date=date.fromordinal(date(2026, 8, 1).toordinal() + offset),
            open=Decimal(close),
            high=Decimal(close) + Decimal("0.50"),
            low=Decimal(close) - Decimal("0.50"),
            close=Decimal(close),
            volume=Decimal(volume),
        )
        for offset, close in enumerate(closes)
    )


def _flat_bars(count: int = 30) -> tuple[OhlcvBar, ...]:
    return _bars_for(["10.00"] * count)


def _default_bars() -> dict[str, tuple[OhlcvBar, ...]]:
    return {"AAA": _flat_bars(), "BBB": _flat_bars()}


def _trending_bars(direction: str) -> tuple[OhlcvBar, ...]:
    if direction == "up":
        closes = [f"{10 + offset}.00" for offset in range(30)]
    else:
        closes = [f"{40 - offset}.00" for offset in range(30)]
    return _bars_for(closes)


def _listings() -> tuple[ListingRecord, ...]:
    return (
        ListingRecord(entity_id="ENTITY-1", symbol="AAA", listed_date=date(2025, 1, 2)),
        ListingRecord(entity_id="ENTITY-2", symbol="OLD", listed_date=date(2025, 1, 2)),
        ListingRecord(entity_id="ENTITY-2", symbol="BBB", listed_date=date(2025, 6, 2)),
    )


def _build_root(
    tmp_path: Path,
    *,
    filings: tuple[tuple[date, int, list[dict[str, object]]], ...] = (),
) -> Path:
    root = tmp_path / "lake"
    root.mkdir(exist_ok=True)
    _write_classification(
        root,
        date(2026, 9, 1),
        [
            _classification_row("AAA"),
            _classification_row("BBB", etf=True),
        ],
    )
    for filed, cik, rows in filings:
        _write_fundamentals(root, filed, cik, rows)
    return root


def _context(
    root: Path,
    *,
    bars: dict[str, tuple[OhlcvBar, ...]] | None = None,
) -> LakeScreenerContext:
    clock = FixedClock()
    listings = StaticListings(_listings())
    return LakeScreenerContext(
        clock=clock,
        universe_view=UniverseView(clock, EquityListingUniverse(listings)),
        security_master=SecurityMasterView(clock, listings),
        ohlcv=StaticOhlcvPort(bars if bars is not None else _default_bars()),
        classification=LakeClassificationFacts(root),
        fundamentals=LakeFundamentalsFacts(root),
        cik_index=StaticCikIndex({"AAA": 1}),
    )


def test_classification_facts_map_the_snapshot(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    context = _context(root)

    stock = context.classification("AAA", AS_OF)
    assert stock["security_type"] == "stock"
    assert stock["exchange"] == "NASDAQ"
    assert stock["security_name"] == "AAA Common Stock"
    etf = context.classification("BBB", AS_OF)
    assert etf["security_type"] == "etf"
    assert context.classification("ZZZ", AS_OF) == {}


def test_classification_snapshot_knowledge_dates_are_point_in_time(
    tmp_path: Path,
) -> None:
    root = _build_root(tmp_path)
    later = date(2026, 9, 5)
    _write_classification(root, later, [_classification_row("AAA", exchange="NYSE")])
    context = _context(root)

    assert context.classification("AAA", date(2026, 9, 4))["exchange"] == "NASDAQ"
    assert context.classification("AAA", later)["exchange"] == "NYSE"


def test_fundamentals_are_filed_date_point_in_time(tmp_path: Path) -> None:
    root = _build_root(
        tmp_path,
        filings=(
            (
                date(2026, 7, 15),
                1,
                [
                    _fact(
                        cik=1,
                        taxonomy="us-gaap",
                        tag="NetIncomeLoss",
                        unit="USD",
                        val="-5000",
                        filed=date(2026, 7, 15),
                    ),
                    _fact(
                        cik=1,
                        taxonomy="dei",
                        tag="EntityCommonStockSharesOutstanding",
                        unit="shares",
                        val="1000000",
                        filed=date(2026, 7, 15),
                    ),
                ],
            ),
            (
                date(2026, 9, 5),
                1,
                [
                    _fact(
                        cik=1,
                        taxonomy="us-gaap",
                        tag="NetIncomeLoss",
                        unit="USD",
                        val="40000",
                        filed=date(2026, 9, 5),
                        accn="0000000002",
                    ),
                ],
            ),
        ),
    )
    context = _context(root)

    before_restatement = context.fundamentals("AAA", date(2026, 9, 1))
    assert before_restatement["net_income"] == "-5000"
    assert before_restatement["shares_outstanding"] == "1000000"
    assert before_restatement["net_income_filed"] == "2026-07-15"

    after_restatement = context.fundamentals("AAA", AS_OF)
    assert after_restatement["net_income"] == "40000"
    assert after_restatement["net_income_filed"] == "2026-09-05"

    assert context.fundamentals("BBB", AS_OF) == {}


def test_universe_and_listing_dates_come_from_the_security_master(
    tmp_path: Path,
) -> None:
    root = _build_root(tmp_path)
    context = _context(root)

    entries = context.universe(AS_OF)

    assert [entry.symbol for entry in entries] == ["AAA", "BBB"]
    # ENTITY-2 renamed OLD -> BBB: the entity's age is the earliest episode.
    assert [entry.listing_date for entry in entries] == [
        date(2025, 1, 2),
        date(2025, 1, 2),
    ]


def test_market_data_maps_bars_with_dollar_volume(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    context = _context(root)

    bars = context.market_data("AAA", AS_OF)

    assert len(bars) == 30
    assert bars[-1].bar_date <= AS_OF
    assert bars[-1].dollar_volume == "10000000.00"


def test_screen_runs_indicator_and_classification_filters_end_to_end(
    tmp_path: Path,
) -> None:
    root = _build_root(tmp_path)
    context = _context(
        root,
        bars={
            "AAA": _trending_bars("down"),
            "BBB": _trending_bars("up"),
        },
    )
    definition = ScreenerDefinition(
        name="oversold-stocks",
        universe_revision="universe:test",
        filters=(
            {"kind": "security_type", "params": {"value": "stock"}},
            {"kind": "rsi_below", "params": {"period": 14, "threshold": 40}},
        ),
        lookback_days=20,
    )

    result = run_screener(definition, context, as_of=AS_OF)

    assert [hit.symbol for hit in result.hits] == ["AAA"]
    metrics = result.hits[0].metrics
    assert metrics["security_type"] == "stock"
    assert metrics["ind:rsi(period=14)"] == "0"


def test_indicator_filters_fail_closed_without_the_port(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    context = _context(root)

    class BareContext:
        def universe(self, as_of: date) -> tuple[UniverseEntry, ...]:
            return context.universe(as_of)

        def market_data(self, symbol: str, window_end: date) -> tuple[MarketBar, ...]:
            return context.market_data(symbol, window_end)

    definition = ScreenerDefinition(
        name="technical",
        universe_revision="universe:test",
        filters=({"kind": "rsi_below", "params": {"period": 14, "threshold": 30}},),
    )

    with pytest.raises(ScreenerError, match="indicator_value port"):
        run_screener(definition, BareContext(), as_of=AS_OF)


def test_fundamentals_filters_fail_closed_without_the_port(tmp_path: Path) -> None:
    root = _build_root(tmp_path)
    context = _context(root)

    class BareContext:
        def universe(self, as_of: date) -> tuple[UniverseEntry, ...]:
            return context.universe(as_of)

        def market_data(self, symbol: str, window_end: date) -> tuple[MarketBar, ...]:
            return context.market_data(symbol, window_end)

    definition = ScreenerDefinition(
        name="profitable",
        universe_revision="universe:test",
        filters=(
            {"kind": "fundamentals_min", "params": {"field": "revenue", "min": 1}},
        ),
    )

    with pytest.raises(ScreenerError, match="fundamentals port"):
        run_screener(definition, BareContext(), as_of=AS_OF)
