"""Generate the market-lake phase-0 parity oracle fixtures.

The fixtures freeze behavior of the reference Python implementation so the Rust
port can be checked against it. Run from the repository root with the project
virtualenv (the reference implementation must be importable)::

    uv run python src/lake/tests/oracle/generate.py

Outputs, all committed:

* ``contracts/tests/oracle/xnys_sessions.json`` — the exchange_calendars XNYS
  sessions (open/close UTC) for 2015-01-01..2027-12-31.
* ``catalog/tests/oracle/catalog_v1.json`` — a canonical ``catalog.json``
  written by the Python ``LocalCatalogRepository``.
* ``coverage/tests/oracle/ensure_job_ids.json`` — Python ``ensure_job_id``
  digests for representative specs.
* ``storage/tests/oracle/parquet/`` + ``parquet_manifest.json`` — Python
  ``ParquetWriter`` output for every pinned dataset with the logical rows.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from exchange_calendars import get_calendar
from lake.coverage.application import EnsureSpec, ensure_job_id
from lake.local.state import LocalCatalogRepository
from lake.storage.parquet import LocalFilesystemStore, ParquetWriter

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType, partition_path

HERE = Path(__file__).resolve().parent
LAKE_ROOT = HERE.parents[1]
CONTRACTS_ORACLE = LAKE_ROOT / "contracts" / "tests" / "oracle"
CATALOG_ORACLE = LAKE_ROOT / "catalog" / "tests" / "oracle"
COVERAGE_ORACLE = LAKE_ROOT / "coverage" / "tests" / "oracle"
STORAGE_ORACLE = LAKE_ROOT / "storage" / "tests" / "oracle"

SESSION_START = "2015-01-01"
SESSION_END = "2027-12-31"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def write_sessions() -> None:
    calendar = get_calendar("XNYS", start="2014-12-25", end="2028-01-07")
    labels = calendar.sessions_in_range(SESSION_START, SESSION_END)
    entries = [
        {
            "date": label.date().isoformat(),
            "open": calendar.session_open(label).isoformat().replace("+00:00", "Z"),
            "close": calendar.session_close(label).isoformat().replace("+00:00", "Z"),
        }
        for label in labels
    ]
    _write_json(CONTRACTS_ORACLE / "xnys_sessions.json", entries)


def write_catalog() -> None:
    root = Path(tempfile.mkdtemp(prefix="lake-oracle-catalog-"))
    try:
        repository = LocalCatalogRepository(root / "state" / "catalog.json")
        records = [
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
                object_path=partition_path(
                    session_date=date(2024, 1, 18),
                    asset_class=AssetClass.EQUITY,
                    data_type=DataType.TRADE,
                    symbol="AAPL",
                ),
                row_count=120,
                byte_size=4567,
                checksum="ab" * 32,
                error_message=None,
            ),
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.QUOTE,
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.PARTIAL,
                object_path=partition_path(
                    session_date=date(2024, 1, 18),
                    asset_class=AssetClass.EQUITY,
                    data_type=DataType.QUOTE,
                    symbol="AAPL",
                ),
                row_count=7,
                byte_size=890,
                checksum="cd" * 32,
                error_message=None,
            ),
            CoverageRecord(
                asset_class=AssetClass.CRYPTO,
                data_type=DataType.QUOTE,
                symbol="BTC/USD",
                session_date=date(2024, 1, 6),
                status=CoverageStatus.COMPLETE,
                object_path=partition_path(
                    session_date=date(2024, 1, 6),
                    asset_class=AssetClass.CRYPTO,
                    data_type=DataType.QUOTE,
                    symbol="BTC/USD",
                ),
                row_count=2001,
                byte_size=73456,
                checksum="ef" * 32,
                error_message=None,
            ),
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="SPY",
                session_date=date(2024, 1, 19),
                status=CoverageStatus.ERROR,
                object_path=None,
                row_count=0,
                byte_size=0,
                checksum=None,
                error_message="trade: AlpacaHTTPError: 503 Service Unavailable",
            ),
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.QUOTE,
                symbol="SPY",
                session_date=date(2024, 1, 22),
                status=CoverageStatus.EMPTY,
                object_path=partition_path(
                    session_date=date(2024, 1, 22),
                    asset_class=AssetClass.EQUITY,
                    data_type=DataType.QUOTE,
                    symbol="SPY",
                ),
                row_count=0,
                byte_size=0,
                checksum=None,
                error_message=None,
            ),
        ]
        repository.upsert_many(records)
        target = CATALOG_ORACLE / "catalog_v1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / "state" / "catalog.json", target)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_ensure_job_ids() -> None:
    specs = [
        EnsureSpec(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
            start_date=date(2024, 1, 18),
            end_date=date(2024, 1, 19),
        ),
        EnsureSpec(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.QUOTE,
            symbol="  spy ",
            start_date=date(2024, 11, 27),
            end_date=date(2024, 11, 29),
        ),
        EnsureSpec(
            asset_class=AssetClass.CRYPTO,
            data_type=DataType.QUOTE,
            symbol="btc/usd",
            start_date=date(2024, 1, 5),
            end_date=date(2024, 1, 7),
        ),
    ]
    _write_json(
        COVERAGE_ORACLE / "ensure_job_ids.json",
        [
            {
                "asset_class": spec.asset_class.value,
                "data_type": spec.data_type.value,
                "symbol": spec.symbol,
                "start_date": spec.start_date.isoformat(),
                "end_date": spec.end_date.isoformat(),
                "job_id": ensure_job_id(spec),
            }
            for spec in specs
        ],
    )


PARQUET_ROWS: dict[tuple[AssetClass, DataType, str], list[dict[str, Any]]] = {
    (AssetClass.EQUITY, DataType.TRADE, "AAPL"): [
        {
            "timestamp": datetime(2024, 1, 18, 14, 30, 0, 123456, tzinfo=UTC),
            "symbol": "AAPL",
            "trade_id": 7,
            "exchange": "N",
            "price": Decimal("123.45"),
            "size": 100,
            "conditions": ["@", "F"],
            "tape": "C",
            "update": None,
        },
        {
            "timestamp": datetime(2024, 1, 18, 15, 0, 0, tzinfo=UTC),
            "symbol": "AAPL",
            "trade_id": 8,
            "exchange": "Q",
            "price": Decimal("124.5"),
            "size": 250,
            "conditions": [],
            "tape": "C",
            "update": "T",
        },
    ],
    (AssetClass.EQUITY, DataType.QUOTE, "AAPL"): [
        {
            "timestamp": datetime(2024, 1, 18, 14, 30, 0, tzinfo=UTC),
            "symbol": "AAPL",
            "bid_exchange": "V",
            "bid_price": Decimal("123.4"),
            "bid_size": 3,
            "ask_exchange": "V",
            "ask_price": Decimal("123.5"),
            "ask_size": 4,
            "conditions": ["R"],
            "tape": "C",
        }
    ],
    (AssetClass.OPTION, DataType.TRADE, "AAPL240119C00150000"): [
        {
            "timestamp": datetime(2024, 1, 18, 15, 30, 0, tzinfo=UTC),
            "symbol": "AAPL240119C00150000",
            "exchange": "M",
            "price": Decimal("5.25"),
            "size": 2,
            "condition": "A",
        }
    ],
    (AssetClass.OPTION, DataType.QUOTE, "AAPL240119P00145000"): [
        {
            "timestamp": datetime(2024, 1, 18, 15, 31, 0, tzinfo=UTC),
            "symbol": "AAPL240119P00145000",
            "bid_exchange": "P",
            "bid_price": Decimal("4.1"),
            "bid_size": 10,
            "ask_exchange": "P",
            "ask_price": Decimal("4.3"),
            "ask_size": 12,
            "condition": "A",
        }
    ],
    (AssetClass.CRYPTO, DataType.TRADE, "BTC/USD"): [
        {
            "timestamp": datetime(2024, 1, 6, 0, 0, 1, tzinfo=UTC),
            "symbol": "BTC/USD",
            "trade_id": 42,
            "exchange": "CBSE",
            "price": Decimal("44123.01"),
            "size": Decimal("0.15"),
            "taker_side": "B",
        },
        {
            "timestamp": datetime(2024, 1, 6, 0, 0, 2, tzinfo=UTC),
            "symbol": "BTC/USD",
            "trade_id": None,
            "exchange": None,
            "price": Decimal("44123.02"),
            "size": Decimal("0.2"),
            "taker_side": None,
        },
    ],
    (AssetClass.CRYPTO, DataType.QUOTE, "BTC/USD"): [
        {
            "timestamp": datetime(2024, 1, 6, 0, 0, 3, tzinfo=UTC),
            "symbol": "BTC/USD",
            "bid_exchange": None,
            "bid_price": Decimal("44120"),
            "bid_size": Decimal("1.5"),
            "ask_exchange": None,
            "ask_price": Decimal("44125"),
            "ask_size": Decimal("2.25"),
        }
    ],
}

PARQUET_DATES = {
    (AssetClass.EQUITY, DataType.TRADE, "AAPL"): date(2024, 1, 18),
    (AssetClass.EQUITY, DataType.QUOTE, "AAPL"): date(2024, 1, 18),
    (AssetClass.OPTION, DataType.TRADE, "AAPL240119C00150000"): date(2024, 1, 18),
    (AssetClass.OPTION, DataType.QUOTE, "AAPL240119P00145000"): date(2024, 1, 18),
    (AssetClass.CRYPTO, DataType.TRADE, "BTC/USD"): date(2024, 1, 6),
    (AssetClass.CRYPTO, DataType.QUOTE, "BTC/USD"): date(2024, 1, 6),
}

FILE_NAMES = {
    (AssetClass.EQUITY, DataType.TRADE): "equity_trade.parquet",
    (AssetClass.EQUITY, DataType.QUOTE): "equity_quote.parquet",
    (AssetClass.OPTION, DataType.TRADE): "option_trade.parquet",
    (AssetClass.OPTION, DataType.QUOTE): "option_quote.parquet",
    (AssetClass.CRYPTO, DataType.TRADE): "crypto_trade.parquet",
    (AssetClass.CRYPTO, DataType.QUOTE): "crypto_quote.parquet",
}


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    converted: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            converted[key] = value.isoformat().replace("+00:00", "Z")
        elif isinstance(value, Decimal):
            converted[key] = str(value)
        elif isinstance(value, list):
            converted[key] = list(value)
        else:
            converted[key] = value
    return converted


def write_parquet() -> None:
    root = Path(tempfile.mkdtemp(prefix="lake-oracle-parquet-"))
    try:
        store = LocalFilesystemStore(root)
        writer = ParquetWriter(store)
        target_dir = STORAGE_ORACLE / "parquet"
        target_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for (asset_class, data_type, symbol), rows in PARQUET_ROWS.items():
            session_date = PARQUET_DATES[(asset_class, data_type, symbol)]
            writer.write(
                rows,
                session_date=session_date,
                asset_class=asset_class,
                data_type=data_type,
                symbol=symbol,
            )
            source = root / partition_path(
                session_date=session_date,
                asset_class=asset_class,
                data_type=data_type,
                symbol=symbol,
            )
            file_name = FILE_NAMES[(asset_class, data_type)]
            shutil.copyfile(source, target_dir / file_name)
            manifest.append(
                {
                    "asset_class": asset_class.value,
                    "data_type": data_type.value,
                    "symbol": symbol,
                    "session_date": session_date.isoformat(),
                    "file": f"parquet/{file_name}",
                    "rows": [_json_row(row) for row in rows],
                }
            )
        _write_json(STORAGE_ORACLE / "parquet_manifest.json", manifest)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    write_sessions()
    write_catalog()
    write_ensure_job_ids()
    write_parquet()
    print("phase-0 oracle fixtures written")


if __name__ == "__main__":
    main()
