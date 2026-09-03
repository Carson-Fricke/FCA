"""DuckDB readers for API-issued Parquet file responses."""

from __future__ import annotations

from collections.abc import Iterable

import duckdb

from lakeclient.models import FileQueryFile


def read_files(
    files: Iterable[FileQueryFile],
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> duckdb.DuckDBPyRelation:
    """Read API-returned Parquet URLs without constructing storage credentials."""
    urls = tuple(file.read_url for file in files)
    if not urls:
        raise ValueError("at least one file response is required")
    active_connection = connection or duckdb.connect(database=":memory:")
    return active_connection.from_parquet(list(urls))


def nbbo_spread(
    files: Iterable[FileQueryFile],
    *,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> duckdb.DuckDBPyRelation:
    """Read quote files and project their row-level NBBO bid/ask spread."""
    quotes = read_files(files, connection=connection)
    required = {"timestamp", "symbol", "bid_price", "ask_price"}
    missing = required.difference(quotes.columns)
    if missing:
        missing_columns = ", ".join(sorted(missing))
        raise ValueError(f"quote files are missing NBBO columns: {missing_columns}")
    return quotes.project(
        '"timestamp", "symbol", "bid_price", "ask_price", '
        '"ask_price" - "bid_price" AS "nbbo_spread"'
    )


__all__ = ["nbbo_spread", "read_files"]
