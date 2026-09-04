"""Decimal-exact accounting for one strategy's virtual book."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.accounting import Fill
from harness.core.types.common import DecimalInput, exact_decimal, text
from harness.core.types.orders import Side

_ZERO = Decimal("0")


class PortfolioError(ValueError):
    """Base class for deterministic portfolio-accounting failures."""


class DuplicateFillError(PortfolioError):
    """Raised when a fill identifier would be accounted more than once."""


class UnknownPositionError(KeyError):
    """Raised when a virtual book has never held the requested symbol."""


@dataclass(frozen=True, slots=True)
class PositionMarker:
    """An explicit mark used to value a position without consulting a clock."""

    symbol: Symbol
    price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        price = exact_decimal(self.price, "price")
        if price <= _ZERO:
            raise ValueError("price must be positive")
        object.__setattr__(self, "price", price)


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Immutable signed position, cost basis, and attributed PnL."""

    symbol: Symbol
    quantity: Decimal
    cost_basis: Decimal
    cash_flow: Decimal
    realized_pnl: Decimal
    mark_price: Decimal | None

    @property
    def is_flat(self) -> bool:
        """Whether no position is currently open."""
        return self.quantity == _ZERO

    @property
    def average_cost(self) -> Decimal:
        """Return the average entry price, or zero for a flat position."""
        if self.is_flat:
            return _ZERO
        return self.cost_basis / self.quantity

    @property
    def market_value(self) -> Decimal | None:
        """Return signed marked value, requiring a mark only when open."""
        if self.is_flat:
            return _ZERO
        if self.mark_price is None:
            return None
        return self.quantity * self.mark_price

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Return marked PnL as the exact remainder after realized PnL."""
        total_pnl = self.total_pnl
        if total_pnl is None:
            return None
        return total_pnl - self.realized_pnl

    @property
    def total_pnl(self) -> Decimal | None:
        """Return the exact marked change from this position's cash flows."""
        market_value = self.market_value
        if market_value is None:
            return None
        return self.cash_flow + market_value


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Immutable valuation of one strategy virtual book."""

    book_id: str
    initial_cash: Decimal
    cash: Decimal
    positions: tuple[PositionSnapshot, ...]
    markers: tuple[PositionMarker, ...]
    fill_count: int

    @property
    def realized_pnl(self) -> Decimal:
        """Return realized PnL across all symbols, including all fill fees."""
        return sum(
            (position.realized_pnl for position in self.positions),
            start=_ZERO,
        )

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Return total unrealized PnL, or ``None`` if an open leg is unmarked."""
        total_pnl = self.total_pnl
        if total_pnl is None:
            return None
        return total_pnl - self.realized_pnl

    @property
    def market_value(self) -> Decimal | None:
        """Return total signed market value when every open leg is marked."""
        total = _ZERO
        for position in self.positions:
            market_value = position.market_value
            if market_value is None:
                return None
            total += market_value
        return total

    @property
    def equity(self) -> Decimal | None:
        """Return marked book equity, or ``None`` while a leg is unmarked."""
        market_value = self.market_value
        if market_value is None:
            return None
        return self.cash + market_value

    @property
    def total_pnl(self) -> Decimal | None:
        """Return the exact marked change in book equity."""
        equity = self.equity
        if equity is None:
            return None
        return equity - self.initial_cash

    def position(self, symbol: str | Symbol) -> PositionSnapshot:
        """Return one position from this immutable snapshot."""
        canonical = canonical_symbol(symbol)
        for position in self.positions:
            if position.symbol == canonical:
                return position
        raise UnknownPositionError(canonical)


@dataclass(slots=True)
class _PositionLedger:
    quantity: Decimal
    cost_basis: Decimal
    cash_flow: Decimal
    realized_pnl: Decimal


