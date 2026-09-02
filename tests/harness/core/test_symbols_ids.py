from decimal import Decimal

import pytest

from harness.core.ids import EngineIdSource, deterministic_id
from harness.core.symbols import OsiSymbol, Symbol
from harness.core.types import symbols_from_parquet, symbols_to_parquet


def test_symbols_are_canonical_and_immutable() -> None:
    symbol = Symbol(" aapl ")

    assert symbol == "AAPL"
    with pytest.raises(ValueError):
        Symbol("../AAPL")


def test_osi_symbol_parses_unpadded_and_padded_roots() -> None:
    symbol = OsiSymbol("AAPL240119C00150000")
    padded = OsiSymbol("AAPL  240119C00150000")

    assert symbol == padded == "AAPL240119C00150000"
    assert symbol.root == "AAPL"
    assert symbol.expiration == "240119"
    assert symbol.option_type == "C"
    assert symbol.strike == Decimal("150")


def test_realistic_osi_symbols_round_trip_through_parquet() -> None:
    symbols = tuple(
        OsiSymbol(f"{root}{expiration}{right}{strike:08d}")
        for root in ("AAPL", "MSFT", "SPY", "QQQ", "TSLA")
        for expiration in (
            "240119",
            "240216",
            "240315",
            "240419",
            "240517",
            "240621",
            "240719",
            "240816",
            "240920",
            "241018",
        )
        for right, strike in (("C", 150000), ("P", 150000))
    )

    restored = symbols_from_parquet(symbols_to_parquet(symbols))

    assert len(symbols) == 100
    assert restored == symbols
    assert all(isinstance(symbol, OsiSymbol) for symbol in restored)


def test_seeded_ids_repeat_without_using_global_state() -> None:
    first = EngineIdSource("run-17")
    second = EngineIdSource("run-17")

    assert [first.next_id("order") for _ in range(4)] == [
        second.next_id("order") for _ in range(4)
    ]
    source = EngineIdSource("run-17")
    source.next_id("order")
    source.next_id("order")
    assert deterministic_id("run-17", "order", 2) == source.next_id("order")


def test_id_source_rejects_missing_seed_and_bool() -> None:
    with pytest.raises(ValueError):
        EngineIdSource("")
    with pytest.raises(TypeError):
        EngineIdSource(True)
