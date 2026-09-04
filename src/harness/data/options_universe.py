"""Point-in-time option contract universe with hard look-ahead clipping.

The universe answers one question per engine date: which option contracts on
an underlying are selectable as of that date.  Membership is loaded from the
market-lake contracts catalog without ever consulting catalog rows dated
after the engine date:

- ``front_set`` mode reads exactly the daily front-set record keyed by the
  engine date, so membership rolls only when the catalog itself rolled and
  future front-set rows are never opened.
- ``static_osi`` mode reads named OSI symbols and requires their catalog
  metadata to prove visibility on the engine date.

Every resolved contract is then clipped to the engine date: contracts that
have not listed yet, have already delisted, or expired before the engine
date are invisible no matter what the catalog contains.  A contract remains
selectable through its expiration date and is retired from the following
day.  DTE, strike, and moneyness filters are evaluated against the engine
date only -- DTE from the engine date, moneyness from the underlying
reference price supplied for the engine date.  Without an as-of reference
price a moneyness filter cannot be evaluated, so no contract can prove
itself selectable and the snapshot is empty.

Like :mod:`harness.data.history`, no code path here reads wall time,
consumes randomness, touches the network, or performs I/O: the as-of instant
comes from the injected engine clock or an explicit argument, and engine
dates are the UTC calendar dates of those instants.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, cast, runtime_checkable

from harness.core.clock import Clock
from harness.core.symbols import Symbol, canonical_osi_symbol
from harness.core.types.common import DecimalInput, decimal
from harness.data.history import LookaheadError, Nanos, from_nanos, to_nanos
from lake.catalog.front_sets import DailyFrontSet
from lake.catalog.models import ContractRecord

_ZERO: Final = Decimal("0")


class SelectionMode(StrEnum):
    """How daily membership is sourced from the contracts catalog."""

    FRONT_SET = "front_set"
    STATIC_OSI = "static_osi"


def _contract_sort_key(record: ContractRecord) -> tuple[date, Decimal, str, str]:
    """Return the deterministic ordering shared with the lake front sets."""
    return (
        record.expiration_date,
        record.strike_price,
        record.option_type.value,
        record.symbol,
    )


def _is_visible(record: ContractRecord, underlying: str, as_of: date) -> bool:
    """Return whether the contract is selectable on the engine date."""
    return (
        record.underlying == underlying
        and as_of <= record.expiration_date
        and (record.listed_date is None or as_of >= record.listed_date)
        and (record.delisted_date is None or as_of <= record.delisted_date)
    )


@dataclass(frozen=True, slots=True)
class DteFilter:
    """Inclusive calendar-DTE bounds evaluated on the engine date."""

    min_days: int | None = None
    max_days: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("min_days", "max_days"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if (
            self.min_days is not None
            and self.max_days is not None
            and self.min_days > self.max_days
        ):
            raise ValueError("min_days must not exceed max_days")

    def matches(self, dte: int) -> bool:
        """Return whether ``dte`` lies inside the inclusive bounds."""
        low = self.min_days
        high = self.max_days
        return (low is None or dte >= low) and (high is None or dte <= high)


@dataclass(frozen=True, slots=True)
class StrikeFilter:
    """Inclusive absolute strike bounds."""

    min_strike: DecimalInput | None = None
    max_strike: DecimalInput | None = None

    def __post_init__(self) -> None:
        low = (
            None
            if self.min_strike is None
            else decimal(self.min_strike, "min_strike", positive=True)
        )
        high = (
            None
            if self.max_strike is None
            else decimal(self.max_strike, "max_strike", positive=True)
        )
        if low is not None and high is not None and low > high:
            raise ValueError("min_strike must not exceed max_strike")
        object.__setattr__(self, "min_strike", low)
        object.__setattr__(self, "max_strike", high)

    def matches(self, strike: Decimal) -> bool:
        """Return whether ``strike`` lies inside the inclusive bounds."""
        low = cast("Decimal | None", self.min_strike)
        high = cast("Decimal | None", self.max_strike)
        return (low is None or strike >= low) and (high is None or strike <= high)


@dataclass(frozen=True, slots=True)
class MoneynessFilter:
    """Inclusive strike-to-reference-price ratio bounds.

    Moneyness is ``strike / reference_price``: values above one sit above the
    underlying reference price.  Bounds are compared by cross-multiplication
    with the positive reference price, so the check stays exact instead of
    rounding through a division.
    """

    min_moneyness: DecimalInput | None = None
    max_moneyness: DecimalInput | None = None

    def __post_init__(self) -> None:
        low = (
            None
            if self.min_moneyness is None
            else decimal(self.min_moneyness, "min_moneyness", positive=True)
        )
        high = (
            None
            if self.max_moneyness is None
            else decimal(self.max_moneyness, "max_moneyness", positive=True)
        )
        if low is not None and high is not None and low > high:
            raise ValueError("min_moneyness must not exceed max_moneyness")
        object.__setattr__(self, "min_moneyness", low)
        object.__setattr__(self, "max_moneyness", high)

    def matches(self, strike: Decimal, reference_price: Decimal) -> bool:
        """Return whether ``strike / reference_price`` is inside the bounds."""
        low = cast("Decimal | None", self.min_moneyness)
        high = cast("Decimal | None", self.max_moneyness)
        return (low is None or strike >= low * reference_price) and (
            high is None or strike <= high * reference_price
        )


@runtime_checkable
class UnderlyingView(Protocol):
    """Point-in-time reference data for an option's underlying instrument."""

    def is_listed(self, underlying: str, as_of: date) -> bool:
        """Return whether the underlying had an active listing on ``as_of``."""

    def reference_price(self, underlying: str, as_of: date) -> Decimal | None:
        """Return the underlying reference price known on ``as_of``, if any."""


