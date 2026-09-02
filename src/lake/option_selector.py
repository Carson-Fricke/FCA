"""Deterministic daily option front-set selection.

The selector is pure apart from an explicit repository method.  It accepts
already normalized T024 contract records and does not create provider or Azure
clients, which keeps daily membership reproducible in tests and workers.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from lake.catalog import ContractRecord, ContractRepository
from lake.contracts import (
    ContractsCatalog,
    DailyFrontSet,
    DailyFrontSetRepository,
)
from lake.universes import OptionSelection, UniverseSettings


def _canonical_underlying(underlying: str) -> str:
    canonical = underlying.strip().upper()
    if not canonical:
        raise ValueError("underlying must not be empty")
    return canonical


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a decimal number") from error
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return result


def _active_on(contract: ContractRecord, selection_date: date) -> bool:
    return (
        contract.listed_date is None or selection_date >= contract.listed_date
    ) and (contract.delisted_date is None or selection_date <= contract.delisted_date)


def _contract_sort_key(
    contract: ContractRecord,
) -> tuple[date, Decimal, str, str]:
    return (
        contract.expiration_date,
        contract.strike_price,
        contract.option_type.value,
        contract.symbol,
    )


class OptionChainSelector:
    """Select the configured nearest expirations and spot-relative strikes."""

    def __init__(self, selection: OptionSelection) -> None:
        if selection.frequency != "daily":
            raise ValueError("option selection frequency must be daily")
        if selection.expiration_order != "nearest":
            raise ValueError("option selection order must be nearest")
        if selection.strike_window.basis != "underlying_spot":
            raise ValueError("strike window basis must be underlying_spot")
        self.selection = selection

    @classmethod
    def from_universes(cls, settings: UniverseSettings) -> OptionChainSelector:
        """Construct a selector from T026 typed universe settings."""
        return cls(settings.options.selection)

    def select(
        self,
        *,
        underlying: str,
        selection_date: date,
        spot: Decimal | int | float | str,
        contracts: Iterable[ContractRecord],
    ) -> tuple[ContractRecord, ...]:
        """Return the deterministic daily front set from candidate contracts.

        Expiration and strike boundaries are inclusive.  Contracts that are
        expired or outside their listed/delisted lifecycle on ``selection_date``
        are excluded before the nearest expiration and strike filters apply.
        """
        if isinstance(selection_date, datetime):
            raise TypeError("selection_date must be a date, not a datetime")
        canonical_underlying = _canonical_underlying(underlying)
        spot_value = _decimal(spot, "spot")
        lower, upper = self._strike_bounds(spot_value)

        eligible = [
            contract
            for contract in contracts
            if contract.underlying == canonical_underlying
            and contract.expiration_date >= selection_date
            and _active_on(contract, selection_date)
        ]
        expiration_dates = sorted({contract.expiration_date for contract in eligible})[
            : self.selection.expiration_count
        ]
        selected_expirations = set(expiration_dates)

        return tuple(
            sorted(
                (
                    contract
                    for contract in eligible
                    if contract.expiration_date in selected_expirations
                    and lower <= contract.strike_price <= upper
                ),
                key=_contract_sort_key,
            )
        )

    def select_from_repository(
        self,
        repository: ContractRepository,
        *,
        underlying: str,
        selection_date: date,
        spot: Decimal | int | float | str,
    ) -> tuple[ContractRecord, ...]:
        """Select from active T024 contract metadata for one date."""
        return self.select(
            underlying=underlying,
            selection_date=selection_date,
            spot=spot,
            contracts=repository.list(
                underlying=underlying,
                as_of=selection_date,
            ),
        )

    def select_and_persist(
        self,
        catalog: ContractsCatalog,
        *,
        underlying: str,
        selection_date: date,
        spot: Decimal | int | float | str,
    ) -> DailyFrontSet:
        """Select active metadata and replace that day's persisted front set."""
        contracts = self.select_from_repository(
            catalog.contracts,
            underlying=underlying,
            selection_date=selection_date,
            spot=spot,
        )
        front_set = DailyFrontSet(
            underlying=underlying,
            selection_date=selection_date,
            contracts=contracts,
        )
        return catalog.upsert_front_set(front_set)

    def _strike_bounds(self, spot: Decimal) -> tuple[Decimal, Decimal]:
        below = Decimal(str(self.selection.strike_window.percent_below)) / 100
        above = Decimal(str(self.selection.strike_window.percent_above)) / 100
        return spot * (1 - below), spot * (1 + above)


def select_front_set(
    selection: OptionSelection,
    *,
    underlying: str,
    selection_date: date,
    spot: Decimal | int | float | str,
    contracts: Iterable[ContractRecord],
) -> tuple[ContractRecord, ...]:
    """Functional convenience wrapper around :class:`OptionChainSelector`."""
    return OptionChainSelector(selection).select(
        underlying=underlying,
        selection_date=selection_date,
        spot=spot,
        contracts=contracts,
    )


def select_and_persist_front_set(
    selector: OptionChainSelector,
    repository: ContractRepository,
    front_sets: DailyFrontSetRepository,
    *,
    underlying: str,
    selection_date: date,
    spot: Decimal | int | float | str,
) -> DailyFrontSet:
    """Select from T024 metadata and persist one daily front-set record."""
    contracts = selector.select_from_repository(
        repository,
        underlying=underlying,
        selection_date=selection_date,
        spot=spot,
    )
    return front_sets.upsert(
        DailyFrontSet(
            underlying=underlying,
            selection_date=selection_date,
            contracts=contracts,
        )
    )


__all__ = [
    "OptionChainSelector",
    "select_and_persist_front_set",
    "select_front_set",
]
