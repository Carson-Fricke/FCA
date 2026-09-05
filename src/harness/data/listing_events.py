"""Listing lifecycle events derived from the market-lake listings table.

The deriver turns :class:`~lake.catalog.models.ListingRecord` episodes into
the two corporate-action facts a strategy must react to: terminal delistings
(last session, delisting type, final price when known) and renames (symbol
transitions that preserve one entity identity).  Derivation is point in time
and mirrors the security master's no-look-ahead rule:

- a delisting surfaces only on the engine date equal to its ``delisted_date``
  -- until that date arrives the delisting is unknown and the listing stays
  active;
- a rename surfaces only on the successor episode's ``listed_date``, the
  first session under the new symbol.  The predecessor's rename delisting
  alone surfaces nothing, because the entity remains listed;
- each event is delivered at most once per deriver, in deterministic
  ``(known_date, entity_id, kind, symbol)`` order.

Like :mod:`harness.data.security_master`, no code path here reads wall time,
consumes randomness, touches the network, or performs I/O: the as-of instant
comes from the injected engine clock or an explicit argument, and engine
dates are the UTC calendar dates of those instants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from harness.core.clock import Clock
from harness.core.symbols import Symbol
from harness.core.types.common import exact_decimal
from harness.data.history import LookaheadError, Nanos, from_nanos, to_nanos
from harness.data.security_master import ListingsCatalogPort
from lake.catalog.models import DelistingType, ListingRecord, ListingType

_ZERO: Final = Decimal("0")


class CorporateActionKind(StrEnum):
    """The listing lifecycle fact carried by one corporate-action event."""

    DELISTING = "delisting"
    RENAME = "rename"


class UnknownDelistingPriceError(ValueError):
    """Raised when pessimistic settlement lacks a final price for a short."""


def _canonical_entity(entity_id: str) -> str:
    canonical = entity_id.strip().upper()
    if not canonical:
        raise ValueError("entity_id must not be empty")
    return canonical


def _canonical_symbol(symbol: str) -> str:
    return str(Symbol(symbol))


def _known_date(value: date) -> date:
    if isinstance(value, datetime):
        raise TypeError("known_date must be a date, not a datetime")
    if not isinstance(value, date):
        raise TypeError("known_date must be a date")
    return value


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    """Base record for one listing lifecycle fact on one entity.

    ``known_date`` is the engine date on which the fact became knowable: the
    delisting date for terminal delistings and the successor symbol's first
    session for renames.  Events are immutable, hashable, and ordered by
    :meth:`sort_key`, which is the deterministic delivery order the engine
    follows when calling the strategy corporate-action hook.
    """

    entity_id: str
    known_date: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", _canonical_entity(self.entity_id))
        object.__setattr__(self, "known_date", _known_date(self.known_date))

    @property
    def kind(self) -> CorporateActionKind:
        """Return the lifecycle fact this event carries."""
        raise NotImplementedError

    @property
    def symbol_key(self) -> str:
        """Return the symbol that orders this event within one entity."""
        raise NotImplementedError

    def sort_key(self) -> tuple[str, str, str, str]:
        """Return the deterministic delivery ordering key."""
        return (
            self.known_date.isoformat(),
            self.entity_id,
            self.kind.value,
            self.symbol_key,
        )


@dataclass(frozen=True, slots=True)
class DelistingEvent(CorporateActionEvent):
    """One terminal delisting: the last session of one symbol episode.

    ``known_date`` equals ``delisted_date`` -- the delisting is knowable on
    that date and not before.  ``final_price`` is ``None`` when the market
    never published a final print; settlement then follows
    :class:`PessimisticDelistingSettlement`.
    """

    symbol: str
    delisting_type: DelistingType
    final_price: Decimal | None = None

    def __post_init__(self) -> None:
        CorporateActionEvent.__post_init__(self)
        object.__setattr__(self, "symbol", _canonical_symbol(self.symbol))
        object.__setattr__(self, "delisting_type", DelistingType(self.delisting_type))
        if self.final_price is not None:
            object.__setattr__(
                self, "final_price", exact_decimal(self.final_price, "final_price")
            )

    @property
    def kind(self) -> CorporateActionKind:
        """Return the lifecycle fact this event carries."""
        return CorporateActionKind.DELISTING

    @property
    def symbol_key(self) -> str:
        """Return the symbol that orders this event within one entity."""
        return self.symbol


@dataclass(frozen=True, slots=True)
class RenameEvent(CorporateActionEvent):
    """One symbol transition that preserves the entity identity.

    ``known_date`` equals the successor's ``listed_date``, the first session
    under ``new_symbol``.  ``entity_id`` is shared with the predecessor
    episode, so positions, trackers, subscriptions, and books migrate under
    one continuous identity.
    """

    old_symbol: str
    new_symbol: str

    def __post_init__(self) -> None:
        CorporateActionEvent.__post_init__(self)
        object.__setattr__(self, "old_symbol", _canonical_symbol(self.old_symbol))
        object.__setattr__(self, "new_symbol", _canonical_symbol(self.new_symbol))
        if self.old_symbol == self.new_symbol:
            raise ValueError("a rename must change the symbol")

    @property
    def kind(self) -> CorporateActionKind:
        """Return the lifecycle fact this event carries."""
        return CorporateActionKind.RENAME

    @property
    def symbol_key(self) -> str:
        """Return the symbol that orders this event within one entity."""
        return self.old_symbol


class PessimisticDelistingSettlement:
    """Exact delisting settlement at the final price, pessimistic otherwise.

    A known ``final_price`` closes every position exactly at that price:
    longs credit ``quantity * final_price`` cash and shorts debit it.  An
    unknown final price is pessimistic for longs -- the position settles
    worthless with zero cash effect -- and unpriceable for shorts, which
    raise :class:`UnknownDelistingPriceError` because no finite pessimistic
    price exists.
    """

    __slots__ = ()

    def settlement_value(
        self, quantity: Decimal, final_price: Decimal | None
    ) -> Decimal:
        """Return the exact cash settled by closing ``quantity`` shares."""
        quantity = exact_decimal(quantity, "quantity")
        if final_price is not None:
            price = exact_decimal(final_price, "final_price")
            if price <= _ZERO:
                raise ValueError("final_price must be positive")
            return quantity * price
        if quantity > _ZERO:
            return _ZERO
        if quantity < _ZERO:
            raise UnknownDelistingPriceError(
                "pessimistic settlement of a short delisted position requires "
                "a known final_price"
            )
        return _ZERO


class ListingEventDeriver:
    """Derive due corporate-action events from the listings catalog.

    The deriver reads only the listings table injected at construction and
    never reads beyond the clock head: polling at an ``as_of`` newer than the
    head raises :class:`LookaheadError` before any listing data is touched.
    Polling is idempotent -- each event is returned exactly once, the first
    time the engine date reaches its knowledge date -- and every batch is
    ordered by :meth:`CorporateActionEvent.sort_key`.
    """

    __slots__ = ("_clock", "_delivered", "_emitted", "_listings")

    def __init__(self, clock: Clock, listings: ListingsCatalogPort) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not isinstance(listings, ListingsCatalogPort):
            raise TypeError("listings must implement ListingsCatalogPort")
        self._clock = clock
        self._listings = listings
        self._delivered: list[CorporateActionEvent] = []
        self._emitted: set[CorporateActionEvent] = set()

    @property
    def delivered(self) -> tuple[CorporateActionEvent, ...]:
        """Return every event returned by :meth:`poll`, in delivery order."""
        return tuple(self._delivered)

    def pending(
        self, as_of: datetime | Nanos | None = None
    ) -> tuple[CorporateActionEvent, ...]:
        """Return due events that have not been polled, without consuming."""
        engine_date = self._engine_date(as_of)
        events = [
            event for event in self._derive(engine_date) if event not in self._emitted
        ]
        events.sort(key=CorporateActionEvent.sort_key)
        return tuple(events)

    def poll(
        self, as_of: datetime | Nanos | None = None
    ) -> tuple[CorporateActionEvent, ...]:
        """Return due events in delivery order and record them as delivered.

        An event becomes due on the engine date that reaches its knowledge
        date and is never returned again afterwards.
        """
        events = self.pending(as_of)
        self._delivered.extend(events)
        self._emitted.update(events)
        return events

    def _engine_date(self, as_of: datetime | Nanos | None) -> date:
        head = to_nanos(self._clock.now(), "clock")
        requested = head if as_of is None else to_nanos(as_of, "as_of")
        if requested > head:
            raise LookaheadError(
                f"as_of {requested} is newer than the engine clock head {head}"
            )
        return from_nanos(requested).date()

    def _derive(self, engine_date: date) -> list[CorporateActionEvent]:
        events: list[CorporateActionEvent] = []
        for chain in self._chains(engine_date):
            for index, episode in enumerate(chain):
                predecessor = chain[index - 1] if index > 0 else None
                events.extend(
                    self._episode_events(
                        entity_id=episode.entity_id,
                        episode=episode,
                        predecessor=predecessor,
                        engine_date=engine_date,
                    )
                )
        return events

    def _episode_events(
        self,
        *,
        entity_id: str,
        episode: ListingRecord,
        predecessor: ListingRecord | None,
        engine_date: date,
    ) -> tuple[CorporateActionEvent, ...]:
        rename: CorporateActionEvent | None = None
        if episode.listing_type is ListingType.RENAME and predecessor is not None:
            rename = RenameEvent(
                entity_id=entity_id,
                known_date=episode.listed_date,
                old_symbol=predecessor.symbol,
                new_symbol=episode.symbol,
            )
        delisting: CorporateActionEvent | None = None
        if (
            episode.delisted_date is not None
            and episode.delisted_date <= engine_date
            and episode.delisting_type is not DelistingType.RENAME
        ):
            delisting = DelistingEvent(
                entity_id=entity_id,
                known_date=episode.delisted_date,
                symbol=episode.symbol,
                delisting_type=episode.delisting_type
                if episode.delisting_type is not None
                else DelistingType.UNKNOWN,
                final_price=episode.final_price,
            )
        return tuple(event for event in (rename, delisting) if event is not None)

    def _chains(self, engine_date: date) -> list[list[ListingRecord]]:
        chains: dict[str, list[ListingRecord]] = {}
        for episode in self._listings.list():
            if episode.listed_date > engine_date:
                continue
            chains.setdefault(episode.entity_id, []).append(episode)
        return [
            sorted(chain, key=lambda item: (item.listed_date, item.symbol))
            for chain in chains.values()
        ]


__all__ = [
    "CorporateActionEvent",
    "CorporateActionKind",
    "DelistingEvent",
    "ListingEventDeriver",
    "PessimisticDelistingSettlement",
    "RenameEvent",
    "UnknownDelistingPriceError",
]
