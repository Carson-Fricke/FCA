"""Nasdaq Trader symbol directory snapshot parsing.

The daily ``nasdaqtraded.txt`` file is a pipe-delimited directory whose final
record carries the file creation timestamp. That timestamp is the point-in-time
knowledge key: each snapshot is archived as its own partition dated by the
creation timestamp's US/Eastern calendar date.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

_MARKET_TIME_ZONE: Final = ZoneInfo("America/New_York")

_HEADER: Final = (
    "Nasdaq Traded",
    "Symbol",
    "Security Name",
    "Listing Exchange",
    "Market Category",
    "ETF",
    "Round Lot Size",
    "Test Issue",
    "Financial Status",
    "CQS Symbol",
    "NASDAQ Symbol",
    "NextShares",
)

_FILE_CREATION_LABEL: Final = "File Creation Time"

_FILE_CREATION_TIME: Final = r"(\d{2})(\d{2})(\d{4})(\d{2}):?(\d{2})##?"

_FIELD_COUNT: Final = len(_HEADER)


class NasdaqParseError(ValueError):
    """The symbol directory payload is not a parsable nasdaqtraded.txt file."""


@dataclass(frozen=True, slots=True)
class NasdaqSecurity:
    """One security row of a Nasdaq Trader symbol directory snapshot."""

    symbol: str
    security_name: str
    listing_exchange: str
    market_category: str
    etf: bool
    round_lot_size: Decimal
    test_issue: bool
    financial_status: str | None


@dataclass(frozen=True, slots=True)
class NasdaqSnapshot:
    """One parsed symbol directory snapshot and its knowledge instant."""

    file_creation_time: datetime
    snapshot_date: date
    securities: tuple[NasdaqSecurity, ...]


def parse_nasdaq_traded(payload: bytes) -> NasdaqSnapshot:
    """Parse a nasdaqtraded.txt payload into securities plus creation instant."""
    lines = [line for line in payload.decode("utf-8-sig").splitlines() if line.strip()]
    if len(lines) < 3:
        raise NasdaqParseError(
            "nasdaqtraded payload must contain a header, securities, and creation line"
        )
    header = tuple(field.strip() for field in lines[0].split("|"))
    if header != _HEADER:
        raise NasdaqParseError(f"unexpected nasdaqtraded header: {lines[0]!r}")

    footer = lines[-1].split("|")
    if len(footer) != 2 or footer[0].strip() != _FILE_CREATION_LABEL:
        raise NasdaqParseError(f"missing file creation line: {lines[-1]!r}")
    creation_time = _parse_file_creation_time(footer[1].strip())

    securities: list[NasdaqSecurity] = []
    for line_number, line in enumerate(lines[1:-1], start=2):
        fields = line.split("|")
        if len(fields) != _FIELD_COUNT:
            raise NasdaqParseError(
                f"nasdaqtraded line {line_number} has {len(fields)} of "
                f"{_FIELD_COUNT} fields"
            )
        securities.append(_parse_security(fields, line_number))

    ordered = tuple(sorted(securities, key=lambda security: security.symbol))
    return NasdaqSnapshot(
        file_creation_time=creation_time,
        snapshot_date=creation_time.date(),
        securities=ordered,
    )


def _parse_security(fields: list[str], line_number: int) -> NasdaqSecurity:
    (
        _nasdaq_traded,
        symbol,
        security_name,
        listing_exchange,
        market_category,
        etf,
        round_lot_size,
        test_issue,
        financial_status,
        _cqs_symbol,
        _nasdaq_symbol,
        _next_shares,
    ) = fields
    if not symbol.strip():
        raise NasdaqParseError(f"nasdaqtraded line {line_number} has an empty symbol")
    if not security_name.strip():
        raise NasdaqParseError(
            f"nasdaqtraded line {line_number} has an empty security name"
        )
    try:
        round_lot = Decimal(round_lot_size.strip())
    except (InvalidOperation, ValueError) as error:
        raise NasdaqParseError(
            f"nasdaqtraded line {line_number} has an invalid round lot size: "
            f"{round_lot_size!r}"
        ) from error
    if not round_lot.is_finite() or round_lot <= 0:
        raise NasdaqParseError(
            f"nasdaqtraded line {line_number} round lot size must be positive: "
            f"{round_lot_size!r}"
        )
    status = financial_status.strip()
    return NasdaqSecurity(
        symbol=symbol.strip(),
        security_name=security_name.strip(),
        listing_exchange=listing_exchange.strip(),
        market_category=market_category.strip(),
        etf=_parse_flag(etf, line_number),
        round_lot_size=round_lot,
        test_issue=_parse_flag(test_issue, line_number),
        financial_status=status or None,
    )


def _parse_flag(value: str, line_number: int) -> bool:
    flag = value.strip()
    if flag == "Y":
        return True
    if flag == "N":
        return False
    raise NasdaqParseError(
        f"nasdaqtraded line {line_number} flag must be Y or N: {value!r}"
    )


def _parse_file_creation_time(value: str) -> datetime:
    match = re.fullmatch(_FILE_CREATION_TIME, value)
    if match is None:
        raise NasdaqParseError(f"invalid file creation time: {value!r}")
    month, day, year, hour, minute = (int(part) for part in match.groups())
    try:
        created = datetime(year, month, day, hour, minute, tzinfo=_MARKET_TIME_ZONE)
    except ValueError as error:
        raise NasdaqParseError(f"invalid file creation time: {value!r}") from error
    return created.astimezone(_MARKET_TIME_ZONE)


__all__ = [
    "NasdaqParseError",
    "NasdaqSecurity",
    "NasdaqSnapshot",
    "parse_nasdaq_traded",
]
