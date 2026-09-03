"""Bounded Parquet writing and row serialization helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from harness.core.types.common import text
from harness.results.recorder.config import _key_text


def _json_value(value: object, path: str) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating point")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value, path)
    if isinstance(value, Mapping):
        return {
            _key_text(key, f"{path}.key"): _json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, f"{path}[]") for item in value]
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _json_text(value: object, path: str = "value") -> str:
    return json.dumps(
        _json_value(value, path),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _optional_text(value: str | None, field_name: str) -> str | None:
    return None if value is None else text(value, field_name)


def _enum_value(value: object, enum_type: type[StrEnum], field_name: str) -> str:
    if isinstance(value, enum_type):
        return str(value.value)
    if isinstance(value, str):
        return str(enum_type(value).value)
    raise TypeError(f"{field_name} must be a {enum_type.__name__}")


class _ArtifactWriter:
    """Append rows to one Parquet artifact in bounded row batches."""

    __slots__ = (
        "_batch_size",
        "_compression",
        "_path",
        "_pending",
        "_row_count",
        "_schema",
        "_version",
        "_writer",
    )

    def __init__(
        self,
        path: Path,
        schema: pa.Schema,
        *,
        batch_size: int,
        compression: str,
        version: str,
    ) -> None:
        self._path = path
        self._schema = schema
        self._batch_size = batch_size
        self._compression = compression
        self._version = version
        self._pending: list[dict[str, object]] = []
        self._row_count = 0
        self._writer: pq.ParquetWriter | None = None

    @property
    def row_count(self) -> int:
        """Return rows already written to the artifact."""
        return self._row_count

    @property
    def pending_rows(self) -> int:
        """Return rows buffered in memory and not yet written."""
        return len(self._pending)

    def append(self, row: Mapping[str, object]) -> None:
        """Buffer one row and flush when the batch is full."""
        self._pending.append(dict(row))
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Write the pending batch as one row group."""
        if not self._pending:
            return
        table = pa.Table.from_pylist(self._pending, schema=self._schema)
        self._require_writer().write_table(table)
        self._row_count += table.num_rows
        self._pending.clear()

    def close(self) -> None:
        """Flush the pending batch and finalize the file."""
        self.flush()
        writer = self._require_writer()
        writer.close()
        self._writer = None

    def _require_writer(self) -> pq.ParquetWriter:
        if self._writer is None:
            self._writer = pq.ParquetWriter(
                self._path,
                self._schema,
                compression=self._compression,
                version=self._version,
            )
        return self._writer
