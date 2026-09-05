"""Point-in-time strategy universe with hard look-ahead clipping.

The view answers one question per engine date: which symbols are selectable
as of that date, and what is known about them.  Membership is composed from
point-in-time member sources -- equity listings read from the market-lake
listings table and option contracts read from the market-lake contracts
catalog -- under a union or intersection rule, without ever consulting rows
dated after the engine date:

- an equity listing episode whose ``listed_date`` has not arrived is
  invisible, so a symbol cannot enter the universe before it lists and a
  rename surfaces under exactly one symbol per entity;
- a delisting dated after the as-of instant is treated as unknown, so the
  symbol stays selectable until the delisting date actually arrives;
- an option contract is selectable from its listing date through its
  expiration date and is retired from the following day, exactly like the
  option universe in :mod:`harness.data.options_universe`.

Because membership derives solely from listing episodes and contract rows
dated on or before the as-of instant -- never from the set of symbols that
happen to trade today -- a universe composed from these sources is
survivorship-clean by construction, and :attr:`UniverseSnapshot.provenance`
records the member counts a run manifest can declare.

Every resolved snapshot is then clipped to the engine date at construction,
which is the structural no-look-ahead chokepoint: members that are not yet
listed, already retired, or otherwise invalid on ``as_of`` are dropped even
if a caller passes them directly.

Dynamic subscriptions -- deterministic boundary contract
--------------------------------------------------------

Runtime ``subscribe`` and ``unsubscribe`` operations
(:meth:`harness.strategy.context.StrategyContext.subscribe` and
``unsubscribe`` over :class:`harness.strategy.context.SubscriptionSet`) are
honored at the per-event delivery boundary.  The event-dispatch path consults
the live subscription set immediately before each strategy callback
(``StrategyBinding._receive`` calls ``StrategyContext._accepts``, which reads
``SubscriptionSet.is_subscribed``, for every single dispatched event), and
the engine delivers events one at a time in the deterministic bus order
(timestamp, sequence, event-class priority).  A subscription change made
inside a callback therefore:

- never rewrites delivery of any event already delivered, including the
  callback's own triggering event, which was gated before the callback ran;
- is in force for every event delivered after the callback returns, starting
  with the very next dispatched event;
- is equivalent in replay and mock-live shells, which share the same
  bus-to-binding delivery path and consult the set at the same point.

Subscriptions taken in ``on_start`` are in force before the first market
event is delivered, because start callbacks run before event dispatch begins.
There is no deferral queue, no snapshotting of subscriptions at binding
attach or at session boundaries, and no retroactive replay of suppressed
events.  A subscribe naming symbols outside the strategy universe raises
``ValueError`` immediately, duplicate subscribes are idempotent, and
unsubscribing a missing entry is a harmless no-op -- all deterministic.

Like :mod:`harness.data.history`, no code path here reads wall time,
consumes randomness, touches the network, or performs I/O: the as-of instant
comes from the injected engine clock or an explicit argument, and engine
dates are the UTC calendar dates of those instants.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from harness.core.clock import Clock
from harness.core.symbols import Symbol, canonical_osi_symbol, canonical_symbol
from harness.data.history import LookaheadError, Nanos, from_nanos, to_nanos
from harness.data.options_universe import ContractsCatalogPort
from harness.data.security_master import ListingsCatalogPort
from lake.catalog.models import ContractRecord, ListingRecord


class MemberKind(StrEnum):
    """The asset kind a universe member's record describes."""

    EQUITY = "equity"
    OPTION = "option"


class UniverseComposition(StrEnum):
    """How membership is composed across the view's member sources."""

    UNION = "union"
    INTERSECTION = "intersection"


_KIND_ORDER: Final[Mapping[MemberKind, int]] = MappingProxyType(
    {MemberKind.EQUITY: 0, MemberKind.OPTION: 1}
)


