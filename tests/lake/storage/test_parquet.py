import hashlib
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from lake.contracts import AssetClass, DataType, schema_for
from lake.storage import (
    LocalFilesystemStore,
    ObjectStore,
    ParquetWriter,
    normalize_records,
)


class RecordingStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.replacements: list[str] = []

    def replace(self, relative_path: str, payload: bytes) -> None:
        self.objects[relative_path] = payload
        self.replacements.append(relative_path)


def _equity_trade(symbol: str = "AAPL", price: object = "190.125") -> dict[str, object]:
    return {
        "timestamp": "2024-01-19T14:30:00.123456789Z",
        "symbol": symbol,
        "trade_id": 7,
        "exchange": "XNAS",
        "price": price,
        "size": 100,
        "conditions": ["@"],
        "tape": "C",
        "update": None,
    }


def test_normalize_records_canonicalizes_symbol_timestamp_and_decimal() -> None:
    table = normalize_records(
        [_equity_trade(" aapl ")],
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )

    assert table.schema.equals(
        schema_for(AssetClass.EQUITY, DataType.TRADE), check_metadata=True
    )
    assert table.column("symbol")[0].as_py() == "AAPL"
    assert table.column("timestamp")[0].value == 1_705_674_600_123_456_789
    assert table.column("price")[0].as_py() == Decimal("190.125000000000000000")


def test_writer_replaces_one_deterministic_object_and_returns_metadata() -> None:
    store = RecordingStore()
    writer = ParquetWriter(store)

    first = writer.write(
        [_equity_trade(price="190.125")],
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="aapl",
    )
    second = writer.write(
        [_equity_trade(price="191.250"), _equity_trade(price="191.500")],
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )

    assert (
        first.path
        == second.path
        == ("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet")
    )
    assert len(store.objects) == 1
    assert store.replacements == [first.path, second.path]
    assert second.rows == 2
    assert second.bytes == len(store.objects[second.path])
    assert second.checksum == hashlib.sha256(store.objects[second.path]).hexdigest()
    assert first.checksum != second.checksum


def test_local_store_round_trip_preserves_schema_and_zstd(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path)
    writer = ParquetWriter(store)
    writer.write(
        [_equity_trade()],
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )
    metadata = writer.write(
        [_equity_trade(price="191.125"), _equity_trade(price="191.250")],
        session_date=date(2024, 1, 19),
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="AAPL",
    )

    payload = store.read(metadata.path)
    parquet = pq.ParquetFile(tmp_path / metadata.path)
    assert len(payload) == metadata.bytes
    assert parquet.metadata.num_rows == metadata.rows == 2
    assert parquet.read().schema.equals(
        schema_for(AssetClass.EQUITY, DataType.TRADE), check_metadata=True
    )
    assert {
        parquet.metadata.row_group(0).column(index).compression
        for index in range(parquet.metadata.row_group(0).num_columns)
    } == {"ZSTD"}


def test_writer_round_trips_all_t021_dataset_schemas() -> None:
    records: list[tuple[AssetClass, DataType, str, dict[str, object]]] = [
        (
            AssetClass.EQUITY,
            DataType.TRADE,
            "AAPL",
            _equity_trade(),
        ),
        (
            AssetClass.EQUITY,
            DataType.QUOTE,
            "AAPL",
            {
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL",
                "bid_exchange": "XNAS",
                "bid_price": "190.10",
                "bid_size": 100,
                "ask_exchange": "XNAS",
                "ask_price": "190.20",
                "ask_size": 200,
                "conditions": ["R"],
                "tape": "C",
            },
        ),
        (
            AssetClass.OPTION,
            DataType.TRADE,
            "AAPL240119C00150000",
            {
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL240119C00150000",
                "exchange": "XOPR",
                "price": "1.25",
                "size": 10,
                "condition": "I",
            },
        ),
        (
            AssetClass.OPTION,
            DataType.QUOTE,
            "AAPL240119C00150000",
            {
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "AAPL240119C00150000",
                "bid_exchange": "XOPR",
                "bid_price": "1.20",
                "bid_size": 10,
                "ask_exchange": "XOPR",
                "ask_price": "1.30",
                "ask_size": 20,
                "condition": "I",
            },
        ),
        (
            AssetClass.CRYPTO,
            DataType.TRADE,
            "BTC/USD",
            {
                "timestamp": "2024-01-19T14:30:00.123456789Z",
                "symbol": "BTC/USD",
                "trade_id": None,
                "exchange": None,
                "price": Decimal("41234.123456789012345678"),
                "size": Decimal("0.000000010000000000"),
                "taker_side": None,
            },
        ),
        (
            AssetClass.CRYPTO,
            DataType.QUOTE,
            "BTC/USD",
            {
                "timestamp": "2024-01-19T14:30:00Z",
                "symbol": "BTC/USD",
                "bid_exchange": None,
                "bid_price": Decimal("41234.12"),
                "bid_size": Decimal("0.1"),
                "ask_exchange": None,
                "ask_price": Decimal("41235.12"),
                "ask_size": Decimal("0.2"),
            },
        ),
    ]
    store = RecordingStore()
    writer = ParquetWriter(store)

    for asset_class, data_type, symbol, record in records:
        metadata = writer.write(
            [record],
            session_date=date(2024, 1, 19),
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        table = pq.read_table(io.BytesIO(store.objects[metadata.path]))
        assert table.schema.equals(
            schema_for(asset_class, data_type), check_metadata=True
        )
        assert metadata.rows == 1

    assert len(store.objects) == len(records)


def test_normalization_rejects_symbol_mismatch_and_binary_float() -> None:
    with pytest.raises(ValueError, match="does not match"):
        normalize_records(
            [_equity_trade("MSFT")],
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
        )

    with pytest.raises(TypeError, match="exact decimal"):
        normalize_records(
            [_equity_trade(price=190.125)],
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
        )


def test_object_store_protocol_is_implemented_by_recording_store() -> None:
    assert isinstance(RecordingStore(), ObjectStore)
