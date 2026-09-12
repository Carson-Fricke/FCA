"""Composition entrypoint for the daily coverage and budget report.

The command is a thin composition around :func:`build_daily_report`. Every
repository, the capacity guard, the alert channel, and the as-of instant are
injected so the command stays offline and deterministic in tests.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime

from lake.catalog.ports import JobRepository
from lake.configuration.settings import OperationsSettings
from lake.coverage.service import CoverageReport
from lake.operations.service import AlertChannel, build_daily_report
from lake.queue.dead_letter import DeadLetterStore
from lake.storage.capacity import StorageCapGuard


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lake report",
        description="Emit the daily coverage and budget report as JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and print the report without sending it to the channel",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    coverage: CoverageReport,
    jobs: JobRepository,
    dead_letters: DeadLetterStore,
    cap_guard: StorageCapGuard,
    settings: OperationsSettings,
    channel: AlertChannel,
    as_of: datetime,
) -> int:
    """Build, print, and optionally deliver one daily report.

    Returns ``0`` on success and ``1`` when the channel send fails.
    """
    arguments = _parser().parse_args(argv)
    report = build_daily_report(
        coverage=coverage,
        jobs=jobs,
        dead_letters=dead_letters,
        cap_guard=cap_guard,
        settings=settings,
        as_of=as_of,
    )
    print(json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True))
    if arguments.dry_run:
        return 0
    try:
        channel.send(report)
    except Exception as error:
        print(f"lake report: error: {error}", file=sys.stderr)
        return 1
    return 0


__all__ = ["main"]
