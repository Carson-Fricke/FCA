"""Idempotent, schema-preserving Parquet partition writes.

The writer produces one object for a symbol, session date, and dataset. Object
replacement is expressed through a small protocol so the local filesystem can
be tested without coupling the writer to a future blob-storage client.
"""

import hashlib
import io
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq

from lake.partition_paths import AssetClass, DataType, partition_path, symbol_filename
from lake.pyarrow_schemas import schema_for


@runtime_checkable
class ObjectStore(Protocol):
    """Storage boundary used by :class:`ParquetWriter`.

    ``replace`` has whole-object semantics: a successful call makes the
    supplied payload the complete contents at ``relative_path``.
    """

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Atomically replace the object at a repository-relative path."""


class LocalFilesystemStore:
    """Store objects below a local root using atomic filesystem replacement."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Write ``payload`` to ``relative_path`` and replace any prior file."""
        target = self._target(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def read(self, relative_path: str) -> bytes:
        """Read an object, primarily for local verification and development."""
        return self._target(relative_path).read_bytes()

    def _target(self, relative_path: str) -> Path:
        if (
            not relative_path
            or "\\" in relative_path
            or relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise ValueError("object path must be a non-empty relative POSIX path")

        relative = PurePosixPath(relative_path)
        if relative.is_absolute():
            raise ValueError("object path must be relative")
        target = (self._root / Path(*relative.parts)).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError("object path escapes the store root")
        return target


@dataclass(frozen=True, slots=True)
class ParquetMetadata:
    """Metadata returned for the exact bytes committed to an object store."""

    path: str
    rows: int
    bytes: int
    checksum: str


_NANOSECONDS_PER_SECOND: Final = 1_000_000_000
_ISO_FRACTION: Final = re.compile(r"\.(\d{1,9})(?:Z|[+-]\d{2}:?\d{2})$")


def normalize_records(
    records: Iterable[Mapping[str, object]],
    *,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: str,
) -> pa.Table:
    """Normalize records into the stable Arrow schema for one partition.

    The partition symbol is authoritative. Input timestamps become UTC
    nanoseconds and decimal fields accept exact decimal or string values, but
    not binary floating-point values.
    """
    schema = schema_for(asset_class, data_type)
    canonical_symbol = _canonical_symbol(symbol)
    normalized_rows: list[dict[str, object]] = []

    for row_number, record in enumerate(records):
        normalized: dict[str, object] = dict(record)
        input_symbol = normalized.get("symbol")
        if input_symbol is not None:
            if not isinstance(input_symbol, str):
                raise TypeError(f"record {row_number} symbol must be a string")
            if _canonical_symbol(input_symbol) != canonical_symbol:
                raise ValueError(
                    f"record {row_number} symbol does not match partition symbol"
                )
        normalized["symbol"] = canonical_symbol

        for field in schema:
            value = normalized.get(field.name)
            if value is None:
                if not field.nullable:
                    raise ValueError(
                        f"record {row_number} is missing required field {field.name!r}"
                    )
                continue
            normalized[field.name] = _normalize_value(value, field.type, field.name)

        normalized_rows.append(normalized)

    return pa.Table.from_pylist(normalized_rows, schema=schema)


class ParquetWriter:
    """Serialize normalized records and replace their deterministic object."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def write(
        self,
        records: Iterable[Mapping[str, object]],
        *,
        session_date: date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
    ) -> ParquetMetadata:
        """Write one zstd-compressed Parquet object and return its metadata."""
        table = normalize_records(
            records,
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        output = io.BytesIO()
        pq.write_table(
            table,
            output,
            compression="zstd",
            use_compliant_nested_type=False,
            version="2.6",
        )
        payload = output.getvalue()
        path = partition_path(
            session_date=session_date,
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        self._store.replace(path, payload)
        return ParquetMetadata(
            path=path,
            rows=table.num_rows,
            bytes=len(payload),
            checksum=hashlib.sha256(payload).hexdigest(),
        )


def _canonical_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise TypeError("market symbol must be a string")
    canonical = symbol.strip().upper()
    symbol_filename(canonical)
    return canonical


def _normalize_value(value: object, data_type: pa.DataType, field_name: str) -> object:
    if pa.types.is_timestamp(data_type):
        return _timestamp_nanoseconds(value, field_name)
    if pa.types.is_decimal(data_type):
        return _decimal(value, field_name)
    if pa.types.is_list(data_type):
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{field_name} must be a list or tuple")
        return list(value)
    return value


def _timestamp_nanoseconds(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an aware datetime or UTC nanoseconds")
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        return _datetime_nanoseconds(value, field_name)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
        nanoseconds = _datetime_nanoseconds(parsed, field_name)
        fraction_match = _ISO_FRACTION.search(value)
        if fraction_match is not None:
            nanoseconds += int(fraction_match.group(1).ljust(9, "0")) - (
                parsed.microsecond * 1_000
            )
        return nanoseconds
    raise TypeError(f"{field_name} must be an aware datetime or UTC nanoseconds")


def _datetime_nanoseconds(value: datetime, field_name: str) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = utc_value - epoch
    return (
        elapsed.days * 86_400 * _NANOSECONDS_PER_SECOND
        + elapsed.seconds * _NANOSECONDS_PER_SECOND
        + elapsed.microseconds * 1_000
    )


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be an exact decimal value, not float")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(f"{field_name} must be an exact decimal value")
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an exact decimal value") from error