@runtime_checkable
class ContractsCatalogPort(Protocol):
    """Read boundary over the market-lake contracts catalog."""

    def get(self, symbol: str) -> ContractRecord | None:
        """Return contract metadata by OSI symbol."""

    def get_front_set(
        self, underlying: str, selection_date: date
    ) -> DailyFrontSet | None:
        """Return the persisted front set for one underlying and date."""


@dataclass(frozen=True, slots=True)
class OptionUniverseSpec:
    """Validated selection configuration for one option universe."""

    underlying: str
    mode: SelectionMode
    static_symbols: Sequence[str] = ()
    dte: DteFilter | None = None
    strike: StrikeFilter | None = None
    moneyness: MoneynessFilter | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", Symbol(self.underlying))
        object.__setattr__(self, "mode", SelectionMode(self.mode))
        if self.dte is not None and not isinstance(self.dte, DteFilter):
            raise TypeError("dte must be a DteFilter")
        if self.strike is not None and not isinstance(self.strike, StrikeFilter):
            raise TypeError("strike must be a StrikeFilter")
        if self.moneyness is not None and not isinstance(
            self.moneyness, MoneynessFilter
        ):
            raise TypeError("moneyness must be a MoneynessFilter")
        symbols = tuple(canonical_osi_symbol(symbol) for symbol in self.static_symbols)
        if len(set(symbols)) != len(symbols):
            raise ValueError("static_symbols must not contain duplicate OSI symbols")
        if self.mode is SelectionMode.STATIC_OSI and not symbols:
            raise ValueError("static OSI mode requires at least one static symbol")
        if self.mode is SelectionMode.FRONT_SET and symbols:
            raise ValueError("front-set mode must not name static symbols")
        object.__setattr__(self, "static_symbols", symbols)


