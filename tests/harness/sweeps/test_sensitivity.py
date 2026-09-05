"""Focused tests for sweep ranking and parameter sensitivity statistics."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from harness.configuration.hashing import configuration_hash
from harness.configuration.loader import load_yaml_mapping
from harness.configuration.models import BacktestConfig
from harness.http.launcher import BacktestLaunchJob, parse_backtest_configuration
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
    RunCatalogRecord,
    RunStatus,
)
from harness.sweeps.runner import SweepRunner
from harness.sweeps.sensitivity import parameter_sensitivity, rank_runs
from harness.sweeps.spec import SweepSpec

CONFIG_ROOT: Final = Path(__file__).parents[3] / "config" / "harness"
INSTANCE_ID: Final = "spy-momentum"


def base_configuration() -> BacktestConfig:
    """Return the benchmark backtest configuration as the sweep base."""
    return parse_backtest_configuration(
        load_yaml_mapping(CONFIG_ROOT / "backtest.yaml")
    )


def make_record(run_id: str, sharpe: str | None) -> RunCatalogRecord:
    """Return one catalog row carrying the given sharpe metric."""
    metrics = {} if sharpe is None else {"sharpe": sharpe}
    return RunCatalogRecord(
        run_id=run_id,
        status=RunStatus.COMPLETE,
        strategy_id=INSTANCE_ID,
        metrics=metrics,
    )


class CompletingLauncher:
    """Launch port recording completed runs with deterministic metrics."""

    def __init__(self, store: BacktestResultsStore) -> None:
        self.store = store
        self.jobs: list[BacktestLaunchJob] = []

    def __call__(self, job: BacktestLaunchJob) -> None:
        self.jobs.append(job)
        configuration = job.configuration
        parameters = configuration.strategy_instances[0].parameters
        lookback = Decimal(str(parameters["lookback"]))
        quantity = Decimal(str(parameters["quantity"]))
        sharpe = lookback + quantity * Decimal("0.01")
        self.store.put(
            RunCatalogRecord(
                run_id=job.job_id,
                status=RunStatus.COMPLETE,
                strategy_id=INSTANCE_ID,
                configuration_digest=configuration_hash(configuration),
                metrics={"sharpe": str(sharpe)},
            ),
            {"backtest.json": b"{}"},
        )


def test_rank_runs_orders_by_metric_with_run_id_tie_breaks() -> None:
    records = [
        make_record("run-b", "1.5000"),
        make_record("run-c", "2.0000"),
        make_record("run-a", "2.0000"),
    ]

    ranked = rank_runs(records, "sharpe")

    assert [(row.rank, row.run_id) for row in ranked] == [
        (1, "run-a"),
        (2, "run-c"),
        (3, "run-b"),
    ]
    assert all(row.value == Decimal("2.0") for row in ranked[:2])
    assert ranked[2].value == Decimal("1.5")


def test_rank_runs_supports_ascending_order() -> None:
    records = [make_record("run-b", "1.5"), make_record("run-a", "2.0")]

    ranked = rank_runs(records, "sharpe", descending=False)

    assert [(row.rank, row.run_id) for row in ranked] == [(1, "run-b"), (2, "run-a")]


def test_rank_runs_excludes_missing_and_unparseable_metrics() -> None:
    records = [
        make_record("run-missing", None),
        make_record("run-text", "n/a"),
        make_record("run-nan", "NaN"),
        make_record("run-keep", "1.0"),
    ]

    ranked = rank_runs(records, "sharpe")

    assert [row.run_id for row in ranked] == ["run-keep"]
    assert ranked[0].rank == 1


def test_rank_runs_rejects_an_empty_metric_name() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        rank_runs([], "  ")


def test_parameter_sensitivity_computes_exact_level_effects() -> None:
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"lookback": [2, 4], "quantity": [10, 30]},
    )
    points = spec.expand()
    values = {
        points[0].run_id: "1.0",
        points[1].run_id: "3.0",
        points[2].run_id: "5.0",
        points[3].run_id: "7.0",
    }
    records = [make_record(point.run_id, values[point.run_id]) for point in points]

    summary = parameter_sensitivity(points, records, "sharpe")

    assert summary.metric == "sharpe"
    assert summary.runs_used == 4
    assert summary.grand_mean == Decimal("4")
    lookback, quantity = summary.parameters
    assert lookback.name == "lookback"
    assert lookback.effect_range == Decimal("4")
    assert [(level.value, level.mean, level.count) for level in lookback.levels] == [
        ("2", Decimal("2"), 2),
        ("4", Decimal("6"), 2),
    ]
    assert lookback.best_value == "4"
    assert quantity.name == "quantity"
    assert quantity.effect_range == Decimal("2")
    assert [(level.value, level.mean) for level in quantity.levels] == [
        ("10", Decimal("3")),
        ("30", Decimal("5")),
    ]
    assert quantity.best_value == "30"


def test_level_labels_reuse_canonical_scalar_encodings() -> None:
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"threshold": [Decimal("0.05"), "0.05"]},
    )
    points = spec.expand()
    records = [make_record(point.run_id, "1.0") for point in points]

    summary = parameter_sensitivity(points, records, "sharpe")

    (threshold,) = summary.parameters
    assert [level.value for level in threshold.levels] == [
        '"0.05"',
        '{"$decimal":"0.05"}',
    ]
    assert threshold.effect_range == Decimal("0")
    assert threshold.best_value == '"0.05"'


def test_sensitivity_ignores_runs_outside_the_sweep_or_metric() -> None:
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})
    points = spec.expand()
    records = [
        make_record(points[0].run_id, "1.0"),
        make_record(points[1].run_id, None),
        make_record("unrelated-run", "9.0"),
    ]

    summary = parameter_sensitivity(points, records, "sharpe")

    assert summary.runs_used == 1
    (lookback,) = summary.parameters
    assert lookback.levels[0].count == 1
    assert lookback.effect_range == Decimal("0")


def test_sensitivity_without_usable_runs_is_rejected() -> None:
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 3]})

    with pytest.raises(ValueError, match="no cataloged runs carry the metric"):
        parameter_sensitivity(spec.expand(), [], "sharpe")


def test_sensitivity_through_the_runner_catalog_is_deterministic() -> None:
    blobs = InMemoryRunBlobStore()
    store = BacktestResultsStore(blobs, InMemoryRunCatalogRepository())
    runner = SweepRunner(CompletingLauncher(store), store, blobs)
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"lookback": [2, 3], "quantity": [10, 20]},
    )

    runner.submit(spec)
    records = store.list()
    first = parameter_sensitivity(spec.expand(), records, "sharpe")
    second = parameter_sensitivity(spec.expand(), store.list(), "sharpe")

    assert first == second
    lookback, quantity = first.parameters
    assert lookback.effect_range == Decimal("1.00")
    assert lookback.best_value == "3"
    assert quantity.effect_range == Decimal("0.10")
    assert quantity.best_value == "20"
    ranked = rank_runs(records, "sharpe")
    best = max(
        spec.expand(),
        key=lambda point: (
            dict(point.parameters)["lookback"],
            dict(point.parameters)["quantity"],
        ),
    )
    assert ranked[0].run_id == best.run_id
