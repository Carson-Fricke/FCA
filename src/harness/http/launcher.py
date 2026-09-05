"""Backtest job launching for the harness HTTP boundary.

The launch route validates one submitted backtest configuration, records a
queued job, and hands the work to an injected :data:`BacktestJobLauncher`.
The default :class:`InProcessBacktestLauncher` runs the deterministic
backtest runner inside the API process and records the completed run in the
:class:`~harness.results.store.BacktestResultsStore`, so a local launch is
immediately queryable. Hosted compositions replace the launcher — for
example with an Azure Container Apps Job client — without route changes.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from harness.configuration import BacktestConfig, HarnessConfig, configuration_hash
from harness.results.store import (
    BacktestResultsStore,
    RunCatalogRecord,
    RunStatus,
)
from harness.runtime.backtest import BacktestResult, run_backtest
from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import JobRepository

_ARTIFACT_NAME: Final = "backtest.json"
_HARNESS_CONFIG: Final = TypeAdapter(HarnessConfig)


@dataclass(frozen=True, slots=True)
class BacktestLaunchJob:
    """Work description passed to an injected backtest launcher."""

    job_id: str
    configuration: BacktestConfig


BacktestJobLauncher = Callable[[BacktestLaunchJob], None | Awaitable[None]]


def parse_backtest_configuration(payload: Mapping[str, object]) -> BacktestConfig:
    """Validate one decoded configuration document mapping.

    Validation follows :func:`harness.configuration.loader.load_configuration`:
    the same discriminated strict adapter accepts the API, backtest, and
    trader shells, and only a backtest document is accepted here.
    """
    configuration = _HARNESS_CONFIG.validate_python(payload)
    if not isinstance(configuration, BacktestConfig):
        raise ValueError(
            f"configuration shell must be 'backtest', not {configuration.shell!r}"
        )
    return configuration


def parse_backtest_configuration_json(body: bytes | str) -> BacktestConfig:
    """Decode and validate one submitted JSON configuration document.

    Decoding uses ``parse_float=Decimal`` so JSON numbers become exact
    decimals from their source text, mirroring the YAML loader's
    unquoted-decimal convention while quoted strings stay string-exact in
    parameter trees. Malformed JSON, unknown fields, wrong shells, and
    invalid values all raise for the route to translate into one
    ``validation_error`` response.
    """
    return parse_backtest_configuration(json.loads(body, parse_float=Decimal))


def launch_record(
    configuration: BacktestConfig,
    result: BacktestResult,
) -> RunCatalogRecord:
    """Map one completed runner result onto a results-store catalog row.

    ``strategy_id`` is the single configured instance ID, or the sorted
    comma-joined instance IDs for multi-strategy configurations.
    """
    instance_ids = sorted(
        instance.instance_id for instance in configuration.strategy_instances
    )
    return RunCatalogRecord(
        run_id=configuration.run_id,
        status=RunStatus.COMPLETE if result.completed else RunStatus.FAILED,
        strategy_id=",".join(instance_ids),
        data_start=configuration.start.date(),
        data_end=configuration.end.date(),
        configuration_digest=configuration_hash(configuration),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class InProcessBacktestLauncher:
    """Launcher that runs one deterministic backtest inside this process."""

    store: BacktestResultsStore
    jobs: JobRepository
    data_source: Path | None = None
    clock: Callable[[], datetime] = field(default=_utc_now)

    def __call__(self, job: BacktestLaunchJob) -> None:
        """Run the job's backtest and record its run row and job state."""
        configuration = job.configuration
        self._update_job(job.job_id, JobState.RUNNING)
        result = run_backtest(configuration, data_source=self.data_source)
        self.store.put(
            launch_record(configuration, result),
            {_ARTIFACT_NAME: result.artifact_json().encode("utf-8")},
        )
        self._update_job(job.job_id, JobState.SUCCEEDED)

    def _update_job(
        self,
        job_id: str,
        state: JobState,
    ) -> None:
        existing = self.jobs.get(job_id)
        self.jobs.upsert(
            JobRecord(
                job_id=job_id,
                state=state,
                created_at=(
                    existing.created_at if existing is not None else self.clock()
                ),
                updated_at=self.clock(),
            )
        )


__all__ = [
    "BacktestJobLauncher",
    "BacktestLaunchJob",
    "InProcessBacktestLauncher",
    "launch_record",
    "parse_backtest_configuration",
    "parse_backtest_configuration_json",
]
