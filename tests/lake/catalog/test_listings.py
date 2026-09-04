"""Focused tests for point-in-time security listing records and adapters."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode

from lake.catalog import (
    AzureTableListingRepository,
    DelistingType,
    InMemoryListingRepository,
    ListingRecord,
    ListingRepository,
    ListingType,
    listing_from_entity,
    listing_partition_key,
    listing_to_entity,
)


class FakeTable:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], TableEntity] = {}
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def get_entity(self, partition_key: str, row_key: str) -> TableEntity:
        try:
            return self.entities[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError("missing") from None

    def upsert_entity(self, entity: TableEntity, mode: UpdateMode) -> None:
        assert mode is UpdateMode.REPLACE
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = TableEntity(entity)

    def query_entities(
        self,
        query_filter: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> list[TableEntity]:
        self.queries.append((query_filter, parameters))
        if parameters is None:
            return list(self.entities.values())
        partition = parameters.get("partition")
        if partition is not None:
            return [
                entity
                for entity in self.entities.values()
                if entity["PartitionKey"] == partition
            ]
        return [
            entity
            for entity in self.entities.values()
            if str(entity["PartitionKey"]).startswith(parameters["start"])
            and str(entity["PartitionKey"]) < parameters["end"]
        ]


def _episode(
    listing_type: ListingType = ListingType.IPO,
    *,
    delisted: date | None = date(2022, 6, 8),
    delisting_type: DelistingType | None = DelistingType.RENAME,
    final_price: str | None = "196.35",
) -> ListingRecord:
    return ListingRecord(
        entity_id=" meta ",
        symbol=" fb ",
        listed_date=date(2012, 5, 18),
        delisted_date=delisted,
        listing_type=listing_type,
        delisting_type=delisting_type,
        final_price=None if final_price is None else Decimal(final_price),
    )


def _successor() -> ListingRecord:
    return ListingRecord(
        entity_id="META",
        symbol="META",
        listed_date=date(2022, 6, 9),
        listing_type=ListingType.RENAME,
    )


def _delisted_record(listing_type: ListingType) -> ListingRecord:
    return ListingRecord(
        entity_id="META",
        symbol="FB",
        listed_date=date(2012, 5, 18),
        delisted_date=date(2022, 6, 8),
        listing_type=listing_type,
        delisting_type=DelistingType.RENAME,
        final_price=Decimal("196.35"),
    )


@pytest.mark.parametrize("listing_type", list(ListingType))
@pytest.mark.parametrize("delisting_type", list(DelistingType))
def test_listing_round_trips_through_entity(
    listing_type: ListingType, delisting_type: DelistingType
) -> None:
    record = ListingRecord(
        entity_id="META",
        symbol="FB",
        listed_date=date(2012, 5, 18),
        delisted_date=date(2022, 6, 8),
        listing_type=listing_type,
        delisting_type=delisting_type,
        final_price=Decimal("196.35"),
    )

    entity = listing_to_entity(record)

    assert entity["PartitionKey"] == "listings|META"
    assert entity["RowKey"] == "FB"
    assert entity["FinalPrice"] == "196.35"
    assert listing_from_entity(entity) == record


@pytest.mark.parametrize("listing_type", list(ListingType))
def test_active_listing_round_trips_without_optional_fields(
    listing_type: ListingType,
) -> None:
    record = ListingRecord(
        entity_id="META",
        symbol="META",
        listed_date=date(2022, 6, 9),
        listing_type=listing_type,
    )

    assert listing_from_entity(listing_to_entity(record)) == record


@pytest.mark.parametrize("listing_type", list(ListingType))
def test_listing_round_trips_through_memory(listing_type: ListingType) -> None:
    record = _delisted_record(listing_type)
    repository = InMemoryListingRepository()

    repository.upsert(record)

    assert repository.get(record.entity_id, record.symbol) == record


def test_listing_record_canonicalizes_identity_and_keys() -> None:
    record = _episode()

    assert record.entity_id == "META"
    assert record.symbol == "FB"
    assert record.key == ("META", "FB")
    assert record.partition_key == "listings|META"
    assert record.row_key == "FB"
    assert listing_partition_key(record) == "listings|META"
    assert record.listing_type is ListingType.IPO
    assert record.delisting_type is DelistingType.RENAME
    assert record.final_price == Decimal("196.35")


@pytest.mark.parametrize(
    "overrides",
    [
        {"entity_id": "  "},
        {"listed_date": datetime(2012, 5, 18)},
        {"delisted_date": datetime(2022, 6, 8)},
        {"delisted_date": date(2012, 5, 17)},
        {"delisting_type": DelistingType.MERGER},
        {"delisted_date": date(2022, 6, 8)},
        {"final_price": Decimal("10")},
        {"final_price": Decimal("0")},
        {"final_price": "not-a-number"},
    ],
)
def test_invalid_listing_records_are_rejected(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "entity_id": "META",
        "symbol": "FB",
        "listed_date": date(2012, 5, 18),
    }
    values.update(overrides)

    with pytest.raises((ValueError, TypeError)):
        ListingRecord(**values)


def test_listing_activity_is_point_in_time() -> None:
    record = _episode()
    known_day = date(2024, 1, 2)

    assert record.is_known(date(2012, 5, 18))
    assert not record.is_known(date(2012, 5, 17))
    assert record.active_on(date(2020, 1, 2), as_of=known_day)
    assert record.active_on(date(2022, 6, 8), as_of=known_day)
    assert not record.active_on(date(2022, 6, 9), as_of=known_day)
    assert not record.active_on(date(2010, 1, 2), as_of=known_day)
    with pytest.raises(TypeError, match="must be dates"):
        record.active_on(datetime(2020, 1, 2), as_of=known_day)


def test_unknown_delisting_keeps_the_listing_active() -> None:
    pending = ListingRecord(
        entity_id="EARLY",
        symbol="EARLY",
        listed_date=date(2020, 2, 20),
        delisted_date=date(2026, 12, 31),
        delisting_type=DelistingType.VOLUNTARY,
        final_price=Decimal("10.00"),
    )

    assert pending.active_on(date(2024, 6, 28), as_of=date(2024, 6, 28))
    assert pending.active_on(date(2027, 1, 4), as_of=date(2024, 6, 28))
    assert not pending.active_on(date(2027, 1, 4), as_of=date(2027, 1, 4))
    assert pending.active_on(date(2024, 6, 28), as_of=date(2027, 1, 4))


def test_memory_repository_filters_sorts_and_respects_listing_knowledge() -> None:
    repository: ListingRepository = InMemoryListingRepository(
        [_episode(), _successor()]
    )

    assert repository.get("meta", "FB") == _episode()
    assert repository.get("META", "UNKNOWN") is None
    assert [record.symbol for record in repository.list()] == ["FB", "META"]
    assert [record.symbol for record in repository.list(entity_id="meta")] == [
        "FB",
        "META",
    ]
    assert repository.list(symbol="fb") == (_episode(),)
    assert repository.list(as_of=date(2020, 1, 2)) == (_episode(),)
    assert repository.list(as_of=date(2012, 5, 17)) == ()
    assert repository.list(as_of=date(2022, 6, 9)) == (_episode(), _successor())
    with pytest.raises(ValueError, match="entity_id must not be empty"):
        repository.get("  ", "FB")


def test_azure_listing_adapter_round_trips_without_live_azure() -> None:
    table = FakeTable()
    adapter = AzureTableListingRepository(cast(TableClient, table))
    record = _episode()

    assert adapter.upsert(record) == record
    assert ("listings|META", "FB") in table.entities
    assert adapter.get("meta", "fb") == record
    assert adapter.get("META", "MISSING") is None


def test_azure_listing_adapter_uses_partition_query_and_post_filters() -> None:
    table = FakeTable()
    adapter = AzureTableListingRepository(cast(TableClient, table))
    adapter.upsert(_episode())
    adapter.upsert(_successor())

    records = adapter.list(entity_id="META", symbol="FB", as_of=date(2020, 1, 2))

    assert records == (_episode(),)
    assert table.queries == [
        ("PartitionKey eq @partition", {"partition": "listings|META"})
    ]

    unqualified = AzureTableListingRepository(cast(TableClient, FakeTable()))
    unqualified.upsert(_episode())
    unqualified.upsert(_successor())

    assert [record.symbol for record in unqualified.list()] == ["FB", "META"]
    assert unqualified.list(entity_id="MISSING") == ()