@runtime_checkable
class UniverseSource(Protocol):
    """Point-in-time member source read on explicit engine dates."""

    def members(self, as_of: date) -> tuple[UniverseMember, ...]:
        """Return the source's members valid on ``as_of``, in sorted order."""
        ...


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One selectable universe symbol with its point-in-time record.

    Exactly one of ``listing`` (for equity members) or ``contract`` (for
    option members) is present, and the record is the one proven on the
    snapshot date -- not the latest row the catalog happens to hold.
    """

    symbol: str
    kind: MemberKind
    listing: ListingRecord | None = None
    contract: ContractRecord | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(canonical_symbol(self.symbol)))
        object.__setattr__(self, "kind", MemberKind(self.kind))
        if (self.listing is None) == (self.contract is None):
            raise ValueError("a universe member requires exactly one record")
        if self.kind is MemberKind.EQUITY:
            if not isinstance(self.listing, ListingRecord):
                raise TypeError("equity members require a ListingRecord")
        elif not isinstance(self.contract, ContractRecord):
            raise TypeError("option members require a ContractRecord")

    @property
    def entity_id(self) -> str:
        """Return the owning entity for equities, or the underlying for options."""
        if self.listing is not None:
            return self.listing.entity_id
        assert self.contract is not None
        return str(self.contract.underlying)

    @property
    def metadata(self) -> ListingRecord | ContractRecord:
        """Return the point-in-time listing or contract record."""
        if self.listing is not None:
            return self.listing
        assert self.contract is not None
        return self.contract


def _contract_visible(record: ContractRecord, underlying: str, as_of: date) -> bool:
    """Return whether the contract is selectable on the engine date."""
    return (
        str(record.underlying) == underlying
        and as_of <= record.expiration_date
        and (record.listed_date is None or as_of >= record.listed_date)
        and (record.delisted_date is None or as_of <= record.delisted_date)
    )


def _member_covers(member: UniverseMember, as_of: date) -> bool:
    """Return whether the member's record proves membership on ``as_of``."""
    if member.kind is MemberKind.EQUITY:
        assert member.listing is not None
        return member.listing.active_on(as_of, as_of=as_of)
    assert member.contract is not None
    return _contract_visible(member.contract, str(member.contract.underlying), as_of)


class EquityListingUniverse:
    """Point-in-time equity membership from the market-lake listings table.

    Membership is one tradable symbol per entity with an active listing
    episode on the engine date: episodes listed after the date are invisible,
    delistings dated after the date are unknown (so the symbol stays
    selectable), and rename chains collapse to the single symbol active on
    the date.  This mirrors the security master's point-in-time rules and
    makes the universe survivorship-clean by construction.
    """

    __slots__ = ("_listings",)

    def __init__(self, listings: ListingsCatalogPort) -> None:
        if not isinstance(listings, ListingsCatalogPort):
            raise TypeError("listings must implement ListingsCatalogPort")
        self._listings = listings

    def members(self, as_of: date) -> tuple[UniverseMember, ...]:
        """Return the listed entities selectable on ``as_of``."""
        if isinstance(as_of, datetime):
            raise TypeError("as_of must be a date, not a datetime")
        active: dict[str, ListingRecord] = {}
        for episode in sorted(
            self._listings.list(),
            key=lambda episode: (episode.listed_date, episode.symbol),
        ):
            if episode.active_on(as_of, as_of=as_of):
                active[episode.entity_id] = episode
        return tuple(
            sorted(
                (
                    UniverseMember(episode.symbol, MemberKind.EQUITY, listing=episode)
                    for episode in active.values()
                ),
                key=lambda member: member.symbol,
            )
        )


