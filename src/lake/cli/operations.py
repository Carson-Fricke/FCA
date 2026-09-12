"""Daily coverage and budget report command for market-lake operators.

The command composes the report engine in :mod:`lake.operations` over local
market-lake state so an operator can emit (or deliver) the concise daily view
of freshness, failures, queue age, stored bytes, and budget pressure without
constructing Azure clients. The alert channel is only opened when
``LAKE_OPERATIONS__ALERT_URL`` names a webhook; otherwise the report is emitted
to stdout and a deterministic recording channel.
"""

from __future__ import annotations

import argparse
import os
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta

from lake.catalog.memory import InMemoryJobRepository, InMemoryListingRepository
from lake.configuration import load_settings
from lake.configuration.universes import load_universes
from lake.contracts import AssetClass
from lake.coverage import CoverageSession, calculate_coverage, equity_sessions
from lake.local.state import LocalCatalogRepository, LocalLayout
from lake.operations import (
    AlertChannel,
    HttpAlertChannel,
    RecordingAlertChannel,
)
from lake.operations import (
    main as report_main,
)
from lake.queue.dead_letter import InMemoryDeadLetterStore
from lake.storage.capacity import StorageCapGuard, estimate_current_storage

_DEFAULT_LOOKBACK_DAYS = 30
_ALERT_URL_ENV = "LAKE_OPERATIONS__ALERT_URL"


class _UrllibAlertTransport:
    """Minimal HTTP transport used only when an alert webhook is configured."""

    def post(self, url: str, *, body: bytes, headers: Mapping[str, str]) -> int:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return int(response.status)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lake operations",
        description="Emit the daily coverage and budget report.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and print the report without delivering it to the channel",
    )
    parser.add_argument(
        "--data-dir",
        help="local state root; defaults to LAKE_LOCAL_DATA_DIR or .local/market-lake",
    )
    parser.add_argument(
        "--session-date",
        type=date.fromisoformat,
        help="closed XNYS session to report; defaults to the latest closed session",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Compose and run one daily coverage and budget report."""
    arguments = _parser().parse_args(argv)
    environment = os.environ if environ is None else environ
    clock = now or (lambda: datetime.now(UTC))
    instant = _utc(clock())

    layout = LocalLayout.from_environment(arguments.data_dir, environ=environment)
    layout.initialize()
    catalog = LocalCatalogRepository(layout.catalog)
    listings = InMemoryListingRepository()
    settings = load_settings(environ=environment)
    universes = load_universes(environ=environment)
    soak = universes.equities.soak
    session = _select_session(arguments.session_date, instant)
    coverage = calculate_coverage(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols=soak.symbols,
        data_types=soak.data_types,
        start=session.session_date,
        end=session.session_date,
        as_of=instant,
        listings=listings,
    )
    channel = _channel(environment)
    report_argv = ["--dry-run"] if arguments.dry_run else []
    return report_main(
        report_argv,
        coverage=coverage,
        jobs=InMemoryJobRepository(),
        dead_letters=InMemoryDeadLetterStore(),
        cap_guard=StorageCapGuard(
            storage_usage=lambda: estimate_current_storage(catalog),
            settings=settings.storage,
        ),
        settings=settings.operations,
        channel=channel,
        as_of=instant,
    )


def _channel(environment: Mapping[str, str]) -> AlertChannel:
    url = (environment.get(_ALERT_URL_ENV) or "").strip()
    if not url:
        return RecordingAlertChannel()
    return HttpAlertChannel(url=url, transport=_UrllibAlertTransport())


def _select_session(session_date: date | None, now: datetime) -> CoverageSession:
    if session_date is None:
        sessions = equity_sessions(
            now.date() - timedelta(days=_DEFAULT_LOOKBACK_DAYS),
            now.date(),
            as_of=now,
        )
        closed = [session for session in sessions if session.close_time <= now]
        if not closed:
            raise ValueError("no closed XNYS session is available")
        return max(closed, key=lambda session: session.session_date)

    sessions = equity_sessions(session_date, session_date, as_of=now)
    if not sessions or sessions[0].close_time > now:
        raise ValueError("session-date must identify a closed XNYS session")
    return sessions[0]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["main"]
