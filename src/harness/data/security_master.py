"""Point-in-time security master over the market-lake listings table.

The master answers one question per engine date: what is known about a
security as of that date.  Membership is loaded from the market-lake listings
table -- one :class:`~lake.catalog.models.ListingRecord` per entity listing
episode, carrying entity identity, listing and delisting dates and types,
final prices, and the rename chain that links historical tickers to one
entity -- without ever consulting rows dated after the engine date:

- a listing episode whose ``listed_date`` has not arrived is invisible, so a
  rename cannot surface under its new symbol before the rename takes effect;
- a delisting dated after the as-of instant is treated as unknown, so the
  listing keeps reporting as active until the delisting date actually
  arrives;
- an entity's rename chain is resolved through its shared entity identity,
  so historical symbols resolve to one entity with no duplicate coverage.

Every resolved snapshot is then clipped to the engine date at construction,
which is the structural no-look-ahead chokepoint: episodes listed after
``as_of`` are dropped and future delistings are hidden even if a caller
passes listing rows directly.

Like :mod:`harness.data.history`, no code path here reads wall time,
consumes randomness, touches the network, or performs I/O: the as-of instant
comes from the injected engine clock or an explicit argument, and engine
dates are the UTC calendar dates of those instants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from harness.core.clock import Clock
from harness.core.symbols import Symbol
from harness.data.history import LookaheadError, Nanos, from_nanos, to_nanos
from lake.catalog.models import DelistingType, ListingRecord


@runtime_checkable
class ListingsCatalogPort(Protocol):
    """Read boundary over the market-lake listings table."""

    def list(
        self,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ListingRecord, ...]:
        """Return listing episodes filtered by entity, symbol, and knowledge."""


def _canonical_entity(entity_id: str) -> str:
    canonical = entity_id.strip().upper()
    if not canonical:
        raise ValueError("entity_id must not be empty")
    return canonical


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    """The security master facts proven for one entity on one engine date.

    Construction is the structural no-look-ahead chokepoint: episodes listed
    after ``as_of`` are clipped here even if a caller passes them directly,
    so the snapshot can only describe what was knowable on ``as_of``.
    """

    entity_id: str
    as_of: date
    queried_symbol: str
    episodes: tuple[ListingRecord, ...]

    def __post_init__(self) -> None:
        if isinstance(self.as_of, datetime):
            raise TypeError("as_of must be a date, not a datetime")
        if not isinstance(self.as_of, date):
            raise TypeError("as_of must be a date")
        object.__setattr__(self, "entity_id", _canonical_entity(self.entity_id))
        object.__setattr__(self, "queried_symbol", str(Symbol(self.queried_symbol)))
        episodes = tuple(
            sorted(
                (
                    episode
                    for episode in self.episodes
                    if episode.listed_date <= self.as_of
                ),
                key=lambda episode: (episode.listed_date, episode.symbol),
            )
        )
        if not episodes:
            raise ValueError("a security snapshot requires at least one known episode")
        object.__setattr__(self, "episodes", episodes)

    @property
    def symbol(self) -> str:
        """Return the symbol under which the entity trades on ``as_of``."""
        active = self._active
        return self.episodes[-1].symbol if active is None else active.symbol

    @property
    def is_listed(self) -> bool:
        """Whether the entity had an active listing on ``as_of``."""
        return self._active is not None

    @property
    def listed_date(self) -> date:
        """Return the earliest listing date known on ``as_of``."""
        return self.episodes[0].listed_date

    @property
    def delisted_date(self) -> date | None:
        """Return the delisting date proven on ``as_of``, if any.

        A delisting dated after ``as_of`` is unknown, and a rename successor
        keeps the entity listed, so this returns a date only when the latest
        known episode has provably ended.
        """
        if self.is_listed:
            return None
        latest = self.episodes[-1]
        if latest.delisted_date is not None and latest.delisted_date <= self.as_of:
            return latest.delisted_date
        return None

    @property
    def delisting_type(self) -> DelistingType | None:
        """Return the delisting type once the delisting is proven."""
        latest = self.episodes[-1]
        if self.delisted_date is None:
            return None
        return latest.delisting_type

    @property
    def final_price(self) -> Decimal | None:
        """Return the final price once the delisting is proven."""
        latest = self.episodes[-1]
        if self.delisted_date is None:
            return None
        return latest.final_price

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the rename chain known on ``as_of``, oldest first."""
        return tuple(episode.symbol for episode in self.episodes)

    def episode_on(self, day: date) -> ListingRecord | None:
        """Return the episode covering ``day``, which must not exceed ``as_of``."""
        if isinstance(day, datetime):
            raise TypeError("day must be a date, not a datetime")
        if day > self.as_of:
            raise ValueError("day must be on or before the snapshot as_of date")
        for episode in reversed(self.episodes):
            if episode.active_on(day, as_of=self.as_of):
                return episode
        return None

    @property
    def _active(self) -> ListingRecord | None:
        for episode in reversed(self.episodes):
            if episode.active_on(self.as_of, as_of=self.as_of):
                return episode
        return None


