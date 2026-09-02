"""Bounded, provider-independent DuckDB queries over ensured Parquet files."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

import duckdb
import orjson
import pyarrow as pa
import pyarrow.ipc as ipc
from pydantic_core import to_jsonable_python

from lake.contracts import AssetClass, DataType, partition_path


class BoundedQueryError(ValueError):
    """Base class for an invalid or over-budget bounded query."""

    code: str = "bounded_query_error"


class SqlNotAllowedError(BoundedQueryError):
    """The statement is not a read-only query over the exposed relation."""

    code = "sql_not_allowed"


class StoragePathNotAllowedError(BoundedQueryError):
    """The query or ensured-file mapping attempted to escape the data root."""

    code = "storage_path_not_allowed"


class ScanLimitExceededError(BoundedQueryError):
    """The conservative catalog/file scan estimate exceeds its configured cap."""

    code = "scan_limit_exceeded"


class ResultLimitExceededError(BoundedQueryError):
    """The serialized query result exceeds its configured cap."""

    code = "result_limit_exceeded"


class QueryStorageUnavailableError(BoundedQueryError):
    """The configured local query root or an ensured file is unavailable."""

    code = "query_storage_unavailable"


class ForceOverrideNotAuthorizedError(BoundedQueryError):
    """A caller requested a cap override without server-side authorization."""

    code = "force_override_not_authorized"


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """Runtime byte limits for one bounded query."""

    max_scan_bytes: int
    max_result_bytes: int = 33_554_432

    def __post_init__(self) -> None:
        if self.max_scan_bytes <= 0 or self.max_result_bytes <= 0:
            raise ValueError("query byte limits must be positive")
        if self.max_result_bytes > self.max_scan_bytes:
            raise ValueError("result limit cannot exceed scan limit")


@dataclass(frozen=True, slots=True)
class EnsuredQueryFile:
    """One canonical catalog file made visible to the query relation."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    object_path: str
    byte_size: int

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise TypeError("session_date must be a date, not a datetime")
        if not isinstance(self.object_path, str):
            raise TypeError("object_path must be a string")
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise TypeError("byte_size must be an integer")
        if self.byte_size < 0:
            raise ValueError("byte_size must not be negative")
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(self, "data_type", DataType(self.data_type))
        object.__setattr__(self, "symbol", self.symbol.strip().upper())

    def expected_path(self) -> str:
        """Return the only object path valid for this request partition."""
        return partition_path(
            session_date=self.session_date,
            asset_class=self.asset_class,
            data_type=self.data_type,
            symbol=self.symbol,
        )


@dataclass(frozen=True, slots=True)
class BoundedQueryResult:
    """JSON rows and Arrow IPC bytes produced by one bounded query."""

    columns: tuple[str, ...]
    rows: list[dict[str, object]]
    arrow_bytes: bytes
    result_bytes: int
    estimated_scan_bytes: int
    forced: bool


@dataclass(frozen=True, slots=True)
class _SqlToken:
    kind: str
    value: str


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DANGEROUS_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "ALTER",
        "ATTACH",
        "BEGIN",
        "CALL",
        "CHECKPOINT",
        "COMMIT",
        "COPY",
        "CREATE",
        "CREDENTIAL",
        "DELETE",
        "DETACH",
        "DROP",
        "EXECUTE",
        "EXPORT",
        "GRANT",
        "IMPORT",
        "INSERT",
        "INSTALL",
        "LOAD",
        "MACRO",
        "MERGE",
        "PREPARE",
        "PRAGMA",
        "RESET",
        "REVOKE",
        "ROLLBACK",
        "SECRET",
        "SEQUENCE",
        "SET",
        "TRUNCATE",
        "UPDATE",
        "USE",
        "VACUUM",
    }
)
_EXTERNAL_SCAN_MARKERS: Final[tuple[str, ...]] = (
    "TABLE_FUNCTION",
    "READ_PARQUET",
    "PARQUET_SCAN",
    "READ_CSV",
    "CSV_SCAN",
    "READ_JSON",
    "JSON_SCAN",
    "HTTPFS",
    "HTTP_SCAN",
    "GENERATOR",
    "GENERATE_SERIES",
    "RANGE",
    "UNNEST",
)
_EXTERNAL_SCAN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "ARROW_SCAN",
        "GLOB",
        "HTTP_GET",
        "PARQUET_SCAN",
        "PARQUET_SCHEMA",
        "PARQUET_METADATA",
        "PARQUET_FILE_METADATA",
        "PARQUET_KV_METADATA",
        "QUERY_TABLE",
        "READ_BLOB",
        "READ_CSV",
        "READ_CSV_AUTO",
        "READ_JSON",
        "READ_JSON_AUTO",
        "READ_PARQUET",
        "READ_TEXT",
        "DELTA_SCAN",
        "ICEBERG_SCAN",
        "SQLITE_SCAN",
        "POSTGRES_SCAN",
        "MYSQL_SCAN",
        "ODBC_SCAN",
    }
)


