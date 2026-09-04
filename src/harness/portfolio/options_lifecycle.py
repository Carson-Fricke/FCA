"""Deterministic option expiry, exercise, and assignment settlement.

The lifecycle derives contract terms from canonical OSI symbols, takes all
timing from an injected clock, and settles physical equity-style exercise and
assignment as exact underlying fills at the strike. Option fills are accounted
exactly as applied, so producers must quote per-contract notional prices
(per-share quote times the contract multiplier) for real-dollar cash
exactness; ``multiplier`` converts contracts to underlying shares at
settlement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from harness.core.clock import Clock
from harness.core.symbols import OsiSymbol, Symbol, canonical_symbol
from harness.core.types.accounting import Fill
from harness.core.types.common import DecimalInput, exact_decimal
from harness.core.types.orders import Side
from harness.portfolio.book import PortfolioError, PositionSnapshot, VirtualBook
from harness.portfolio.hub import PortfolioHub

_ZERO = Decimal("0")


class OptionLifecycleError(PortfolioError):
    """Base class for deterministic option-settlement failures."""


class ExpiredContractError(OptionLifecycleError):
    """Raised when a retired or expired contract is traded again."""


class MissingUnderlyingPriceError(OptionLifecycleError):
    """Raised when expiry settlement lacks an exact underlying spot."""


class InvalidAssignmentError(OptionLifecycleError):
    """Raised when an assignment policy returns an unusable quantity."""


class SettlementKind(StrEnum):
    """How one contract position ended at expiration."""

    EXERCISED = "exercised"
    ASSIGNED = "assigned"
    WORTHLESS = "worthless"


@runtime_checkable
class AssignmentPolicy(Protocol):
    """Portion of an expiring short contract position that is assigned."""

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        """Return the exact contract quantity assigned at expiration."""
        ...


class PessimisticAssignment:
    """Assign one hundred percent of ITM short contracts and nothing else."""

    __slots__ = ()

    def assigned_quantity(
        self, contract: OsiSymbol, spot: Decimal, short_quantity: Decimal
    ) -> Decimal:
        """Return every short contract when ITM and none otherwise."""
        if intrinsic_value(contract, spot) > _ZERO:
            return short_quantity
        return _ZERO


def intrinsic_value(contract: OsiSymbol, spot: DecimalInput) -> Decimal:
    """Return the exact per-share intrinsic value of ``contract`` at ``spot``."""
    price = exact_decimal(spot, "spot")
    if price <= _ZERO:
        raise ValueError("spot must be positive")
    strike = contract.strike
    if contract.right == "C":
        return max(price - strike, _ZERO)
    return max(strike - price, _ZERO)


@dataclass(frozen=True, slots=True)
class DaysToExpiry:
    """One deterministic days-to-expiry notification for an open contract."""

    symbol: OsiSymbol
    days_to_expiry: int
    threshold: int
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class ContractSettlement:
    """Immutable result of settling one contract position in one book.

    ``contracts`` is the quantity that moved underlying shares through
    exercise or assignment and is zero for a worthless expiry.
    ``settlement_value`` is the exact cash applied at option-ledger
    retirement; it is zero under the default full-assignment policy and
    negative cash-settlement of any unassigned intrinsic value otherwise.
    """

    symbol: OsiSymbol
    book_id: str
    kind: SettlementKind
    contracts: Decimal
    spot: Decimal
    settlement_value: Decimal
    underlying_fill: Fill | None
    option_position: PositionSnapshot


def _as_osi(symbol: Symbol) -> OsiSymbol | None:
    """Return ``symbol`` as an OSI contract, or ``None`` for other symbols."""
    try:
        return OsiSymbol(str(symbol))
    except ValueError:
        return None


class OptionLifecycle:
    """Settle option exercise, assignment, and worthless expiry exactly.

    Long in-the-money contracts always exercise; short contracts settle
    through the injected :class:`AssignmentPolicy`, which defaults to a
    pessimistic one-hundred-percent ITM assignment. Contracts that are out of
    the money or pinned at the strike expire worthless. Retired contracts
    reject new fills and leave books with flat, fully realized ledgers.
    """

    __slots__ = (
        "_assignment_policy",
        "_clock",
        "_dte_thresholds",
        "_emitted_dte",
        "_hub",
        "_multiplier",
        "_retired",
        "_sequence",
        "_settlement_fee",
    )

    def __init__(
        self,
        hub: PortfolioHub,
        clock: Clock,
        *,
        multiplier: DecimalInput = 100,
        assignment_policy: AssignmentPolicy | None = None,
        dte_thresholds: Sequence[int] = (7, 1),
        settlement_fee: DecimalInput = 0,
    ) -> None:
        if not isinstance(hub, PortfolioHub):
            raise TypeError("hub must be a PortfolioHub")
        if not isinstance(clock, Clock):
            raise TypeError("clock must provide a timezone-aware now()")
        self._hub = hub
        self._clock = clock
        self._multiplier = exact_decimal(multiplier, "multiplier")
        if self._multiplier <= _ZERO:
            raise ValueError("multiplier must be positive")
        self._assignment_policy: AssignmentPolicy = (
            PessimisticAssignment() if assignment_policy is None else assignment_policy
        )
        thresholds: list[int] = []
        for threshold in dte_thresholds:
            if (
                isinstance(threshold, bool)
                or not isinstance(threshold, int)
                or threshold < 0
            ):
                raise ValueError("dte thresholds must be non-negative integers")
            if threshold not in thresholds:
                thresholds.append(threshold)
        self._dte_thresholds: tuple[int, ...] = tuple(sorted(thresholds, reverse=True))
        self._settlement_fee = exact_decimal(settlement_fee, "settlement_fee")
        if self._settlement_fee < _ZERO:
            raise ValueError("settlement_fee must not be negative")
        self._retired: dict[Symbol, datetime] = {}
        self._emitted_dte: dict[Symbol, set[int]] = {}
        self._sequence = 0

    @property
    def hub(self) -> PortfolioHub:
        """Return the portfolio hub that owns the settled books."""
        return self._hub

    @property
    def multiplier(self) -> Decimal:
        """Return the contracts-to-shares multiplier used at settlement."""
        return self._multiplier

    @property
    def assignment_policy(self) -> AssignmentPolicy:
        """Return the policy that decides short-contract assignment."""
        return self._assignment_policy

    @property
    def dte_thresholds(self) -> tuple[int, ...]:
        """Return configured days-to-expiry thresholds in emission order."""
        return self._dte_thresholds

    @property
    def retired_symbols(self) -> tuple[Symbol, ...]:
        """Return retired contracts in retirement order."""
        return tuple(self._retired)

    @property
    def open_contracts(self) -> tuple[OsiSymbol, ...]:
        """Return open OSI contracts across books in canonical symbol order."""
        return tuple(sorted(self._open_interest(), key=str))

    def is_retired(self, symbol: str | Symbol) -> bool:
        """Whether the contract has been settled and retired."""
        return canonical_symbol(symbol) in self._retired

    def days_to_expiry(self, symbol: str | Symbol) -> int:
        """Return whole calendar days from the clock date to expiration."""
        contract = self._require_osi(symbol)
        return (contract.expiration_date - self._clock.now().date()).days

    def ensure_tradable(self, symbol: str | Symbol) -> None:
        """Reject trading a retired contract or one already past expiration.

        Non-option symbols pass through unchanged, and contracts remain
        tradable on their expiration day until settlement retires them.
        """
        canonical = canonical_symbol(symbol)
        if canonical in self._retired:
            raise ExpiredContractError(
                f"contract {canonical.value!r} is retired and rejects new orders"
            )
        try:
            contract = self._require_osi(canonical)
        except ValueError:
            return
        if contract.expiration_date < self._clock.now().date():
            raise ExpiredContractError(
                f"contract {contract.value!r} expired on "
                f"{contract.expiration_date.isoformat()} and rejects new orders"
            )

    def apply_fill(self, book_id: str, fill: Fill) -> PositionSnapshot:
        """Route one fill through the hub, rejecting expired contracts first."""
        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        self.ensure_tradable(fill.symbol)
        return self._hub.apply_fill(book_id, fill)

    def poll_dte_events(self) -> tuple[DaysToExpiry, ...]:
        """Return and record new days-to-expiry events for open contracts.

        Each threshold event is emitted once per contract the first time the
        contract is observed at or below that many days to expiry. Contracts
        already past expiration are left to settlement and never emit events.
        """
        now = self._clock.now()
        today = now.date()
        events: list[DaysToExpiry] = []
        for contract in sorted(self._open_interest(), key=str):
            days = (contract.expiration_date - today).days
            if days < 0:
                continue
            emitted = self._emitted_dte.setdefault(contract, set())
            for threshold in self._dte_thresholds:
                if days <= threshold and threshold not in emitted:
                    emitted.add(threshold)
                    events.append(DaysToExpiry(contract, days, threshold, now))
        return tuple(events)

    def settle_expired(
        self, spots: Mapping[str | Symbol, DecimalInput]
    ) -> tuple[ContractSettlement, ...]:
        """Settle every at-or-past-expiration contract across all books.

        ``spots`` maps underlying root symbols to exact positive prices.
        Long ITM contracts exercise into exact underlying fills at the strike,
        short contracts settle through the assignment policy with any
        unassigned remainder cash-settled at intrinsic value, and out-of-the-
        money or pinned contracts expire worthless. Spots are validated for
        every expiring root before any book state changes, and settlement is
        idempotent because retired contracts no longer hold open interest.
        """
        now = self._clock.now()
        today = now.date()
        open_interest = {
            contract: holders
            for contract, holders in self._open_interest().items()
            if contract.expiration_date <= today
        }
        if not open_interest:
            return ()

        spot_by_root: dict[Symbol, Decimal] = {}
        for key, value in spots.items():
            spot = exact_decimal(value, "spot")
            if spot <= _ZERO:
                raise ValueError("spot must be positive")
            spot_by_root[Symbol(str(key))] = spot
        missing = sorted(
            contract.root
            for contract in open_interest
            if Symbol(contract.root) not in spot_by_root
        )
        if missing:
            raise MissingUnderlyingPriceError(
                "expiry settlement requires spots for underlying roots: "
                + ", ".join(missing)
            )

        settlements: list[ContractSettlement] = []
        for contract in sorted(open_interest, key=str):
            spot = spot_by_root[Symbol(contract.root)]
            for book, quantity in open_interest[contract]:
                settlements.append(self._settle(book, contract, quantity, spot, now))
            self._retired.setdefault(contract, now)
        return tuple(settlements)

    def _open_interest(self) -> dict[OsiSymbol, list[tuple[VirtualBook, Decimal]]]:
        interest: dict[OsiSymbol, list[tuple[VirtualBook, Decimal]]] = {}
        for book in self._hub.books:
            for position in book.positions:
                if position.quantity == _ZERO:
                    continue
                contract = _as_osi(position.symbol)
                if contract is None:
                    continue
                interest.setdefault(contract, []).append((book, position.quantity))
        return interest

    def _settle(
        self,
        book: VirtualBook,
        contract: OsiSymbol,
        quantity: Decimal,
        spot: Decimal,
        now: datetime,
    ) -> ContractSettlement:
        is_call = contract.right == "C"
        intrinsic = intrinsic_value(contract, spot)
        if quantity > _ZERO:
            exercised = quantity if intrinsic > _ZERO else _ZERO
            settlement_value = intrinsic * (quantity - exercised) * self._multiplier
            moved = exercised
            kind = (
                SettlementKind.EXERCISED
                if exercised > _ZERO
                else SettlementKind.WORTHLESS
            )
            side = Side.BUY if is_call else Side.SELL
        else:
            short_quantity = -quantity
            assigned = exact_decimal(
                self._assignment_policy.assigned_quantity(
                    contract, spot, short_quantity
                ),
                "assigned_quantity",
            )
            if assigned < _ZERO or assigned > short_quantity:
                raise InvalidAssignmentError(
                    f"assignment policy returned {assigned} contracts for "
                    f"{contract.value!r} short {short_quantity} contracts"
                )
            unassigned = short_quantity - assigned
            settlement_value = -(intrinsic * unassigned * self._multiplier)
            moved = assigned
            kind = (
                SettlementKind.ASSIGNED
                if assigned > _ZERO
                else SettlementKind.WORTHLESS
            )
            side = Side.SELL if is_call else Side.BUY

        fill: Fill | None = None
        if moved > _ZERO:
            fill = self._settlement_fill(book, contract, side, moved, now)
            self._hub.apply_fill(book.book_id, fill)
        position = book.retire_position(contract, settlement_value=settlement_value)
        return ContractSettlement(
            symbol=contract,
            book_id=book.book_id,
            kind=kind,
            contracts=moved,
            spot=spot,
            settlement_value=settlement_value,
            underlying_fill=fill,
            option_position=position,
        )

    def _settlement_fill(
        self,
        book: VirtualBook,
        contract: OsiSymbol,
        side: Side,
        contracts: Decimal,
        now: datetime,
    ) -> Fill:
        fill_id = f"settle:{contract.value}:{book.book_id}"
        fill = Fill(
            event_id=f"event-{fill_id}",
            timestamp=now,
            sequence=self._sequence,
            fill_id=fill_id,
            order_id=f"settle:{contract.value}",
            symbol=Symbol(contract.root),
            side=side,
            quantity=contracts * self._multiplier,
            price=contract.strike,
            fee=self._settlement_fee,
        )
        self._sequence += 1
        return fill

    @staticmethod
    def _require_osi(symbol: str | Symbol) -> OsiSymbol:
        canonical = canonical_symbol(symbol)
        if not isinstance(canonical, OsiSymbol):
            raise ValueError(f"not an OSI option symbol: {str(symbol)!r}")
        return canonical


__all__ = (
    "AssignmentPolicy",
    "ContractSettlement",
    "DaysToExpiry",
    "ExpiredContractError",
    "InvalidAssignmentError",
    "MissingUnderlyingPriceError",
    "OptionLifecycle",
    "OptionLifecycleError",
    "PessimisticAssignment",
    "SettlementKind",
    "intrinsic_value",
)
