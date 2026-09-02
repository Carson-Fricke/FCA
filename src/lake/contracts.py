"""Daily option front-set records and deterministic in-memory storage.

Contract metadata remains owned by :mod:`lake.catalog`.  This module stores
the date-specific membership selected from that metadata so a later query can
reproduce exactly which OSI symbols were used for a session.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from lake.catalog import ContractRecord, ContractRepository


def _canonical_underlying(underlying: str) -> str:
    canonical = underlying.strip().upper()
    if not canonical:
        raise ValueError("underlying must not be empty")
    return canonical


@dataclass(frozen=True, slots=True)
class DailyFrontSet:
    """The immutable OSI membership selected for one underlying and date."""

    underlying: str
    selection_date: date
    contracts: tuple[ContractRecord, ...]

    def __post_init__(self) -> None:
        if isinstance(self.selection_date, datetime):
            raise TypeError("selection_date must be a date, not a datetime")

        underlying = _canonical_underlying(self.underlying)
        contracts = tuple(self.contracts)
        if any(contract.underlying != underlying for contract in contracts):
            raise ValueError("front-set contracts must share the selected underlying")
        symbols = [contract.symbol for contract in contracts]
        if len(set(symbols)) != len(symbols):
            raise ValueError("front-set contracts must not contain duplicate symbols")

        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(
            self,
            "contracts",
            tuple(sorted(contracts, key=_contract_sort_key)),
        )

    @property
    def key(self) -> tuple[str, date]:
        """Return the stable logical key for this daily front set."""
        return self.underlying, self.selection_date

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return selected OSI symbols in deterministic order."""
        return tuple(contract.symbol for contract in self.contracts)

    @property
    def expiration_dates(self) -> tuple[date, ...]:
        """Return selected expiration dates in ascending order."""
        return tuple(sorted({contract.expiration_date for contract in self.contracts}))


def _contract_sort_key(
    contract: ContractRecord,
) -> tuple[date, object, str, str]:
    return (
        contract.expiration_date,
        contract.strike_price,
        contract.option_type.value,
        contract.symbol,
    )


class DailyFrontSetRepository(Protocol):
    """Persistence contract for date-specific front-set membership."""

    def get(self, underlying: str, selection_date: date) -> DailyFrontSet | None:
        """Return the selected front set for one underlying and date."""

    def upsert(self, front_set: DailyFrontSet) -> DailyFrontSet:
        """Replace the membership for the front set's logical key."""

    def list(
        self,
        *,
        underlying: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[DailyFrontSet, ...]:
        """Return matching front sets in underlying/date order."""


class InMemoryDailyFrontSetRepository:
    """Deterministic process-local storage for daily front sets."""

    def __init__(self, front_sets: Iterable[DailyFrontSet] = ()) -> None:
        self._front_sets = {front_set.key: front_set for front_set in front_sets}

    def get(self, underlying: str, selection_date: date) -> DailyFrontSet | None:
        return self._front_sets.get((_canonical_underlying(underlying), selection_date))

    def upsert(self, front_set: DailyFrontSet) -> DailyFrontSet:
        self._front_sets[front_set.key] = front_set
        return front_set

    def put(self, front_set: DailyFrontSet) -> DailyFrontSet:
        """Alias for callers that use put semantics for repository writes."""
        return self.upsert(front_set)

    def list(
        self,
        *,
        underlying: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[DailyFrontSet, ...]:
        if start is not None and end is not None and start > end:
            raise ValueError("start must be on or before end")
        canonical_underlying = (
            _canonical_underlying(underlying) if underlying is not None else None
        )
        return tuple(
            front_set
            for front_set in sorted(
                self._front_sets.values(), key=lambda item: item.key
            )
            if (
                canonical_underlying is None
                or front_set.underlying == canonical_underlying
            )
            and (start is None or front_set.selection_date >= start)
            and (end is None or front_set.selection_date <= end)
        )


class ContractsCatalog:
    """Expose T024 contract metadata and daily front-set membership together."""

    def __init__(
        self,
        contracts: ContractRepository,
        front_sets: DailyFrontSetRepository | None = None,
    ) -> None:
        self.contracts = contracts
        self.front_sets = (
            InMemoryDailyFrontSetRepository() if front_sets is None else front_sets
        )

    def get(self, symbol: str) -> ContractRecord | None:
        """Return contract metadata by OSI symbol."""
        return self.contracts.get(symbol)

    def upsert(self, record: ContractRecord) -> ContractRecord:
        """Persist contract metadata through the T024 repository contract."""
        return self.contracts.upsert(record)

    def list(
        self,
        *,
        underlying: str | None = None,
        as_of: date | None = None,
    ) -> tuple[ContractRecord, ...]:
        """Return active contract metadata for an underlying and date."""
        return self.contracts.list(underlying=underlying, as_of=as_of)

    def get_front_set(
        self, underlying: str, selection_date: date
    ) -> DailyFrontSet | None:
        """Return the persisted front set for an underlying and date."""
        return self.front_sets.get(underlying, selection_date)

    def upsert_front_set(self, front_set: DailyFrontSet) -> DailyFrontSet:
        """Replace the persisted membership for one underlying and date."""
        return self.front_sets.upsert(front_set)

    def list_front_sets(
        self,
        *,
        underlying: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[DailyFrontSet, ...]:
        """Query persisted front sets by underlying and inclusive date range."""
        return self.front_sets.list(underlying=underlying, start=start, end=end)


FrontSetRecord = DailyFrontSet
FrontSetRepository = DailyFrontSetRepository
InMemoryFrontSetRepository = InMemoryDailyFrontSetRepository
InMemoryFrontSetCatalog = InMemoryDailyFrontSetRepository


__all__ = [
    "ContractsCatalog",
    "DailyFrontSet",
    "DailyFrontSetRepository",
    "FrontSetRecord",
    "FrontSetRepository",
    "InMemoryDailyFrontSetRepository",
    "InMemoryFrontSetCatalog",
    "InMemoryFrontSetRepository",
]