class OptionContractUniverse:
    """Point-in-time option membership from the market-lake contracts catalog.

    Membership for one underlying is sourced like the option universe in
    :mod:`harness.data.options_universe`: by default the daily front-set
    record keyed by the engine date, so membership rolls only when the
    catalog itself rolled, or a fixed set of OSI symbols read from contract
    metadata.  Every resolved contract is clipped to the engine date: not yet
    listed, already delisted, or expired contracts are invisible.
    """

    __slots__ = ("_catalog", "_static_symbols", "_underlying")

    def __init__(
        self,
        catalog: ContractsCatalogPort,
        underlying: str | Symbol,
        *,
        static_symbols: Sequence[str] | None = None,
    ) -> None:
        if not isinstance(catalog, ContractsCatalogPort):
            raise TypeError("catalog must implement ContractsCatalogPort")
        self._catalog = catalog
        self._underlying = str(Symbol(underlying))
        if static_symbols is None:
            self._static_symbols = None
        else:
            symbols = tuple(canonical_osi_symbol(symbol) for symbol in static_symbols)
            if not symbols:
                raise ValueError(
                    "static_symbols must name at least one OSI symbol when provided"
                )
            if len(set(symbols)) != len(symbols):
                raise ValueError(
                    "static_symbols must not contain duplicate OSI symbols"
                )
            self._static_symbols = symbols

    def members(self, as_of: date) -> tuple[UniverseMember, ...]:
        """Return the contracts on the underlying selectable on ``as_of``."""
        if isinstance(as_of, datetime):
            raise TypeError("as_of must be a date, not a datetime")
        if self._static_symbols is None:
            front_set = self._catalog.get_front_set(self._underlying, as_of)
            records = () if front_set is None else front_set.contracts
        else:
            records = tuple(
                record
                for symbol in self._static_symbols
                if (record := self._catalog.get(str(symbol))) is not None
            )
        return tuple(
            sorted(
                (
                    UniverseMember(record.symbol, MemberKind.OPTION, contract=record)
                    for record in records
                    if _contract_visible(record, self._underlying, as_of)
                ),
                key=lambda member: member.symbol,
            )
        )


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    """The universe members proven for one engine date.

    Construction is the structural no-look-ahead chokepoint: members that are
    not yet listed, already retired, or otherwise invalid on ``as_of`` are
    clipped here even if a caller passes them directly, so the snapshot can
    only describe what was selectable on ``as_of``.
    """

    as_of: date
    composition: UniverseComposition
    members: tuple[UniverseMember, ...]

    def __post_init__(self) -> None:
        if isinstance(self.as_of, datetime) or not isinstance(self.as_of, date):
            raise TypeError("as_of must be a date, not a datetime")
        object.__setattr__(self, "composition", UniverseComposition(self.composition))
        selected: list[UniverseMember] = []
        seen: set[tuple[MemberKind, str]] = set()
        for member in self.members:
            if not isinstance(member, UniverseMember):
                raise TypeError("members must be UniverseMember instances")
            if not _member_covers(member, self.as_of):
                continue
            key = (member.kind, member.symbol)
            if key not in seen:
                seen.add(key)
                selected.append(member)
        selected.sort(key=lambda member: (_KIND_ORDER[member.kind], member.symbol))
        object.__setattr__(self, "members", tuple(selected))

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return member symbols in deterministic (kind, symbol) order."""
        return tuple(member.symbol for member in self.members)

    @property
    def equities(self) -> tuple[str, ...]:
        """Return equity member symbols in deterministic order."""
        return tuple(
            member.symbol for member in self.members if member.kind is MemberKind.EQUITY
        )

    @property
    def options(self) -> tuple[str, ...]:
        """Return option member symbols in deterministic order."""
        return tuple(
            member.symbol for member in self.members if member.kind is MemberKind.OPTION
        )

    @property
    def provenance(self) -> Mapping[str, int]:
        """Return member counts by kind for run-manifest cleanliness records.

        A snapshot resolved from the point-in-time sources in this module is
        survivorship-clean by construction, because membership derives solely
        from listing episodes and contract rows dated on or before ``as_of``.
        """
        counts: dict[str, int] = {"total": len(self.members)}
        for member in self.members:
            counts[member.kind.value] = counts.get(member.kind.value, 0) + 1
        return MappingProxyType(counts)

    def membership(self, symbol: str | Symbol) -> bool:
        """Return whether ``symbol`` belongs to this snapshot's universe."""
        return self.member(symbol) is not None

    def member(self, symbol: str | Symbol) -> UniverseMember | None:
        """Return one member with its point-in-time record, if selected."""
        target = str(canonical_symbol(symbol))
        return next(
            (member for member in self.members if member.symbol == target), None
        )

    def __contains__(self, symbol: object) -> bool:
        if not isinstance(symbol, str):
            return False
        try:
            target = str(canonical_symbol(symbol))
        except (TypeError, ValueError):
            return False
        return any(member.symbol == target for member in self.members)

    def __iter__(self) -> Iterator[UniverseMember]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)


