"""On-demand local ensure execution backed by bounded Alpaca fetches.

The local market-lake API previously installed a no-op ensure launcher, so a
coverage request for symbols that had never been ingested could never be
satisfied.  This module supplies the real local launcher: it maps one
``EnsureJob`` onto the arbitrary-symbol equity or crypto backfill, so the
harness (or any published lake client) can request data it has not fetched
before and receive it instead of a permanent "missing symbol" failure.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime

from lake.contracts import AssetClass
from lake.coverage.application import EnsureJob
from lake.local.ingest import (
    AlpacaCryptoProvider,
    AlpacaEquityProvider,
    LocalIngestError,
    run_local_backfill,
    run_local_crypto_backfill,
)
from lake.local.state import LocalLayout

#: The ensure route waits this long inside the request before returning 202.
#: Local fetches are long, so the wait is deliberately short: the launcher runs
#: in a worker thread, the request returns promptly, and the harness polls the
#: catalog coverage until the requested sessions are terminal.
DEFAULT_ENSURE_DEADLINE_SECONDS = 10.0


class LocalEnsureLauncher:
    """Run a local ensure request as a bounded provider fetch.

    The launcher is callable so it satisfies the published
    :data:`lake.coverage.application.EnsureJobLauncher` contract; the ensure
    route invokes it in a worker thread and persists the resulting coverage in
    the local catalog.
    """

    def __init__(
        self,
        layout: LocalLayout,
        *,
        environ: Mapping[str, str] | None = None,
        now: datetime | None = None,
        equity_provider: AlpacaEquityProvider | None = None,
        crypto_provider: AlpacaCryptoProvider | None = None,
    ) -> None:
        self._layout = layout
        self._environ = environ
        self._now = now
        self._equity_provider = equity_provider
        self._crypto_provider = crypto_provider

    async def __call__(self, job: EnsureJob) -> None:
        request = job.request
        if request.asset_class is AssetClass.EQUITY:
            await run_local_backfill(
                layout=self._layout,
                symbols=[request.symbol],
                start_date=request.start_date,
                end_date=request.end_date,
                provider=self._equity_provider,
                environ=self._environ,
                now=self._now,
            )
            return
        if request.asset_class is AssetClass.CRYPTO:
            await run_local_crypto_backfill(
                layout=self._layout,
                pairs=[request.symbol],
                start_date=request.start_date,
                end_date=request.end_date,
                provider=self._crypto_provider,
                environ=self._environ,
                now=self._now,
            )
            return
        raise LocalIngestError(
            f"local ensure does not support {request.asset_class.value} data"
        )


def ensure_deadline_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Return the configured ensure wait deadline for the local API."""
    environment = os.environ if environ is None else environ
    raw = environment.get("LAKE_LOCAL_ENSURE_DEADLINE_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_ENSURE_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise LocalIngestError(
            "LAKE_LOCAL_ENSURE_DEADLINE_SECONDS must be a number"
        ) from error
    if value <= 0:
        raise LocalIngestError("LAKE_LOCAL_ENSURE_DEADLINE_SECONDS must be positive")
    return value


__all__ = [
    "DEFAULT_ENSURE_DEADLINE_SECONDS",
    "LocalEnsureLauncher",
    "ensure_deadline_seconds",
]
