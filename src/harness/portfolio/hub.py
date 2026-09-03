"""Multiple virtual books and their shared-account aggregate."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.accounting import Fill
from harness.core.types.common import DecimalInput, exact_decimal, text
from harness.portfolio.book import (
    BookSnapshot,
    DuplicateFillError,
    PortfolioError,
    PositionMarker,
    PositionSnapshot,
    VirtualBook,
)

_ZERO = Decimal("0")


class DuplicateBookError(PortfolioError):
    """Raised when a virtual book identifier is already registered."""


class UnknownBookError(KeyError):
    """Raised when a virtual book is not registered with a portfolio hub."""


@dataclass(frozen=True, slots=True)
class AggregatePosition:
    """Shared-account totals retaining virtual-book PnL attribution."""

    symbol: Symbol
    quantity: Decimal
    long_quantity: Decimal
    short_quantity: Decimal
    cost_basis: Decimal
    cash_flow: Decimal
    average_cost: Decimal | None
    realized_pnl: Decimal
    mark_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None

    @property
    def gross_quantity(self) -> Decimal:
        """Return long quantity plus the absolute short quantity."""
        return self.long_quantity + self.short_quantity

    @property
    def total_pnl(self) -> Decimal | None:
        """Return the exact marked change from constituent cash flows."""
        if self.market_value is None:
            return None
        return self.cash_flow + self.market_value


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Immutable shared-account aggregate and constituent book snapshots."""

    account_id: str
    initial_cash: Decimal
    cash: Decimal
    positions: tuple[AggregatePosition, ...]
    books: tuple[BookSnapshot, ...]
    fill_count: int

    @property
    def realized_pnl(self) -> Decimal:
        """Return realized PnL attributed across all virtual books."""
        return sum(
            (position.realized_pnl for position in self.positions),
            start=_ZERO,
        )

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Return attributed unrealized PnL when every open leg is marked."""
        total_pnl = self.total_pnl
        if total_pnl is None:
            return None
        return total_pnl - self.realized_pnl

    @property
    def market_value(self) -> Decimal | None:
        """Return total signed market value when every open leg is marked."""
        total = _ZERO
        for position in self.positions:
            if position.market_value is None:
                return None
            total += position.market_value
        return total

    @property
    def equity(self) -> Decimal | None:
        """Return shared marked equity."""
        market_value = self.market_value
        if market_value is None:
            return None
        return self.cash + market_value

    @property
    def total_pnl(self) -> Decimal | None:
        """Return the exact change in aggregate equity."""
        equity = self.equity
        if equity is None:
            return None
        return equity - self.initial_cash

    def position(self, symbol: str | Symbol) -> AggregatePosition:
        """Return aggregate state for one symbol."""
        canonical = canonical_symbol(symbol)
        for position in self.positions:
            if position.symbol == canonical:
                return position
        raise KeyError(canonical)

    def book(self, book_id: str) -> BookSnapshot:
        """Return one constituent immutable book snapshot."""
        for book in self.books:
            if book.book_id == book_id:
                return book
        raise UnknownBookError(book_id)


class PortfolioHub:
    """Route fills to independent books and expose shared-account totals."""

    __slots__ = ("_books", "_fill_routes", "_markers", "account_id")

    def __init__(self, account_id: str, books: Iterable[VirtualBook] = ()) -> None:
        self.account_id = text(account_id, "account_id")
        self._books: dict[str, VirtualBook] = {}
        self._fill_routes: dict[str, str] = {}
        self._markers: dict[Symbol, PositionMarker] = {}
        for book in books:
            self.register_book(book)

    @property
    def books(self) -> tuple[VirtualBook, ...]:
        """Return registered books in registration order."""
        return tuple(self._books.values())

    @property
    def book_ids(self) -> tuple[str, ...]:
        """Return registered book identifiers in registration order."""
        return tuple(self._books)

    @property
    def snapshot(self) -> PortfolioSnapshot:
        """Return current constituent and shared-account valuations."""
        books = tuple(book.snapshot for book in self._books.values())
        positions_by_symbol: dict[Symbol, list[PositionSnapshot]] = {}
        for book in books:
            for position in book.positions:
                positions_by_symbol.setdefault(position.symbol, []).append(position)
        positions = tuple(
            self._aggregate_position(symbol, positions_by_symbol[symbol])
            for symbol in sorted(positions_by_symbol, key=str)
        )
        return PortfolioSnapshot(
            account_id=self.account_id,
            initial_cash=sum((book.initial_cash for book in books), start=_ZERO),
            cash=sum((book.cash for book in books), start=_ZERO),
            positions=positions,
            books=books,
            fill_count=sum((book.fill_count for book in books), start=0),
        )

    @property
    def cash(self) -> Decimal:
        """Return aggregate strategy cash."""
        return self.snapshot.cash

    @property
    def realized_pnl(self) -> Decimal:
        """Return aggregate attributed realized PnL."""
        return self.snapshot.realized_pnl

    @property
    def unrealized_pnl(self) -> Decimal | None:
        """Return aggregate attributed unrealized PnL when marked."""
        return self.snapshot.unrealized_pnl

    @property
    def equity(self) -> Decimal | None:
        """Return aggregate marked equity."""
        return self.snapshot.equity

    @property
    def total_pnl(self) -> Decimal | None:
        """Return aggregate marked change in equity."""
        return self.snapshot.total_pnl

    def register_book(self, book: VirtualBook) -> VirtualBook:
        """Register an existing virtual book after validating global fill IDs."""
        if not isinstance(book, VirtualBook):
            raise TypeError("book must be a VirtualBook")
        if book.book_id in self._books:
            raise DuplicateBookError(f"book {book.book_id!r} is already registered")
        for fill_id in book.fill_ids:
            if fill_id in self._fill_routes:
                routed_book = self._fill_routes[fill_id]
                raise DuplicateFillError(
                    f"fill {fill_id!r} is already routed to book {routed_book!r}"
                )

        self._books[book.book_id] = book
        for fill_id in book.fill_ids:
            self._fill_routes[fill_id] = book.book_id
        for marker in self._markers.values():
            book.apply_marker(marker)
        return book

    def create_book(self, book_id: str, initial_cash: DecimalInput) -> VirtualBook:
        """Create and register a strategy virtual book."""
        return self.register_book(VirtualBook(book_id, initial_cash))

    def book(self, book_id: str) -> VirtualBook:
        """Return a registered mutable virtual book."""
        try:
            return self._books[book_id]
        except KeyError as error:
            raise UnknownBookError(book_id) from error

    def apply_fill(self, book_id: str, fill: Fill) -> PositionSnapshot:
        """Route one globally unique fill to exactly one strategy book."""
        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        if fill.fill_id in self._fill_routes:
            routed_book = self._fill_routes[fill.fill_id]
            raise DuplicateFillError(
                f"fill {fill.fill_id!r} is already routed to book {routed_book!r}"
            )
        book = self.book(book_id)
        position = book.apply_fill(fill)
        self._fill_routes[fill.fill_id] = book_id
        return position

    def apply_marker(self, marker: PositionMarker) -> PositionMarker:
        """Apply one explicit market marker to every strategy book."""
        if not isinstance(marker, PositionMarker):
            raise TypeError("marker must be a PositionMarker")
        for book in self._books.values():
            book.apply_marker(marker)
        self._markers[marker.symbol] = marker
        return marker

    def mark(self, symbol: str | Symbol, price: DecimalInput) -> PositionMarker:
        """Create and broadcast an explicit price marker."""
        return self.apply_marker(
            PositionMarker(canonical_symbol(symbol), exact_decimal(price, "price"))
        )

    @staticmethod
    def _aggregate_position(
        symbol: Symbol, positions: list[PositionSnapshot]
    ) -> AggregatePosition:
        quantity = sum((position.quantity for position in positions), start=_ZERO)
        long_quantity = sum(
            (position.quantity for position in positions if position.quantity > _ZERO),
            start=_ZERO,
        )
        short_quantity = sum(
            (-position.quantity for position in positions if position.quantity < _ZERO),
            start=_ZERO,
        )
        cost_basis = sum((position.cost_basis for position in positions), start=_ZERO)
        cash_flow = sum((position.cash_flow for position in positions), start=_ZERO)
        average_cost = (
            cost_basis / quantity
            if quantity != _ZERO and (long_quantity == _ZERO or short_quantity == _ZERO)
            else None
        )
        realized_pnl = sum(
            (position.realized_pnl for position in positions), start=_ZERO
        )

        open_positions = [
            position for position in positions if position.quantity != _ZERO
        ]
        marks = {position.mark_price for position in open_positions}
        mark_price = marks.pop() if len(marks) == 1 and None not in marks else None

        market_value: Decimal | None = _ZERO
        for position in positions:
            position_market_value = position.market_value
            if position_market_value is None:
                market_value = None
                break
            market_value += position_market_value
        unrealized_pnl = (
            None if market_value is None else cash_flow + market_value - realized_pnl
        )

        return AggregatePosition(
            symbol=symbol,
            quantity=quantity,
            long_quantity=long_quantity,
            short_quantity=short_quantity,
            cost_basis=cost_basis,
            cash_flow=cash_flow,
            average_cost=average_cost,
            realized_pnl=realized_pnl,
            mark_price=mark_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
        )


__all__ = (
    "AggregatePosition",
    "DuplicateBookError",
    "PortfolioHub",
    "PortfolioSnapshot",
    "UnknownBookError",
)
