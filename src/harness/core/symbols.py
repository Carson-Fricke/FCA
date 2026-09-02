"""Immutable, canonical market-symbol value objects.

The core accepts symbols from feeds and strategy code, but never lets a
provider-specific spelling leak into deterministic state.  Option symbols use
the OCC/OSI compact representation without the six-character root padding.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Final

_MARKET_SYMBOL: Final = re.compile(r"^[A-Z0-9][A-Z0-9./:_-]*$")
_OSI_SYMBOL: Final = re.compile(
    r"^(?P<root>[A-Z0-9.]{1,6})(?P<expiration>\d{6})"
    r"(?P<right>[CP])(?P<strike>\d{8})$"
)


class Symbol(str):
    """A canonical, immutable market symbol.

    ``Symbol`` deliberately subclasses ``str`` so it remains convenient as a
    dictionary key and as a PyArrow string value.  Construction is the only
    normalization point; instances cannot be mutated afterward.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Symbol:
        if not isinstance(value, str):
            raise TypeError("symbol must be a string")
        canonical = value.strip().upper()
        if not canonical:
            raise ValueError("symbol must not be empty")
        if not _MARKET_SYMBOL.fullmatch(canonical):
            raise ValueError(f"invalid market symbol: {value!r}")
        return str.__new__(cls, canonical)

    @property
    def value(self) -> str:
        """Return the canonical string value."""
        return str(self)


class OsiSymbol(Symbol):
    """A canonical OCC/OSI option symbol.

    Both the usual unpadded form (``AAPL240119C00150000``) and the raw OSI
    form with spaces in the six-character root are accepted.  The stored value
    always uses the unpadded form.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> OsiSymbol:
        if not isinstance(value, str):
            raise TypeError("OSI symbol must be a string")
        candidate = value.strip().upper()
        if len(candidate) >= 21 and " " in candidate[:6]:
            candidate = candidate[:6].rstrip() + candidate[6:]
        if _OSI_SYMBOL.fullmatch(candidate) is None:
            raise ValueError(f"invalid OSI symbol: {value!r}")
        return str.__new__(cls, candidate)

    @property
    def root(self) -> str:
        """Return the underlying root symbol."""
        match = _OSI_SYMBOL.fullmatch(self)
        assert match is not None
        return match.group("root")

    @property
    def expiration(self) -> str:
        """Return expiration as ``YYMMDD``."""
        match = _OSI_SYMBOL.fullmatch(self)
        assert match is not None
        return match.group("expiration")

    @property
    def expiration_date(self) -> date:
        """Return expiration as a calendar date without consulting the clock."""
        return datetime.strptime(self.expiration, "%y%m%d").date()

    @property
    def option_type(self) -> str:
        """Return ``C`` or ``P``."""
        match = _OSI_SYMBOL.fullmatch(self)
        assert match is not None
        return match.group("right")

    @property
    def right(self) -> str:
        """Return ``C`` or ``P`` using standard option terminology."""
        return self.option_type

    @property
    def strike(self) -> Decimal:
        """Return the OSI strike, whose wire scale is three decimals."""
        match = _OSI_SYMBOL.fullmatch(self)
        assert match is not None
        return Decimal(match.group("strike")).scaleb(-3)


MarketSymbol = Symbol
OSISymbol = OsiSymbol
OptionSymbol = OsiSymbol


def canonical_symbol(value: str | Symbol) -> Symbol:
    """Return a canonical symbol without accepting binary floating point."""
    if isinstance(value, OsiSymbol):
        return value
    raw = str(value) if isinstance(value, Symbol) else value
    try:
        return OsiSymbol(raw)
    except ValueError:
        return value if isinstance(value, Symbol) else Symbol(raw)


def is_osi_symbol(value: str | Symbol) -> bool:
    """Return whether ``value`` is a valid compact or padded OSI symbol."""
    try:
        OsiSymbol(value)
    except (TypeError, ValueError):
        return False
    return True


def canonical_osi_symbol(value: str | OsiSymbol) -> OsiSymbol:
    """Return a canonical OSI option symbol."""
    return OsiSymbol(value) if not isinstance(value, OsiSymbol) else value


__all__ = [
    "MarketSymbol",
    "OSISymbol",
    "OptionSymbol",
    "OsiSymbol",
    "Symbol",
    "canonical_osi_symbol",
    "canonical_symbol",
    "is_osi_symbol",
]
