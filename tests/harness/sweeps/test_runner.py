"""Focused tests for sweep orchestration over the injected launcher port."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from harness.configuration.loader import load_yaml_mapping
from harness.configuration.models import BacktestConfig
from harness.http.launcher import (
    BacktestLaunchJob,
    InProcessBacktestLauncher,
    parse_backtest_configuration,
)
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
    RunCatalogRecord,
    RunStatus,
)
from harness.sweeps.runner import SweepRunner, sweep_prefix
from harness.sweeps.spec import SweepSpec
from lake.catalog.memory import InMemoryJobRepository

CONFIG_ROOT: Final = Path(__file__).parents[3] / "config" / "harness"
INSTANCE_ID: Final = "spy-momentum"


def base_configuration() -> BacktestConfig:
    """Return the benchmark backtest configuration as the sweep base."""
    return parse_backtest_configuration(
        load_yaml_mapping(CONFIG_ROOT / "backtest.yaml")
    )


def make_parts() -> tuple[BacktestResultsStore, InMemoryRunBlobStore]:
    """Return one results store and its blob adapter."""
    blobs = InMemoryRunBlobStore()
    return BacktestResultsStore(blobs, InMemoryRunCatalogRepository()), blobs


def catalog(
    run_id: str,
    status: RunStatus,
    store: BacktestResultsStore,
) -> RunCatalogRecord:
    """Record one completed or failed run row in the catalog."""
    return store.put(
        RunCatalogRecord(
            run_id=run_id,
            status=status,
            strategy_id=INSTANCE_ID,
            metrics={"sharpe": "1.2345"},
        ),
        {"backtest.json": b"{}"},
    )


class RecordingLauncher:
    """Sweep job-launcher port that only records the submitted jobs."""

    def __init__(self) -> None:
        self.jobs: list[BacktestLaunchJob] = []

    def __call__(self, job: BacktestLaunchJob) -> None:
        self.jobs.append(job)


def test_submit_launches_one_job_per_deduplicated_point() -> None:
    store, blobs = make_parts()
    launcher = RecordingLauncher()
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})

    submission = runner.submit(spec)

    assert submission.sweep_id == spec.sweep_id
    assert submission.requested == 2
    assert submission.expanded == 2
    assert submission.duplicates_skipped == 0
    assert submission.run_ids == tuple(point.run_id for point in submission.points)
    assert len(launcher.jobs) == 2
    assert [job.job_id for job in launcher.jobs] == list(submission.run_ids)


def test_submit_jobs_carry_derived_run_ids_and_configurations() -> None:
    store, blobs = make_parts()
    launcher = RecordingLauncher()
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})

    submission = runner.submit(spec)

    assert [job.job_id for job in launcher.jobs] == list(submission.run_ids)
    assert [job.configuration for job in launcher.jobs] == [
        point.configuration for point in submission.points
    ]
    assert all(job.configuration.run_id == job.job_id for job in launcher.jobs)
    assert all(
        job.configuration.strategy_instances[0].parameters["lookback"] == 2 + index
        for index, job in enumerate(launcher.jobs)
    )


def test_submit_persists_the_sweep_record_before_launching() -> None:
    store, blobs = make_parts()
    launcher = RecordingLauncher()
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})

    submission = runner.submit(spec)

    document = json.loads(blobs.read(f"{sweep_prefix(spec.sweep_id)}/sweep.json"))
    assert document["sweep_id"] == spec.sweep_id
    assert document["requested"] == 2
    assert [entry["run_id"] for entry in document["runs"]] == list(submission.run_ids)
    assert [entry["configuration_digest"] for entry in document["runs"]] == [
        point.configuration_digest for point in submission.points
    ]


def test_duplicate_effective_configs_are_reported_not_launched() -> None:
    store, blobs = make_parts()
    launcher = RecordingLauncher()
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 2, 3]})

    submission = runner.submit(spec)

    assert submission.requested == 3
    assert submission.expanded == 2
    assert submission.duplicates_skipped == 1
    assert len(launcher.jobs) == 2


def test_status_joins_the_run_catalog_by_run_id() -> None:
    store, blobs = make_parts()
    runner = SweepRunner(RecordingLauncher(), store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})
    submission = runner.submit(spec)
    first, second = submission.run_ids

    pending = runner.status(submission.sweep_id)

    assert pending.sweep_id == submission.sweep_id
    assert pending.requested == 2
    assert pending.expanded == 2
    assert [view.run_id for view in pending.runs] == [first, second]
    assert all(view.status is None for view in pending.runs)
    assert pending.complete == 0
    assert pending.failed == 0
    assert pending.pending == 2
    assert pending.is_complete is False

    catalog(first, RunStatus.COMPLETE, store)
    catalog(second, RunStatus.FAILED, store)

    settled = runner.status(submission.sweep_id)
    assert settled.complete == 1
    assert settled.failed == 1
    assert settled.pending == 0
    assert settled.is_complete is False
    assert settled.runs[0].metrics["sharpe"] == "1.2345"


def test_status_reports_completion_when_every_run_completes() -> None:
    store, blobs = make_parts()
    runner = SweepRunner(RecordingLauncher(), store, blobs)
    submission = runner.submit(
        SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})
    )

    for run_id in submission.run_ids:
        catalog(run_id, RunStatus.COMPLETE, store)

    status = runner.status(submission.sweep_id)

    assert status.is_complete is True
    assert status.complete == 2


def test_status_of_an_unknown_sweep_raises_key_error() -> None:
    store, blobs = make_parts()
    runner = SweepRunner(RecordingLauncher(), store, blobs)

    with pytest.raises(KeyError, match="sweep not found"):
        runner.status("f" * 64)


def test_malformed_sweep_ids_are_rejected() -> None:
    store, blobs = make_parts()
    runner = SweepRunner(RecordingLauncher(), store, blobs)

    with pytest.raises(ValueError, match="sha-256 hex digest"):
        sweep_prefix("SWEEP")
    with pytest.raises(ValueError, match="sha-256 hex digest"):
        runner.status("short")


def test_sweep_ids_listing_is_sorted() -> None:
    store, blobs = make_parts()
    runner = SweepRunner(RecordingLauncher(), store, blobs)
    second = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})
    first = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 4]})

    first_submission = runner.submit(first)
    second_submission = runner.submit(second)

    assert runner.sweep_ids() == tuple(
        sorted([first_submission.sweep_id, second_submission.sweep_id])
    )


def test_resubmission_is_idempotent_over_record_bytes() -> None:
    store, blobs = make_parts()
    launcher = RecordingLauncher()
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})

    first = runner.submit(spec)
    record_path = f"{sweep_prefix(spec.sweep_id)}/sweep.json"
    before = blobs.read(record_path)
    second = runner.submit(spec)
    after = blobs.read(record_path)

    assert first == second
    assert before == after
    assert len(launcher.jobs) == 4


def test_fifty_configuration_sweep_completes_with_all_runs_queryable() -> None:
    store, blobs = make_parts()
    launcher = InProcessBacktestLauncher(store, InMemoryJobRepository())
    runner = SweepRunner(launcher, store, blobs)
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {
            "lookback": [2, 3],
            "quantity": [10, 20, 30, 40, 50],
            "threshold": ["0.03", "0.05", "0.07", "0.09", "0.11"],
        },
    )

    submission = runner.submit(spec)

    assert submission.requested == 50
    assert submission.expanded == 50
    assert len(set(submission.run_ids)) == 50
    status = runner.status(submission.sweep_id)
    assert status.is_complete is True
    assert status.complete == 50
    assert status.pending == 0
    assert status.failed == 0
    for view in status.runs:
        row = store.get(view.run_id)
        assert row is not None
        assert row.status is RunStatus.COMPLETE
        assert row.configuration_digest is not None