def validate_read_only_sql(sql: str) -> str:
    """Validate one SELECT/CTE statement before giving it to DuckDB.

    The connection intentionally exposes one relation named ``data``.  In
    addition to rejecting mutating statements, this lexical guard rejects file
    table functions and direct path relations before DuckDB can resolve them.
    The query plan is checked again after the relation is installed.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlNotAllowedError("sql must be a non-empty read-only statement")
    tokens = _tokenize_sql(sql)
    if not tokens or tokens[0].kind != "IDENTIFIER":
        raise SqlNotAllowedError("only SELECT statements are allowed")
    if tokens[0].value not in {"SELECT", "WITH"}:
        raise SqlNotAllowedError("only SELECT statements are allowed")
    if any(token.value == ";" for token in tokens[:-1]):
        raise SqlNotAllowedError("multiple SQL statements are not allowed")
    if any(
        token.kind == "IDENTIFIER" and token.value in _DANGEROUS_KEYWORDS
        for token in tokens
    ):
        raise SqlNotAllowedError("mutating or administrative SQL is not allowed")

    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        if token.kind == "IDENTIFIER" and token.value in {
            "FROM",
            "JOIN",
        }:
            if next_token.kind == "STRING":
                raise StoragePathNotAllowedError(
                    "SQL cannot use a storage path as a relation"
                )
            if next_token.kind == "IDENTIFIER" and next_token.value in {
                "LATERAL",
            }:
                continue
        if (
            token.kind == "IDENTIFIER"
            and token.value in _EXTERNAL_SCAN_FUNCTIONS
            and next_token.value == "("
        ):
            raise StoragePathNotAllowedError(
                "SQL cannot invoke external storage table functions"
            )

    return sql.strip()


class BoundedDuckDBQuery:
    """Execute SQL against only the supplied, canonical local Parquet files."""

    def __init__(self, file_root: str | Path, limits: QueryLimits) -> None:
        self._root = Path(file_root).resolve()
        self._limits = limits

    def execute(
        self,
        sql: str,
        files: Sequence[EnsuredQueryFile],
        *,
        force: bool = False,
        force_authorized: bool = False,
        output: Literal["json", "arrow"] = "json",
    ) -> BoundedQueryResult:
        """Run one bounded SELECT over the supplied ensured files."""
        statement = validate_read_only_sql(sql)
        if force and not force_authorized:
            raise ForceOverrideNotAuthorizedError(
                "force override is not authorized for this query endpoint"
            )

        paths, estimated_scan_bytes = self._resolve_files(files)
        if not paths:
            raise QueryStorageUnavailableError(
                "no complete ensured files are available for this query"
            )
        if estimated_scan_bytes > self._limits.max_scan_bytes and not force:
            raise ScanLimitExceededError(
                f"estimated scan of {estimated_scan_bytes} bytes exceeds "
                f"the {self._limits.max_scan_bytes}-byte limit"
            )

        try:
            with duckdb.connect(database=":memory:") as connection:
                relation = connection.from_parquet([str(path) for path in paths])
                relation.create("data")
                connection.execute("SET enable_external_access = false")
                self._validate_query_plan(connection, statement)
                result = connection.execute(statement)
                reader = result.to_arrow_reader(batch_size=1024)
                columns = tuple(reader.schema.names)
                rows, arrow_bytes, result_bytes = _collect_result(
                    reader,
                    output=output,
                    max_result_bytes=self._limits.max_result_bytes,
                    force=force,
                )
        except BoundedQueryError:
            raise
        except duckdb.Error as error:
            raise SqlNotAllowedError(
                f"DuckDB rejected the SQL query: {error}"
            ) from error

        return BoundedQueryResult(
            columns=columns,
            rows=rows,
            arrow_bytes=arrow_bytes,
            result_bytes=result_bytes,
            estimated_scan_bytes=estimated_scan_bytes,
            forced=force,
        )

    def _resolve_files(
        self, files: Sequence[EnsuredQueryFile]
    ) -> tuple[tuple[Path, ...], int]:
        if not self._root.is_dir():
            raise QueryStorageUnavailableError("the local query root is unavailable")

        paths: list[Path] = []
        estimated_scan_bytes = 0
        seen: set[Path] = set()
        for file in files:
            if file.object_path != file.expected_path():
                raise StoragePathNotAllowedError(
                    "catalog object path does not match the canonical partition"
                )
            if (
                not file.object_path
                or "\\" in file.object_path
                or file.object_path.startswith("/")
                or any(part in {"", ".", ".."} for part in file.object_path.split("/"))
            ):
                raise StoragePathNotAllowedError("object path must be canonical")
            relative = PurePosixPath(file.object_path)
            candidate = (self._root / Path(*relative.parts)).resolve()
            if candidate != self._root and self._root not in candidate.parents:
                raise StoragePathNotAllowedError("object path escapes the query root")
            if candidate in seen:
                continue
            if not candidate.is_file():
                raise QueryStorageUnavailableError(
                    f"ensured query file is unavailable: {file.object_path}"
                )
            seen.add(candidate)
            paths.append(candidate)
            if file.byte_size < 0:
                raise StoragePathNotAllowedError(
                    "catalog byte size must not be negative"
                )
            estimated_scan_bytes += max(file.byte_size, candidate.stat().st_size)
        return tuple(paths), estimated_scan_bytes

    @staticmethod
    def _validate_query_plan(connection: duckdb.DuckDBPyConnection, sql: str) -> None:
        try:
            plan_rows = connection.execute(f"EXPLAIN {sql}").fetchall()
        except duckdb.Error as error:
            raise SqlNotAllowedError(
                f"DuckDB rejected the SQL query: {error}"
            ) from error
        plan = "\n".join(str(value) for row in plan_rows for value in row).upper()
        if any(marker in plan for marker in _EXTERNAL_SCAN_MARKERS):
            raise StoragePathNotAllowedError(
                "query plan contains an external storage scan"
            )
        if "SEQ_SCAN" not in plan or "DATA" not in plan:
            raise SqlNotAllowedError("SQL must read the ensured data relation")


def _collect_result(
    reader: Any,
    *,
    output: Literal["json", "arrow"],
    max_result_bytes: int,
    force: bool,
) -> tuple[list[dict[str, object]], bytes, int]:
    if output == "arrow":
        return _collect_arrow_result(reader, max_result_bytes, force)
    return _collect_json_result(reader, max_result_bytes, force)


def _collect_arrow_result(
    reader: Any, max_result_bytes: int, force: bool
) -> tuple[list[dict[str, object]], bytes, int]:
    output = pa.BufferOutputStream()
    with ipc.new_stream(output, reader.schema) as writer:
        for batch in reader:
            writer.write_batch(batch)
            if output.tell() > max_result_bytes and not force:
                raise ResultLimitExceededError(
                    f"result of more than {max_result_bytes} bytes exceeds the "
                    f"{max_result_bytes}-byte limit"
                )
    arrow_bytes = output.getvalue().to_pybytes()
    _check_result_size(len(arrow_bytes), max_result_bytes, force)
    return [], arrow_bytes, len(arrow_bytes)


def _collect_json_result(
    reader: Any, max_result_bytes: int, force: bool
) -> tuple[list[dict[str, object]], bytes, int]:
    rows: list[dict[str, object]] = []
    encoded_rows: list[bytes] = []
    result_bytes = 2
    for batch in reader:
        batch_rows = to_jsonable_python(
            pa.Table.from_batches([batch], schema=reader.schema).to_pylist()
        )
        if not isinstance(batch_rows, list) or any(
            not isinstance(row, dict) for row in batch_rows
        ):
            raise SqlNotAllowedError("query results must be row-shaped")
        for row in batch_rows:
            encoded = orjson.dumps(row)
            result_bytes += len(encoded) + (1 if rows else 0)
            _check_result_size(result_bytes, max_result_bytes, force)
            rows.append(row)
            encoded_rows.append(encoded)
    payload = b"[" + b",".join(encoded_rows) + b"]"
    _check_result_size(len(payload), max_result_bytes, force)
    return rows, b"", len(payload)


def _check_result_size(result_bytes: int, max_result_bytes: int, force: bool) -> None:
    if result_bytes > max_result_bytes and not force:
        raise ResultLimitExceededError(
            f"result of {result_bytes} bytes exceeds the {max_result_bytes}-byte limit"
        )


def _tokenize_sql(sql: str) -> tuple[_SqlToken, ...]:
    tokens: list[_SqlToken] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                raise SqlNotAllowedError("unterminated SQL comment")
            index = end + 2
            continue
        if character == "'":
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise SqlNotAllowedError("unterminated SQL string")
            tokens.append(_SqlToken("STRING", sql[start:index]))
            continue
        if character in {'"', "`"}:
            quote = character
            start = index
            index += 1
            while index < len(sql):
                if sql[index] == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise SqlNotAllowedError("unterminated SQL identifier")
            tokens.append(_SqlToken("QUOTED_IDENTIFIER", sql[start:index]))
            continue
        match = _IDENTIFIER.match(sql, index)
        if match is not None:
            tokens.append(_SqlToken("IDENTIFIER", match.group().upper()))
            index = match.end()
            continue
        tokens.append(_SqlToken("SYMBOL", character))
        index += 1
    return tuple(tokens)


__all__ = [
    "BoundedDuckDBQuery",
    "BoundedQueryError",
    "BoundedQueryResult",
    "EnsuredQueryFile",
    "ForceOverrideNotAuthorizedError",
    "QueryLimits",
    "QueryStorageUnavailableError",
    "ResultLimitExceededError",
    "ScanLimitExceededError",
    "SqlNotAllowedError",
    "StoragePathNotAllowedError",
    "validate_read_only_sql",
]
