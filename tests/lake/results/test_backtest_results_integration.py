"""Credential-guarded integration tests for the backtest results store.

Runs :class:`~harness.results.store.BacktestResultsStore` against the
production Azure blob and Table adapters. The suite stays network-free by
default: tests are skipped unless ``AZURE_STORAGE_CONNECTION_STRING`` is
set, optionally redirected to Azurite or another account through the
container and table name environment variables.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Final

import pytest
from azure.core.exceptions import ResourceExistsError
from azure.data.tables import TableClient
from azure.storage.blob import ContainerClient

from harness.results.store import (
    BacktestResultsStore,
    LakeRunCatalogRepository,
    RunBlobStore,
    RunCatalogRecord,
    RunCatalogRepository,
    RunStatus,
    run_prefix,
)
from lake.catalog import AzureTableRunRepository
from lake.storage import AzureReadableBlobStore

CONNECTION_STRING_ENV: Final = "AZURE_STORAGE_CONNECTION_STRING"
CONTAINER_ENV: Final = "FCA_RESULTS_BLOB_CONTAINER"
TABLE_ENV: Final = "FCA_RESULTS_RUN_TABLE"
CONTAINER_DEFAULT: Final = "backtests"
TABLE_DEFAULT: Final = "backtestruns"
RUN_ID: Final = "t191-integration"

requires_azure = pytest.mark.skipif(
    os.environ.get(CONNECTION_STRING_ENV) is None,
    reason=f"set {CONNECTION_STRING_ENV} to run the live Azure integration test",
)


def make_record() -> RunCatalogRecord:
    """Build one deterministic catalog row for the integration run."""
    return RunCatalogRecord(
        run_id=RUN_ID,
        status=RunStatus.COMPLETE,
        strategy_id="momentum",
        data_start=date(2024, 1, 2),
        data_end=date(2024, 1, 3),
        metrics={"sharpe": "1.2345", "max_drawdown": "-0.0825"},
    )


def make_artifacts() -> dict[str, bytes]:
    """Return the canonical two-artifact upload for the integration run."""
    return {"actions.parquet": b"actions-bytes", "fills.parquet": b"fills-bytes"}


@requires_azure
def test_backtest_results_store_round_trips_through_azure_adapters() -> None:
    connection_string = os.environ[CONNECTION_STRING_ENV]
    container = ContainerClient.from_connection_string(
        connection_string,
        container_name=os.environ.get(CONTAINER_ENV, CONTAINER_DEFAULT),
    )
    table = TableClient.from_connection_string(
        connection_string,
        table_name=os.environ.get(TABLE_ENV, TABLE_DEFAULT),
    )
    if not container.exists():
        container.create_container()
    try:
        table.create_table()
    except ResourceExistsError:
        pass

    blobs = AzureReadableBlobStore(container)
    catalog: RunCatalogRepository = LakeRunCatalogRepository(
        AzureTableRunRepository(table)
    )
    assert isinstance(blobs, RunBlobStore)
    store = BacktestResultsStore(blobs, catalog)
    prefix = f"{run_prefix(RUN_ID)}/"
    # Repeated invocations start from a clean, prefix-complete state.
    store.delete(RUN_ID)
    try:
        stored = store.put(make_record(), make_artifacts())

        assert store.get(RUN_ID) == stored
        fetched = store.fetch(RUN_ID)
        assert fetched.record == stored
        assert dict(fetched.artifacts) == make_artifacts()
        assert blobs.read(f"{prefix}fills.parquet") == b"fills-bytes"
        assert blobs.list_prefix(prefix) == (
            f"{prefix}actions.parquet",
            f"{prefix}fills.parquet",
        )
        assert store.delete(RUN_ID) is True
        assert store.get(RUN_ID) is None
        assert blobs.list_prefix(prefix) == ()
        assert store.delete(RUN_ID) is False
    finally:
        blobs.delete_prefix(prefix)
        catalog.delete(RUN_ID)
