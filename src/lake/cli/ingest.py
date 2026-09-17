"""Production equity ingest process command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential

from lake.catalog import AzureTableCatalogRepository
from lake.configuration import load_settings, load_universes
from lake.coverage import CoverageSession, equity_sessions
from lake.ingestion.azure import AzureAtomicIngestCommitter
from lake.ingestion.equities.transaction import EquityIngestTransaction
from lake.ingestion.leased import LeasedAtomicIngestCommitter
from lake.providers.alpaca import AlpacaClient
from lake.storage import AzureBlobStore
from lake.storage.leases import AzureBlobLeaseProvider

_DEFAULT_COVERAGE_TABLE = "coverage"
_DEFAULT_LOOKBACK_DAYS = 31
_DEFAULT_STORAGE_CONTAINER = "lake"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lake ingest",
        description="Run the market-lake ingest process.",
        epilog="Fetches and persists one Alpaca equity session.",
    )
    parser.add_argument(
        "--symbol",
        help="equity symbol; defaults to the first configured soak symbol",
    )
    parser.add_argument(
        "--session-date",
        type=date.fromisoformat,
        help="closed XNYS session date; defaults to the latest closed session",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one real-provider equity ingest operation."""
    arguments = _parser().parse_args(argv)
    result = asyncio.run(
        run_ingest(symbol=arguments.symbol, session_date=arguments.session_date)
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


async def run_ingest(
    *,
    symbol: str | None = None,
    session_date: date | None = None,
) -> dict[str, object]:
    """Fetch, persist, and summarize one configured equity session."""
    settings = load_settings()
    universes = load_universes()
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        raise RuntimeError(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET are required"
        )

    selected_symbol = (
        universes.equities.soak.symbols[0] if symbol is None else symbol.strip().upper()
    )
    if selected_symbol not in universes.equities.soak.symbols:
        raise ValueError(
            f"symbol is not in the configured equity universe: {selected_symbol}"
        )

    now = datetime.now(UTC)
    session = _select_session(session_date, now)
    storage_account = os.environ.get("LAKE_STORAGE_ACCOUNT_NAME")
    if not storage_account:
        raise RuntimeError("LAKE_STORAGE_ACCOUNT_NAME is required for Azure ingest")

    credential = DefaultAzureCredential(
        managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID")
    )
    container_name = os.environ.get(
        "LAKE_STORAGE_CONTAINER", _DEFAULT_STORAGE_CONTAINER
    )
    blob_url = f"https://{storage_account}.blob.core.windows.net"
    table_url = f"https://{storage_account}.table.core.windows.net"
    blob_store = AzureBlobStore.from_account_url(
        blob_url, container_name, credential=credential
    )
    catalog = AzureTableCatalogRepository(
        TableClient(
            endpoint=table_url,
            table_name=os.environ.get("LAKE_COVERAGE_TABLE", _DEFAULT_COVERAGE_TABLE),
            credential=credential,
        )
    )
    lease_provider = AzureBlobLeaseProvider.from_account_url(
        blob_url, container_name, credential=credential
    )
    committer = LeasedAtomicIngestCommitter(
        AzureAtomicIngestCommitter(object_store=blob_store, catalog=catalog),
        lease_provider,
    )
    client = AlpacaClient.from_settings(settings.provider)
    try:
        transaction = EquityIngestTransaction(
            client=client,
            committer=committer,
        )
        ingest_result = await transaction.run(selected_symbol, session)
    finally:
        await client.aclose()

    return {
        "coverage": [
            {
                "data_type": record.data_type.value,
                "rows": record.row_count,
                "status": record.status.value,
            }
            for record in ingest_result.coverage
        ],
        "objects": [
            {"bytes": item.bytes, "path": item.path, "rows": item.rows}
            for item in ingest_result.objects
        ],
        "session_date": ingest_result.session_date.isoformat(),
        "symbol": ingest_result.symbol,
    }


def _select_session(session_date: date | None, now: datetime) -> CoverageSession:
    if session_date is None:
        sessions = equity_sessions(
            now.date() - timedelta(days=_DEFAULT_LOOKBACK_DAYS),
            now.date(),
            as_of=now,
        )
        closed = [session for session in sessions if session.close_time <= now]
        if not closed:
            raise RuntimeError("no closed XNYS session is available")
        return max(closed, key=lambda session: session.session_date)

    sessions = equity_sessions(session_date, session_date, as_of=now)
    if not sessions or sessions[0].close_time > now:
        raise ValueError("session-date must identify a closed XNYS session")
    return sessions[0]