class SecurityMasterView:
    """Point-in-time security master clipped to the engine clock.

    The view reads only the market-lake listings table injected at
    construction and never reads beyond the clock head.  Resolving at an
    ``as_of`` newer than the head raises :class:`LookaheadError` before any
    listing data is touched, mirroring the historical data boundary.  As-of
    queries never expose listings, delistings, or renames dated after the
    as-of instant: future episodes stay invisible and future delistings keep
    the listing active.
    """

    __slots__ = ("_clock", "_listings")

    def __init__(self, clock: Clock, listings: ListingsCatalogPort) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        if not isinstance(listings, ListingsCatalogPort):
            raise TypeError("listings must implement ListingsCatalogPort")
        self._clock = clock
        self._listings = listings

    @property
    def clock(self) -> Clock:
        """Return the injected engine clock."""
        return self._clock

    @property
    def head(self) -> datetime:
        """Return the current engine clock head, the hard read boundary."""
        return self._clock.now()

    def resolve(
        self, symbol: str, as_of: datetime | Nanos | None = None
    ) -> SecuritySnapshot | None:
        """Return the security known under ``symbol`` as of ``as_of``.

        ``None`` means no listing episode for the symbol was known by the
        as-of date, including symbols that only list later.
        """
        engine_date = self._engine_date(as_of)
        episodes = self._visible(symbol=str(Symbol(symbol)), engine_date=engine_date)
        if not episodes:
            return None
        chosen = max(
            episodes, key=lambda episode: (episode.listed_date, episode.entity_id)
        )
        return SecuritySnapshot(
            entity_id=chosen.entity_id,
            as_of=engine_date,
            queried_symbol=str(Symbol(symbol)),
            episodes=self._visible(entity_id=chosen.entity_id, engine_date=engine_date),
        )

    def entity_id(
        self, symbol: str, as_of: datetime | Nanos | None = None
    ) -> str | None:
        """Return the entity identity behind ``symbol`` as of ``as_of``."""
        snapshot = self.resolve(symbol, as_of)
        return None if snapshot is None else snapshot.entity_id

    def is_listed(self, symbol: str, as_of: datetime | Nanos | None = None) -> bool:
        """Return whether ``symbol`` proved listed on ``as_of``."""
        snapshot = self.resolve(symbol, as_of)
        return snapshot is not None and snapshot.is_listed

    def active_symbol(
        self, entity_id: str, as_of: datetime | Nanos | None = None
    ) -> str | None:
        """Return the symbol the entity traded under on ``as_of``, if any."""
        engine_date = self._engine_date(as_of)
        episodes = self._visible(
            entity_id=_canonical_entity(entity_id), engine_date=engine_date
        )
        for episode in reversed(episodes):
            if episode.active_on(engine_date, as_of=engine_date):
                return episode.symbol
        return None

    def listed_symbols(self, as_of: datetime | Nanos | None = None) -> tuple[str, ...]:
        """Return the tradable symbols on ``as_of``, one per listed entity."""
        engine_date = self._engine_date(as_of)
        active_by_entity: dict[str, ListingRecord] = {}
        for episode in self._visible(engine_date=engine_date):
            if episode.active_on(engine_date, as_of=engine_date):
                active_by_entity[episode.entity_id] = episode
        return tuple(sorted({episode.symbol for episode in active_by_entity.values()}))

    def history(
        self, entity_id: str, as_of: datetime | Nanos | None = None
    ) -> tuple[ListingRecord, ...]:
        """Return the entity's rename chain known on ``as_of``."""
        engine_date = self._engine_date(as_of)
        return self._visible(
            entity_id=_canonical_entity(entity_id), engine_date=engine_date
        )

    def _engine_date(self, as_of: datetime | Nanos | None) -> date:
        head = to_nanos(self._clock.now(), "clock")
        requested = head if as_of is None else to_nanos(as_of, "as_of")
        if requested > head:
            raise LookaheadError(
                f"as_of {requested} is newer than the engine clock head {head}"
            )
        return from_nanos(requested).date()

    def _visible(
        self,
        engine_date: date,
        *,
        entity_id: str | None = None,
        symbol: str | None = None,
    ) -> tuple[ListingRecord, ...]:
        """Return episodes known on the engine date, in chain order."""
        episodes = self._listings.list(entity_id=entity_id, symbol=symbol)
        return tuple(
            sorted(
                (episode for episode in episodes if episode.listed_date <= engine_date),
                key=lambda episode: (episode.listed_date, episode.symbol),
            )
        )


__all__ = [
    "ListingsCatalogPort",
    "LookaheadError",
    "Nanos",
    "SecurityMasterView",
    "SecuritySnapshot",
]
