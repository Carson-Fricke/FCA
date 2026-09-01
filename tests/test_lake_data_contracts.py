from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa
import pytest

from lake.partition_paths import (
    SUPPORTED_DATASETS,
    AssetClass,
    DataType,
    partition_path,
    symbol_filename,
)
from lake.pyarrow_schemas import SCHEMA_VERSION, SCHEMAS, schema_for


@pytest.mark.parametrize(
    ("asset_class", "data_type", "symbol", "filename"),
    [
        (AssetClass.EQUITY, DataType.TRADE, "AAPL", "AAPL.parquet"),
        (AssetClass.EQUITY, DataType.QUOTE, "brk.b", "BRK.B.parquet"),
        (
            AssetClass.OPTION,
            DataType.TRADE,
            "AAPL240119C00150000",
            "AAPL240119C00150000.parquet",
        ),
        (
            AssetClass.OPTION,
            DataType.QUOTE,
            "SPY240119P00450000",
            "SPY240119P00450000.parquet",
        ),
        (AssetClass.CRYPTO, DataType.TRADE, "BTC/USD", "BTC%2FUSD.parquet"),
        (AssetClass.CRYPTO, DataType.QUOTE, "eth/usd", "ETH%2FUSD.parquet"),
    ],
)
def test_every_dataset_has_a_date_first_hive_path(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: str,
    filename: str,
) -> None:
    assert (
        partition_path(
            session_date=date(2024, 1, 19),
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        == f"date=2024-01-19/asset_class={asset_class}/"
        f"data_type={data_type}/{filename}"
    )


def test_symbol_filenames_are_canonical_and_collision_safe() -> None:
    assert symbol_filename(" btc/usd ") == symbol_filename("BTC/USD")
    assert symbol_filename("BTC/USD") != symbol_filename("BTC-USD")


@pytest.mark.parametrize("symbol", ["", ".", "..", "BTC%2FUSD", "../AAPL"])
def test_symbol_filename_rejects_invalid_symbols(symbol: str) -> None:
    with pytest.raises(ValueError, match="invalid market symbol"):
        symbol_filename(symbol)


def test_partition_path_requires_a_session_date() -> None:
    with pytest.raises(TypeError, match="must be a date"):
        partition_path(
            session_date=datetime(2024, 1, 19, tzinfo=UTC),
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
        )


def _field_contract(schema: pa.Schema) -> list[tuple[str, str, bool]]:
    return [(field.name, str(field.type), field.nullable) for field in schema]


def test_schema_registry_is_complete_and_versioned() -> None:
    assert tuple(SCHEMAS) == SUPPORTED_DATASETS

    for (asset_class, data_type), schema in SCHEMAS.items():
        assert schema_for(asset_class, data_type) is schema
        assert schema.metadata == {
            b"lake.schema": f"{asset_class}.{data_type}".encode(),
            b"lake.schema.version": SCHEMA_VERSION.encode(),
        }


@pytest.mark.parametrize(
    ("asset_class", "data_type", "expected"),
    [
        (
            AssetClass.EQUITY,
            DataType.TRADE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("trade_id", "uint64", False),
                ("exchange", "string", False),
                ("price", "decimal128(38, 18)", False),
                ("size", "uint32", False),
                ("conditions", "list<item: string not null>", False),
                ("tape", "string", False),
                ("update", "string", True),
            ],
        ),
        (
            AssetClass.EQUITY,
            DataType.QUOTE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("bid_exchange", "string", False),
                ("bid_price", "decimal128(38, 18)", False),
                ("bid_size", "uint32", False),
                ("ask_exchange", "string", False),
                ("ask_price", "decimal128(38, 18)", False),
                ("ask_size", "uint32", False),
                ("conditions", "list<item: string not null>", False),
                ("tape", "string", False),
            ],
        ),
        (
            AssetClass.OPTION,
            DataType.TRADE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("exchange", "string", False),
                ("price", "decimal128(38, 18)", False),
                ("size", "uint32", False),
                ("condition", "string", False),
            ],
        ),
        (
            AssetClass.OPTION,
            DataType.QUOTE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("bid_exchange", "string", False),
                ("bid_price", "decimal128(38, 18)", False),
                ("bid_size", "uint32", False),
                ("ask_exchange", "string", False),
                ("ask_price", "decimal128(38, 18)", False),
                ("ask_size", "uint32", False),
                ("condition", "string", False),
            ],
        ),
        (
            AssetClass.CRYPTO,
            DataType.TRADE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("trade_id", "int64", True),
                ("exchange", "string", True),
                ("price", "decimal128(38, 18)", False),
                ("size", "decimal128(38, 18)", False),
                ("taker_side", "string", True),
            ],
        ),
        (
            AssetClass.CRYPTO,
            DataType.QUOTE,
            [
                ("timestamp", "timestamp[ns, tz=UTC]", False),
                ("symbol", "string", False),
                ("bid_exchange", "string", True),
                ("bid_price", "decimal128(38, 18)", False),
                ("bid_size", "decimal128(38, 18)", False),
                ("ask_exchange", "string", True),
                ("ask_price", "decimal128(38, 18)", False),
                ("ask_size", "decimal128(38, 18)", False),
            ],
        ),
    ],
)
def test_schema_field_contract_is_stable(
    asset_class: AssetClass,
    data_type: DataType,
    expected: list[tuple[str, str, bool]],
) -> None:
    assert _field_contract(schema_for(asset_class, data_type)) == expected


def test_crypto_schema_preserves_fractional_values() -> None:
    schema = schema_for(AssetClass.CRYPTO, DataType.TRADE)
    table = pa.Table.from_pylist(
        [
            {
                "timestamp": datetime(2024, 1, 19, 12, 30, tzinfo=UTC),
                "symbol": "BTC/USD",
                "trade_id": None,
                "exchange": None,
                "price": Decimal("41234.123456789012345678"),
                "size": Decimal("0.000000010000000000"),
                "taker_side": None,
            }
        ],
        schema=schema,
    )

    assert table.schema.equals(schema, check_metadata=True)
    assert table.column("price")[0].as_py() == Decimal("41234.123456789012345678")
    assert table.column("size")[0].as_py() == Decimal("0.000000010000000000")