class VirtualBook:
    """Maintain cash and average-cost positions for one strategy.

    Fill prices determine average cost. Fees, including negative rebate fees,
    are recognized immediately in realized PnL. Marks are explicit and do not
    mutate cost or realized PnL.
    """

    __slots__ = (
        "_cash",
        "_fill_ids",
        "_initial_cash",
        "_markers",
        "_positions",
        "_seen_fill_ids",
        "book_id",
    )

    def __init__(self, book_id: str, initial_cash: Decimal | int | str) -> None:
        self.book_id = text(book_id, "book_id")
        self._initial_cash = exact_decimal(initial_cash, "initial_cash")
        self._cash = self._initial_cash
        self._positions: dict[Symbol, _PositionLedger] = {}
        self._markers: dict[Symbol, PositionMarker] = {}
        self._fill_ids: list[str] = []
        self._seen_fill_ids: set[str] = set()

    @property
    def initial_cash(self) -> Decimal:
        """Return the strategy's opening cash allocation."""
        return self._initial_cash

    @property
    def cash(self) -> Decimal:
        """Return current exact cash after notional and fees."""
        return self._cash

    @property
    def fill_ids(self) -> tuple[str, ...]:
        """Return accounted fill identifiers in application order."""
        return tuple(self._fill_ids)

    @property
    def fill_count(self) -> int:
        """Return the number of accounted fills."""
        return len(self._fill_ids)

    @property
    def positions(self) -> tuple[PositionSnapshot, ...]:
        """Return current position snapshots in canonical symbol order."""
        return self.snapshot.positions

    @property
    def snapshot(self) -> BookSnapshot:
        """Return the current immutable book valuation."""
        positions = tuple(
            self._position_snapshot(symbol, ledger)
            for symbol, ledger in sorted(
                self._positions.items(), key=lambda item: str(item[0])
            )
        )
        markers = tuple(
            marker
            for _, marker in sorted(
                self._markers.items(), key=lambda item: str(item[0])
            )
        )
        return BookSnapshot(
            book_id=self.book_id,
            initial_cash=self.initial_cash,
            cash=self.cash,
            positions=positions,
            markers=markers,
            fill_count=self.fill_count,
        )

    @property
    def realized_pnl(self) -> Decimal:
        """Return realized PnL across all positions."""
        return self.snapshot.realized_pnl

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Return unrealized PnL when all open positions are marked."""
        return self.snapshot.unrealized_pnl

    @property
    def equity(self) -> Decimal | None:
        """Return marked strategy equity."""
        return self.snapshot.equity

    @property
    def total_pnl(self) -> Decimal | None:
        """Return the marked change in strategy equity."""
        return self.snapshot.total_pnl

    def apply_fill(self, fill: Fill) -> PositionSnapshot:
        """Atomically account one immutable core fill."""
        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        if fill.fill_id in self._seen_fill_ids:
            raise DuplicateFillError(f"fill {fill.fill_id!r} was already applied")

        signed_quantity = fill.quantity if fill.side is Side.BUY else -fill.quantity
        current = self._positions.get(fill.symbol)
        quantity = _ZERO if current is None else current.quantity
        cost_basis = _ZERO if current is None else current.cost_basis
        cash_flow = _ZERO if current is None else current.cash_flow
        realized_pnl = _ZERO if current is None else current.realized_pnl
        next_quantity = quantity + signed_quantity

        if quantity == _ZERO or quantity * signed_quantity > _ZERO:
            next_cost_basis = cost_basis + signed_quantity * fill.price
        elif abs(signed_quantity) <= abs(quantity):
            released_basis = (
                cost_basis
                if abs(signed_quantity) == abs(quantity)
                else cost_basis * abs(signed_quantity) / abs(quantity)
            )
            realized_pnl += -signed_quantity * fill.price - released_basis
            next_cost_basis = cost_basis - released_basis
        else:
            closing_quantity = -quantity
            realized_pnl += -closing_quantity * fill.price - cost_basis
            next_cost_basis = next_quantity * fill.price

        realized_pnl -= fill.fee
        next_cash_flow = cash_flow - signed_quantity * fill.price - fill.fee
        if next_quantity == _ZERO:
            next_cost_basis = _ZERO
            realized_pnl = next_cash_flow
        next_ledger = _PositionLedger(
            quantity=next_quantity,
            cost_basis=next_cost_basis,
            cash_flow=next_cash_flow,
            realized_pnl=realized_pnl,
        )
        next_cash = self._cash - signed_quantity * fill.price - fill.fee

        self._positions[fill.symbol] = next_ledger
        self._cash = next_cash
        self._fill_ids.append(fill.fill_id)
        self._seen_fill_ids.add(fill.fill_id)
        return self._position_snapshot(fill.symbol, next_ledger)

    def retire_position(
        self, symbol: str | Symbol, *, settlement_value: DecimalInput = 0
    ) -> PositionSnapshot:
        """Retire one expired contract ledger at its final settlement value.

        Cash and cash flow move by ``settlement_value`` (negative when the
        book pays, for example unassigned intrinsic value), and the ledger is
        finalized exactly like a closing fill at that settlement value: flat
        quantity, zero cost basis, and realized PnL equal to total cash flow.
        Retiring an already flat ledger is refused so settlement effects stay
        unambiguous.
        """
        canonical = canonical_symbol(symbol)
        ledger = self._positions.get(canonical)
        if ledger is None:
            raise UnknownPositionError(canonical)
        if ledger.quantity == _ZERO:
            raise PortfolioError(f"position {canonical.value!r} is already flat")
        value = exact_decimal(settlement_value, "settlement_value")
        next_cash_flow = ledger.cash_flow + value
        next_ledger = _PositionLedger(
            quantity=_ZERO,
            cost_basis=_ZERO,
            cash_flow=next_cash_flow,
            realized_pnl=next_cash_flow,
        )
        self._positions[canonical] = next_ledger
        self._cash += value
        return self._position_snapshot(canonical, next_ledger)

    def apply_marker(self, marker: PositionMarker) -> PositionMarker:
        """Apply one validated explicit marker."""
        if not isinstance(marker, PositionMarker):
            raise TypeError("marker must be a PositionMarker")
        self._markers[marker.symbol] = marker
        return marker

    def mark(self, symbol: str | Symbol, price: DecimalInput) -> PositionMarker:
        """Create and apply an explicit price marker."""
        return self.apply_marker(
            PositionMarker(canonical_symbol(symbol), exact_decimal(price, "price"))
        )

    def marker(self, symbol: str | Symbol) -> PositionMarker | None:
        """Return the current explicit marker for a symbol, if any."""
        return self._markers.get(canonical_symbol(symbol))

    def position(self, symbol: str | Symbol) -> PositionSnapshot:
        """Return the current immutable state for one previously held symbol."""
        canonical = canonical_symbol(symbol)
        try:
            ledger = self._positions[canonical]
        except KeyError as error:
            raise UnknownPositionError(canonical) from error
        return self._position_snapshot(canonical, ledger)

    def _position_snapshot(
        self, symbol: Symbol, ledger: _PositionLedger
    ) -> PositionSnapshot:
        marker = self._markers.get(symbol)
        return PositionSnapshot(
            symbol=symbol,
            quantity=ledger.quantity,
            cost_basis=ledger.cost_basis,
            cash_flow=ledger.cash_flow,
            realized_pnl=ledger.realized_pnl,
            mark_price=None if marker is None else marker.price,
        )


__all__ = (
    "BookSnapshot",
    "DuplicateFillError",
    "PortfolioError",
    "PositionMarker",
    "PositionSnapshot",
    "UnknownPositionError",
    "VirtualBook",
)
