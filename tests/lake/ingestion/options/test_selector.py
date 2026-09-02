from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from lake.catalog import ContractRecord, ContractType, InMemoryContractRepository
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryFrontSetCatalog,
)
from lake.configuration import load_universes
from lake.ingestion.options.selector import OptionChainSelector, select_front_set

ROOT = Path(__file__).parents[4]
SELECTION_DATE = date(2024, 1, 8)


def _contract(
    symbol: str,
    expiration: date,
    strike: str,
    option_type: ContractType = ContractType.CALL,
    *,
    underlying: str = "SPY",
    listed: date | None = None,
    delisted: date | None = None,
) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying=underlying,
        expiration_date=expiration,
        strike_price=Decimal(strike),
        option_type=option_type,
        listed_date=listed,
        delisted_date=delisted,
    )


@pytest.fixture
def candidates() -> tuple[ContractRecord, ...]:
    return (
        _contract("SPY240119P00450000", date(2024, 1, 19), "450", ContractType.PUT),
        _contract("SPY240112C00495000", date(2024, 1, 12), "495"),
        _contract("SPY240112C00450000", date(2024, 1, 12), "450"),
        _contract("SPY240112C00550000", date(2024, 1, 12), "550"),
        _contract("SPY240119C00550000", date(2024, 1, 19), "550"),
        _contract("SPY240126C00500000", date(2024, 1, 26), "500"),
        _contract(
            "SPY240105C00500000",
            date(2024, 1, 5),
            "500",
            delisted=date(2024, 1, 5),
        ),
        _contract(
            "SPY240119C00500000",
            date(2024, 1, 19),
            "500",
            listed=date(2024, 1, 9),
        ),
        _contract(
            "QQQ240112C00495000",
            date(2024, 1, 12),
            "495",
            underlying="QQQ",
        ),
    )


def test_daily_selector_uses_nearest_two_expirations_and_inclusive_spot_window(
    candidates: tuple[ContractRecord, ...],
) -> None:
    selector = OptionChainSelector(load_universes(ROOT).options.selection)

    selected = selector.select(
        underlying="spy",
        selection_date=SELECTION_DATE,
        spot=Decimal("500"),
        contracts=reversed(candidates),
    )

    assert [contract.symbol for contract in selected] == [
        "SPY240112C00450000",
        "SPY240112C00495000",
        "SPY240112C00550000",
        "SPY240119P00450000",
        "SPY240119C00550000",
    ]
    assert {contract.expiration_date for contract in selected} == {
        date(2024, 1, 12),
        date(2024, 1, 19),
    }


def test_selector_is_deterministic_and_rejects_invalid_spot(
    candidates: tuple[ContractRecord, ...],
) -> None:
    selection = load_universes(ROOT).options.selection
    forward = select_front_set(
        selection,
        underlying="SPY",
        selection_date=SELECTION_DATE,
        spot=500,
        contracts=candidates,
    )
    backward = select_front_set(
        selection,
        underlying="SPY",
        selection_date=SELECTION_DATE,
        spot=500,
        contracts=reversed(candidates),
    )

    assert forward == backward
    with pytest.raises(ValueError, match="spot must be a finite positive number"):
        select_front_set(
            selection,
            underlying="SPY",
            selection_date=SELECTION_DATE,
            spot=0,
            contracts=candidates,
        )


def test_catalog_persists_and_queries_daily_front_sets_by_underlying_date(
    candidates: tuple[ContractRecord, ...],
) -> None:
    contract_repository = InMemoryContractRepository(candidates)
    front_sets = InMemoryFrontSetCatalog()
    catalog = ContractsCatalog(contract_repository, front_sets)
    selector = OptionChainSelector.from_universes(load_universes(ROOT))

    persisted = selector.select_and_persist(
        catalog,
        underlying="SPY",
        selection_date=SELECTION_DATE,
        spot=Decimal("500"),
    )

    assert persisted == catalog.get_front_set("spy", SELECTION_DATE)
    assert catalog.list_front_sets(underlying="SPY") == (persisted,)
    assert catalog.list_front_sets(
        underlying="SPY",
        start=SELECTION_DATE,
        end=SELECTION_DATE,
    ) == (persisted,)
    assert catalog.get("spy240112c00495000") == contract_repository.get(
        "SPY240112C00495000"
    )


def test_daily_front_set_replaces_same_underlying_date_and_sorts_contracts() -> None:
    first = _contract("SPY240112P00495000", date(2024, 1, 12), "495", ContractType.PUT)
    second = _contract("SPY240112C00495000", date(2024, 1, 12), "495")
    repository = InMemoryFrontSetCatalog()

    original = repository.upsert(DailyFrontSet("spy", SELECTION_DATE, (first, second)))
    replacement = repository.upsert(DailyFrontSet("SPY", SELECTION_DATE, (second,)))

    assert original.symbols == (
        "SPY240112C00495000",
        "SPY240112P00495000",
    )
    assert repository.get("SPY", SELECTION_DATE) == replacement
    assert repository.list() == (replacement,)


def test_front_set_rejects_mixed_underlyings_and_duplicate_symbols() -> None:
    spy = _contract("SPY240112C00495000", date(2024, 1, 12), "495")
    qqq = ContractRecord(
        symbol="QQQ240112C00495000",
        underlying="QQQ",
        expiration_date=date(2024, 1, 12),
        strike_price=Decimal("495"),
        option_type=ContractType.CALL,
    )

    with pytest.raises(ValueError, match="share the selected underlying"):
        DailyFrontSet("SPY", SELECTION_DATE, (spy, qqq))
    with pytest.raises(ValueError, match="duplicate symbols"):
        DailyFrontSet("SPY", SELECTION_DATE, (spy, spy))