class UniverseView:
    """Point-in-time strategy universe clipped to the engine clock.

    The view composes one or more point-in-time member sources -- equity
    listings and option contract universes -- under a union or intersection
    rule and never reads beyond the clock head.  Resolving at an ``as_of``
    newer than the head raises :class:`LookaheadError` before any source data
    is touched, mirroring the historical data boundary.  This is the
    engine-facing point-in-time counterpart of the static
    :class:`harness.strategy.context.UniverseView`: that class owns one
    strategy's fixed canonical symbol set, while this view re-derives the
    selectable membership per engine date from the security master and the
    contracts catalog.
    """

    __slots__ = ("_clock", "_composition", "_sources")

    def __init__(
        self,
        clock: Clock,
        sources: UniverseSource | Sequence[UniverseSource],
        *,
        composition: UniverseComposition = UniverseComposition.UNION,
    ) -> None:
        if not isinstance(clock, Clock):
            raise TypeError("clock must implement Clock")
        resolved = (sources,) if isinstance(sources, UniverseSource) else tuple(sources)
        if not resolved:
            raise ValueError("a universe view requires at least one source")
        for source in resolved:
            if not isinstance(source, UniverseSource):
                raise TypeError("sources must implement UniverseSource")
        self._clock = clock
        self._sources = resolved
        self._composition = UniverseComposition(composition)

    @property
    def clock(self) -> Clock:
        """Return the injected engine clock."""
        return self._clock

    @property
    def head(self) -> datetime:
        """Return the current engine clock head, the hard read boundary."""
        return self._clock.now()

    @property
    def composition(self) -> UniverseComposition:
        """Return how membership is composed across the sources."""
        return self._composition

    @property
    def sources(self) -> tuple[UniverseSource, ...]:
        """Return the composed member sources in composition order."""
        return self._sources

    def resolve(self, as_of: datetime | Nanos | None = None) -> UniverseSnapshot:
        """Return the selectable members as of ``as_of``, clipped to the head."""
        engine_date = self._engine_date(as_of)
        per_source = tuple(source.members(engine_date) for source in self._sources)
        return UniverseSnapshot(
            as_of=engine_date,
            composition=self._composition,
            members=self._compose(per_source),
        )

    def membership(
        self, symbol: str | Symbol, as_of: datetime | Nanos | None = None
    ) -> bool:
        """Return whether ``symbol`` is selectable on ``as_of``."""
        return self.resolve(as_of).membership(symbol)

    def metadata(
        self, as_of: datetime | Nanos | None = None
    ) -> tuple[UniverseMember, ...]:
        """Return point-in-time records for every member on ``as_of``."""
        return self.resolve(as_of).members

    def member_metadata(
        self, symbol: str | Symbol, as_of: datetime | Nanos | None = None
    ) -> UniverseMember | None:
        """Return the point-in-time record for ``symbol`` on ``as_of``.

        ``None`` means the symbol was not selectable on the as-of date,
        including symbols that only list later.
        """
        return self.resolve(as_of).member(symbol)

    def symbols(self, as_of: datetime | Nanos | None = None) -> tuple[str, ...]:
        """Return the member symbols on ``as_of`` in deterministic order."""
        return self.resolve(as_of).symbols

    def _engine_date(self, as_of: datetime | Nanos | None) -> date:
        head = to_nanos(self._clock.now(), "clock")
        requested = head if as_of is None else to_nanos(as_of, "as_of")
        if requested > head:
            raise LookaheadError(
                f"as_of {requested} is newer than the engine clock head {head}"
            )
        return from_nanos(requested).date()

    def _compose(
        self, per_source: Sequence[tuple[UniverseMember, ...]]
    ) -> tuple[UniverseMember, ...]:
        """Apply the composition rule across per-source membership."""
        if self._composition is UniverseComposition.UNION:
            return tuple(member for members in per_source for member in members)
        shared = {member.symbol for member in per_source[0]}
        for members in per_source[1:]:
            shared &= {member.symbol for member in members}
        if not shared:
            return ()
        chosen: dict[str, UniverseMember] = {}
        for members in per_source:
            for member in members:
                if member.symbol in shared:
                    chosen.setdefault(member.symbol, member)
        return tuple(chosen.values())


__all__ = [
    "EquityListingUniverse",
    "MemberKind",
    "OptionContractUniverse",
    "UniverseComposition",
    "UniverseMember",
    "UniverseSnapshot",
    "UniverseSource",
    "UniverseView",
]
