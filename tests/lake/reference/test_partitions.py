from datetime import UTC, date, datetime

import pytest

from lake.contracts import DataType
from lake.reference.partitions import (
    CLASSIFICATION_OBJECT_NAME,
    cik_object_name,
    cik_symbol,
    parse_cik_symbol,
    reference_partition_path,
)


def test_reference_partition_paths_follow_the_date_first_hive_contract() -> None:
    assert (
        reference_partition_path(
            knowledge_date=date(2024, 4, 19),
            data_type=DataType.CLASSIFICATION,
            name=CLASSIFICATION_OBJECT_NAME,
        )
        == "date=2024-04-19/asset_class=reference/data_type=classification/SNAPSHOT.parquet"
    )
    assert reference_partition_path(
        knowledge_date=date(2024, 2, 2),
        data_type=DataType.FUNDAMENTALS,
        name=cik_object_name(320193),
    ) == (
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
        "CIK0000320193.parquet"
    )


def test_object_names_are_uppercased_and_reuse_symbol_filename_rules() -> None:
    path = reference_partition_path(
        knowledge_date=date(2024, 4, 19),
        data_type=DataType.CLASSIFICATION,
        name="snapshot",
    )
    assert path.endswith("/SNAPSHOT.parquet")


def test_non_reference_data_types_are_rejected() -> None:
    for data_type in (DataType.TRADE, DataType.QUOTE):
        with pytest.raises(ValueError, match="not a reference dataset"):
            reference_partition_path(
                knowledge_date=date(2024, 4, 19),
                data_type=data_type,
                name="SNAPSHOT",
            )


def test_knowledge_date_must_be_a_date() -> None:
    with pytest.raises(TypeError, match="must be a date, not a datetime"):
        reference_partition_path(
            knowledge_date=datetime(2024, 4, 19, tzinfo=UTC),  # type: ignore[arg-type]
            data_type=DataType.CLASSIFICATION,
            name="SNAPSHOT",
        )


def test_cik_symbols_are_zero_padded_ten_digit_canonical_values() -> None:
    assert cik_symbol(320193) == "0000320193"
    assert cik_object_name(320193) == "CIK0000320193"
    assert parse_cik_symbol("0000320193") == 320193
    assert parse_cik_symbol(cik_symbol(1)) == 1


@pytest.mark.parametrize("cik", [0, -1, True, 10**10])
def test_out_of_domain_ciks_are_rejected(cik: int) -> None:
    with pytest.raises(ValueError, match="CIK"):
        cik_symbol(cik)
    with pytest.raises(ValueError, match="CIK"):
        cik_object_name(cik)


@pytest.mark.parametrize("value", ["", "320193", "00032001930", "00A0320193"])
def test_invalid_cik_symbols_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="invalid reference CIK symbol"):
        parse_cik_symbol(value)