@dataclass(frozen=True, slots=True)
class OptionUniverseSnapshot:
    """The option contracts selectable for one underlying on one engine date.

    Construction is the structural no-look-ahead chokepoint: records for a
    different underlying, not yet listed, already delisted, or expired before
    ``as_of`` are clipped here even if a caller passes them directly.
    """

    underlying: str
    as_of: date
    mode: SelectionMode
    contracts: tuple[ContractRecord, ...]
    _by_expiry: Mapping[date, tuple[ContractRecord, ...]] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if isinstance(self.as_of, datetime):
            raise TypeError("as_of must be a date, not a datetime")
        underlying = str(Symbol(self.underlying))
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "mode", SelectionMode(self.mode))
        contracts = tuple(
            record
            for record in sorted(self.contracts, key=_contract_sort_key)
            if _is_visible(record, underlying, self.as_of)
        )
        object.__setattr__(self, "contracts", contracts)
        grouped: dict[date, list[ContractRecord]] = {}
        for record in contracts:
            grouped.setdefault(record.expiration_date, []).append(record)
        object.__setattr__(
            self,
            "_by_expiry",
            MappingProxyType(
                {expiry: tuple(members) for expiry, members in grouped.items()}
            ),
        )

    @property
    def expiries(self) -> tuple[date, ...]:
        """Return selectable expiration dates in ascending order."""
        return tuple(self._by_expiry)

    @property
    def front_expiry(self) -> date | None:
        """Return the earliest selectable expiration date, if any."""
        return self.expiries[0] if self._by_expiry else None

    @property
    def front(self) -> tuple[ContractRecord, ...]:
        """Return the selectable contracts at the front expiration."""
        expiry = self.front_expiry
        return () if expiry is None else self._by_expiry[expiry]

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return selected OSI symbols in deterministic order."""
        return tuple(record.symbol for record in self.contracts)

    def by_expiry(self) -> Mapping[date, tuple[ContractRecord, ...]]:
        """Return the expiry-aware index used for resolution and retirement."""
        return self._by_expiry

    def at_expiry(self, expiry: date) -> tuple[ContractRecord, ...]:
        """Return the contracts expiring on ``expiry``, in sorted order."""
        if isinstance(expiry, datetime):
            raise TypeError("expiry must be a date, not a datetime")
        return self._by_expiry.get(expiry, ())

    def dte(self, expiry: date) -> int:
        """Return calendar days from ``as_of`` to ``expiry``."""
        if isinstance(expiry, datetime):
            raise TypeError("expiry must be a date, not a datetime")
        return (expiry - self.as_of).days

    def contract(self, symbol: str) -> ContractRecord | None:
        """Return one selected contract by OSI symbol, if selected."""
        if not isinstance(symbol, str):
            raise TypeError("symbol must be a string")
        target = symbol.strip().upper()
        return next(
            (record for record in self.contracts if record.symbol == target), None
        )

    def __len__(self) -> int:
        return len(self.contracts)

    def __iter__(self) -> Iterator[ContractRecord]:
        return iter(self.contracts)

    def __contains__(self, symbol: object) -> bool:
        if not isinstance(symbol, str):
            return False
        target = symbol.strip().upper()
        return any(record.symbol == target for record in self.contracts)


class OptionUniverseView:
    """Point-in-time option contract universe clipped to the engine clock.

    The view reads only the market-lake contracts catalog injected at
    construction and never reads beyond the clock head.  Resolving at an
    ``as_of`` newer than the head raises :class:`LookaheadError` before any
    catalog data is touched, mirroring the historical data boundary.
    """

    __slots__ = ("_catalog", "_clock", "_spec", "_underlyings")

    def __init__(
        self,
        clock: Clock,
        catalog: ContractsCatalogPort,
        spec: OptionUniverseSpec,
        *,
        underlyings: UnderlyingView | None = None,
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not isinstance(catalog, ContractsCatalogPort):
            raise TypeError("catalog must implement ContractsCatalogPort")
        if not isinstance(spec, OptionUniverseSpec):
            raise TypeError("spec must be an OptionUniverseSpec")
        if spec.moneyness is not None:
            if underlyings is None:
                raise ValueError("moneyness filters require an underlying view")
            if not isinstance(underlyings, UnderlyingView):
                raise TypeError("underlyings must implement UnderlyingView")
        self._clock = clock
        self._catalog = catalog
        self._spec = spec
        self._underlyings = underlyings

    @property
    def clock(self) -> Clock:
        """Return the injected engine clock."""
        return self._clock

    @property
    def spec(self) -> OptionUniverseSpec:
        """Return the validated selection configuration."""
        return self._spec

    @property
    def head(self) -> datetime:
        """Return the current engine clock head, the hard read boundary."""
        return self._clock.now()

    def resolve(self, as_of: datetime | Nanos | None = None) -> OptionUniverseSnapshot:
        """Return the selectable contracts as of ``as_of``, clipped to the head."""
        head = to_nanos(self._clock.now(), "clock")
        requested = head if as_of is None else to_nanos(as_of, "as_of")
        if requested > head:
            raise LookaheadError(
                f"as_of {requested} is newer than the engine clock head {head}"
            )
        engine_date = from_nanos(requested).date()
        spec = self._spec
        underlying = str(spec.underlying)
        if self._underlyings is not None and not self._underlyings.is_listed(
            underlying, engine_date
        ):
            contracts: tuple[ContractRecord, ...] = ()
        else:
            contracts = self._filtered(self._members(engine_date), engine_date)
        return OptionUniverseSnapshot(
            underlying=underlying,
            as_of=engine_date,
            mode=spec.mode,
            contracts=contracts,
        )

    def _members(self, engine_date: date) -> tuple[ContractRecord, ...]:
        """Return raw membership for the engine date from the catalog."""
        spec = self._spec
        underlying = str(spec.underlying)
        if spec.mode is SelectionMode.FRONT_SET:
            front_set = self._catalog.get_front_set(underlying, engine_date)
            return () if front_set is None else tuple(front_set.contracts)
        members: list[ContractRecord] = []
        for symbol in spec.static_symbols:
            record = self._catalog.get(symbol)
            if record is not None and record.underlying == underlying:
                members.append(record)
        return tuple(members)

    def _filtered(
        self, members: tuple[ContractRecord, ...], engine_date: date
    ) -> tuple[ContractRecord, ...]:
        """Apply the as-of DTE, strike, and moneyness filters in order."""
        spec = self._spec
        dte = spec.dte
        strike = spec.strike
        moneyness = spec.moneyness
        reference: Decimal | None = None
        if moneyness is not None:
            assert self._underlyings is not None
            reference = self._underlyings.reference_price(
                str(spec.underlying), engine_date
            )
            if reference is None or reference <= _ZERO:
                return ()
        selected: list[ContractRecord] = []
        for record in members:
            if dte is not None and not dte.matches(
                (record.expiration_date - engine_date).days
            ):
                continue
            if strike is not None and not strike.matches(record.strike_price):
                continue
            if moneyness is not None and reference is not None:
                if not moneyness.matches(record.strike_price, reference):
                    continue
            selected.append(record)
        return tuple(selected)


__all__ = [
    "ContractsCatalogPort",
    "DteFilter",
    "LookaheadError",
    "MoneynessFilter",
    "Nanos",
    "OptionUniverseSnapshot",
    "OptionUniverseSpec",
    "OptionUniverseView",
    "SelectionMode",
    "StrikeFilter",
    "UnderlyingView",
]
