from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from lake.reference.schemas import (
    CLASSIFICATION_SCHEMA,
    FUNDAMENTALS_SCHEMA,
    REFERENCE_SCHEMA_VERSION,
    classification_table,
    fundamentals_table,
)


def _classification_rows() -> list[dict[str, object]]:
    return [
        {
            "file_creation_time": datetime(2024, 4, 19, 21, 20, tzinfo=UTC),
            "symbol": "MSFT",
            "security_name": "Microsoft Corporation Common Stock",
            "listing_exchange": "Q",
            "market_category": "Q",
            "etf": False,
            "round_lot_size": Decimal("100"),
            "test_issue": False,
            "financial_status": "Q",
        },
        {
            "file_creation_time": datetime(2024, 4, 19, 21, 20, tzinfo=UTC),
            "symbol": "AAPL",
            "security_name": "Apple Inc. Common Stock",
            "listing_exchange": "Q",
            "market_category": "Q",
            "etf": False,
            "round_lot_size": "100",
            "test_issue": False,
            "financial_status": None,
        },
    ]


def _fundamentals_rows() -> list[dict[str, object]]:
    return [
        {
            "cik": 320193,
            "taxonomy": "us-gaap",
            "tag": "Revenues",
            "unit": "USD",
            "period_end": "2023-12-30",
            "fy": 2024,
            "fp": "Q1",
            "form": "10-Q",
            "accn": "0000320193-24-000006",
            "filed": "2024-02-02",
            "frame": "CY2023Q4",
            "val": Decimal("119575000000"),
        },
        {
            "cik": 320193,
            "taxonomy": "dei",
            "tag": "EntityCommonStockSharesOutstanding",
            "unit": "shares",
            "period_end": date(2023, 10, 20),
            "fy": None,
            "fp": None,
            "form": "10-K",
            "accn": "0000320193-23-000106",
            "filed": date(2023, 11, 3),
            "frame": None,
            "val": 15464661000,
        },
    ]


def test_reference_schemas_are_versioned() -> None:
    for schema in (CLASSIFICATION_SCHEMA, FUNDAMENTALS_SCHEMA):
        assert schema.metadata == {
            b"lake.schema": schema.metadata[b"lake.schema"],
            b"lake.schema.version": REFERENCE_SCHEMA_VERSION.encode("ascii"),
        }
    assert CLASSIFICATION_SCHEMA.metadata[b"lake.schema"] == (
        b"reference.classification"
    )
    assert FUNDAMENTALS_SCHEMA.metadata[b"lake.schema"] == b"reference.fundamentals"


def test_reference_schemas_pin_the_d006_field_contracts() -> None:
    assert [
        (field.name, str(field.type), field.nullable) for field in CLASSIFICATION_SCHEMA
    ] == [
        ("file_creation_time", "timestamp[ns, tz=UTC]", False),
        ("symbol", "string", False),
        ("security_name", "string", False),
        ("listing_exchange", "string", False),
        ("market_category", "string", False),
        ("etf", "bool", False),
        ("round_lot_size", "decimal128(38, 18)", False),
        ("test_issue", "bool", False),
        ("financial_status", "string", True),
    ]
    assert [
        (field.name, str(field.type), field.nullable) for field in FUNDAMENTALS_SCHEMA
    ] == [
        ("cik", "uint64", False),
        ("taxonomy", "string", False),
        ("tag", "string", False),
        ("unit", "string", False),
        ("period_end", "date32[day]", False),
        ("fy", "int32", True),
        ("fp", "string", True),
        ("form", "string", False),
        ("accn", "string", False),
        ("filed", "date32[day]", False),
        ("frame", "string", True),
        ("val", "decimal128(38, 18)", False),
    ]


def test_classification_table_sorts_deterministically_by_symbol() -> None:
    table = classification_table(_classification_rows())

    assert table.column("symbol").to_pylist() == ["AAPL", "MSFT"]
    assert table.column("round_lot_size").to_pylist() == [
        Decimal("100.000000000000000000"),
        Decimal("100.000000000000000000"),
    ]
    assert table.column("financial_status").to_pylist() == [None, "Q"]
    assert table.column("file_creation_time").to_pylist() == [
        datetime(2024, 4, 19, 21, 20, tzinfo=UTC),
        datetime(2024, 4, 19, 21, 20, tzinfo=UTC),
    ]


def test_fundamentals_table_sorts_deterministically_by_identity() -> None:
    table = fundamentals_table(_fundamentals_rows())

    assert table.column("taxonomy").to_pylist() == ["dei", "us-gaap"]
    assert table.column("period_end").to_pylist() == [
        date(2023, 10, 20),
        date(2023, 12, 30),
    ]
    assert table.column("filed").to_pylist() == [
        date(2023, 11, 3),
        date(2024, 2, 2),
    ]
    assert table.column("val").to_pylist() == [
        Decimal("15464661000.000000000000000000"),
        Decimal("119575000000.000000000000000000"),
    ]
    assert table.column("fy").to_pylist() == [None, 2024]


def test_timestamp_normalization_requires_timezone_awareness() -> None:
    rows = _classification_rows()
    rows[0]["file_creation_time"] = datetime(2024, 4, 19, 17, 20)

    with pytest.raises(ValueError, match="timezone information"):
        classification_table(rows)


def test_missing_required_field_is_rejected() -> None:
    rows = _classification_rows()
    del rows[0]["symbol"]

    with pytest.raises(ValueError, match="missing required field 'symbol'"):
        classification_table(rows)

    fundamentals = _fundamentals_rows()
    del fundamentals[0]["val"]
    with pytest.raises(ValueError, match="missing required field 'val'"):
        fundamentals_table(fundamentals)


def test_nullable_fields_default_to_null() -> None:
    table = fundamentals_table(
        [
            {
                "cik": 320193,
                "taxonomy": "us-gaap",
                "tag": "Revenues",
                "unit": "USD",
                "period_end": "2023-12-30",
                "form": "10-Q",
                "accn": "0000320193-24-000006",
                "filed": "2024-02-02",
                "val": "1",
            }
        ]
    )

    row = table.to_pylist()[0]
    assert row["fy"] is None
    assert row["fp"] is None
    assert row["frame"] is None


def test_float_and_bool_inputs_follow_the_exact_decimal_rules() -> None:
    with pytest.raises(TypeError, match="cik must be an integer"):
        fundamentals_table([{**_fundamentals_rows()[0], "cik": True}])
    with pytest.raises(TypeError, match="etf must be a boolean"):
        classification_table([{**_classification_rows()[0], "etf": "N"}])
    table = fundamentals_table([{**_fundamentals_rows()[0], "val": 0.1}])
    assert table.column("val").to_pylist() == [Decimal("0.1")]
