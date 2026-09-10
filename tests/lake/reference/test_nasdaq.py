from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lake.reference.nasdaq import (
    NasdaqParseError,
    parse_nasdaq_traded,
)

FIXTURES = Path(__file__).parent / "fixtures"
NASDAQ_TRADED = FIXTURES / "nasdaqtraded.txt"

_HEADER = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
    "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares"
)

_AAPL_ROW = "Y|AAPL|Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL|N"

_CREATED = "0419202417:20##"


def _payload(*rows: str) -> bytes:
    lines = (_HEADER, *rows, f"File Creation Time|{_CREATED}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_golden_snapshot_parses_securities_and_creation_instant() -> None:
    snapshot = parse_nasdaq_traded(NASDAQ_TRADED.read_bytes())

    assert snapshot.snapshot_date == date(2024, 4, 19)
    assert snapshot.file_creation_time == datetime(
        2024, 4, 19, 17, 20, tzinfo=ZoneInfo("America/New_York")
    )
    assert snapshot.file_creation_time.astimezone(UTC) == datetime(
        2024, 4, 19, 21, 20, tzinfo=UTC
    )
    assert [security.symbol for security in snapshot.securities] == [
        "AAPL",
        "GOOG",
        "MSFT",
        "ZVZZT",
    ]


def test_golden_snapshot_security_fields_are_exact() -> None:
    snapshot = parse_nasdaq_traded(NASDAQ_TRADED.read_bytes())
    by_symbol = {security.symbol: security for security in snapshot.securities}

    aapl = by_symbol["AAPL"]
    assert aapl.security_name == "Apple Inc. Common Stock"
    assert aapl.listing_exchange == "Q"
    assert aapl.market_category == "Q"
    assert aapl.etf is False
    assert aapl.round_lot_size == Decimal("100")
    assert aapl.test_issue is False
    assert aapl.financial_status == "Q"

    goog = by_symbol["GOOG"]
    assert goog.listing_exchange == "N"
    assert goog.financial_status is None
    assert goog.etf is False
    assert goog.test_issue is False

    zvzzt = by_symbol["ZVZZT"]
    assert zvzzt.test_issue is True
    assert zvzzt.financial_status is None


def test_rows_are_sorted_by_symbol_regardless_of_feed_order() -> None:
    snapshot = parse_nasdaq_traded(
        _payload(
            "Y|ZZZZ|Zed Security|Q|Q|N|100|N||ZZZZ|ZZZZ|N",
            "Y|AAAA|Alpha Security|Q|Q|N|100|N||AAAA|AAAA|N",
        )
    )

    assert [security.symbol for security in snapshot.securities] == ["AAAA", "ZZZZ"]


def test_file_creation_time_accepts_compact_and_single_hash_variants() -> None:
    for footer in ("0419202417:20##", "0419202417:20#", "041920241720##"):
        snapshot = parse_nasdaq_traded(
            f"{_HEADER}\n{_AAPL_ROW}\nFile Creation Time|{footer}\n".encode()
        )

        assert snapshot.file_creation_time == datetime(
            2024, 4, 19, 17, 20, tzinfo=ZoneInfo("America/New_York")
        )


def test_file_creation_time_requires_the_hash_terminator() -> None:
    for footer in ("0419202417:20", "0419202417:20###"):
        payload = f"{_HEADER}\n{_AAPL_ROW}\nFile Creation Time|{footer}\n".encode()

        with pytest.raises(NasdaqParseError, match="invalid file creation time"):
            parse_nasdaq_traded(payload)


def test_utf8_bom_is_tolerated() -> None:
    payload = f"\ufeff{_HEADER}\n{_AAPL_ROW}\nFile Creation Time|{_CREATED}\n".encode()

    snapshot = parse_nasdaq_traded(payload)

    assert [security.symbol for security in snapshot.securities] == ["AAPL"]


def test_blank_lines_are_ignored() -> None:
    payload = (
        f"{_HEADER}\n\n{_AAPL_ROW}\n\nFile Creation Time|{_CREATED}\n\n"
    ).encode()

    snapshot = parse_nasdaq_traded(payload)

    assert [security.symbol for security in snapshot.securities] == ["AAPL"]


def test_unexpected_header_is_rejected() -> None:
    payload = (
        "Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
        "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|"
        "NextShares\n"
        f"{_AAPL_ROW}\n"
        f"File Creation Time|{_CREATED}\n"
    ).encode()

    with pytest.raises(NasdaqParseError, match="unexpected nasdaqtraded header"):
        parse_nasdaq_traded(payload)


def test_missing_file_creation_line_is_rejected() -> None:
    payload = f"{_HEADER}\n{_AAPL_ROW}\n{_AAPL_ROW}\n".encode()

    with pytest.raises(NasdaqParseError, match="missing file creation line"):
        parse_nasdaq_traded(payload)


def test_malformed_file_creation_time_is_rejected() -> None:
    for footer in (
        "2024-04-19 17:20##",
        "1320240317:20##",
        "0419202417##",
        "garbage",
        "",
    ):
        payload = (f"{_HEADER}\n{_AAPL_ROW}\nFile Creation Time|{footer}\n").encode()

        with pytest.raises(NasdaqParseError, match="invalid file creation time"):
            parse_nasdaq_traded(payload)


def test_row_with_wrong_field_count_is_rejected() -> None:
    payload = _payload("Y|AAPL|Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL")

    with pytest.raises(NasdaqParseError, match="line 2 has 11 of 12 fields"):
        parse_nasdaq_traded(payload)


def test_row_with_invalid_flag_is_rejected() -> None:
    payload = _payload("Y|AAPL|Apple Inc. Common Stock|Q|Q|yes|100|N|Q|AAPL|AAPL|N")

    with pytest.raises(NasdaqParseError, match="flag must be Y or N"):
        parse_nasdaq_traded(payload)


def test_row_with_invalid_round_lot_size_is_rejected() -> None:
    for round_lot in ("0", "-100", "many"):
        payload = _payload(
            f"Y|AAPL|Apple Inc. Common Stock|Q|Q|N|{round_lot}|N|Q|AAPL|AAPL|N"
        )

        with pytest.raises(NasdaqParseError, match="round lot size"):
            parse_nasdaq_traded(payload)


def test_row_with_empty_symbol_or_name_is_rejected() -> None:
    for line in (
        "Y||Apple Inc. Common Stock|Q|Q|N|100|N|Q|AAPL|AAPL|N",
        "Y|AAPL||Q|Q|N|100|N|Q|AAPL|AAPL|N",
    ):
        with pytest.raises(NasdaqParseError, match="empty"):
            parse_nasdaq_traded(_payload(line))


def test_payload_with_fewer_than_three_lines_is_rejected() -> None:
    for payload in (b"", _HEADER.encode("utf-8")):
        with pytest.raises(NasdaqParseError, match="header, securities"):
            parse_nasdaq_traded(payload)
