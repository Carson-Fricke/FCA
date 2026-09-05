"""Focused tests for sweep specs: expansion, dedup, and canonical identity."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from harness.configuration.loader import load_yaml_mapping
from harness.configuration.models import BacktestConfig, ConfigurationValue
from harness.http.launcher import parse_backtest_configuration
from harness.sweeps.spec import SweepMode, SweepPoint, SweepSpec

CONFIG_ROOT: Final = Path(__file__).parents[3] / "config" / "harness"
INSTANCE_ID: Final = "spy-momentum"
IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def base_configuration() -> BacktestConfig:
    """Return the benchmark backtest configuration as the sweep base."""
    return parse_backtest_configuration(
        load_yaml_mapping(CONFIG_ROOT / "backtest.yaml")
    )


def test_grid_expands_cartesian_product_in_sorted_parameter_order() -> None:
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"threshold": ["0.05", "0.10"], "lookback": [2, 3]},
    )

    points = spec.expand()

    assert spec.count == 4
    assert [point.index for point in points] == [0, 1, 2, 3]
    assert [dict(point.parameters) for point in points] == [
        {"lookback": 2, "threshold": "0.05"},
        {"lookback": 2, "threshold": "0.10"},
        {"lookback": 3, "threshold": "0.05"},
        {"lookback": 3, "threshold": "0.10"},
    ]


def test_expanded_points_carry_derived_configs_and_identities() -> None:
    from harness.configuration.hashing import configuration_hash

    base = base_configuration()
    spec = SweepSpec.grid(base, INSTANCE_ID, {"lookback": [2]})

    (point,) = spec.expand()

    assert isinstance(point, SweepPoint)
    experiment = point.configuration.model_copy(update={"run_id": base.run_id})
    assert point.configuration_digest == configuration_hash(experiment)
    assert point.configuration.run_id == point.run_id
    instance = next(
        candidate
        for candidate in point.configuration.strategy_instances
        if candidate.instance_id == INSTANCE_ID
    )
    assert instance.parameters["lookback"] == 2
    assert instance.parameters["quantity"] == 20
    base_instance = next(
        candidate
        for candidate in base.strategy_instances
        if candidate.instance_id == INSTANCE_ID
    )
    assert base_instance.parameters["lookback"] == 4
    assert IDENTIFIER_PATTERN.fullmatch(point.run_id) is not None
    assert len(point.run_id) <= 128
    assert point.run_id == f"{base.run_id}-{point.configuration_digest[:12]}"


def test_duplicate_effective_configs_collapse_to_first_occurrence() -> None:
    spec = SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": [2, 2, 3]})

    points = spec.expand()

    assert spec.count == 3
    assert [dict(point.parameters) for point in points] == [
        {"lookback": 2},
        {"lookback": 3},
    ]
    assert [point.index for point in points] == [0, 1]


def test_sweep_identity_is_canonical_and_content_addressed() -> None:
    base = base_configuration()

    first = SweepSpec.grid(base, INSTANCE_ID, {"lookback": [2, 3]})
    second = SweepSpec.grid(base, INSTANCE_ID, {"lookback": [2, 3]})

    assert first.sweep_id == second.sweep_id
    assert len(first.sweep_id) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", first.sweep_id) is not None
    assert (
        SweepSpec.grid(base, INSTANCE_ID, {"lookback": [2, 4]}).sweep_id
        != first.sweep_id
    )
    assert (
        SweepSpec.random(
            base,
            INSTANCE_ID,
            {"lookback": [2, 3]},
            count=2,
            seed=7,
        ).sweep_id
        != first.sweep_id
    )
    assert (
        SweepSpec.grid(
            base.model_copy(update={"run_id": "other-run"}),
            INSTANCE_ID,
            {"lookback": [2, 3]},
        ).sweep_id
        != first.sweep_id
    )


def test_random_mode_is_seeded_and_reproducible() -> None:
    base = base_configuration()
    space = {
        "lookback": [2, 3, 4],
        "quantity": [10, 20, 30],
        "threshold": ["0.03", "0.05", "0.07"],
    }

    first = SweepSpec.random(base, INSTANCE_ID, space, count=8, seed=1729)
    second = SweepSpec.random(base, INSTANCE_ID, space, count=8, seed=1729)
    other = SweepSpec.random(base, INSTANCE_ID, space, count=8, seed=43)

    assert first.expand() == second.expand()
    assert [dict(point.parameters) for point in first.expand()] != [
        dict(point.parameters) for point in other.expand()
    ]
    assert len(first.expand()) == 8
    assert first.seed == 1729


def test_random_mode_deduplicates_repeated_draws() -> None:
    spec = SweepSpec.random(
        base_configuration(),
        INSTANCE_ID,
        {"lookback": [2, 3]},
        count=16,
        seed=5,
    )

    points = spec.expand()

    assert len(points) == 2
    assert [point.index for point in points] == [0, 1]
    assert len({point.configuration_digest for point in points}) == 2


def test_long_base_run_ids_stay_within_the_identifier_limit() -> None:
    base = base_configuration().model_copy(update={"run_id": "a" * 120})
    spec = SweepSpec.grid(base, INSTANCE_ID, {"lookback": [2, 3]})

    points = spec.expand()

    assert all(len(point.run_id) <= 128 for point in points)
    assert all(
        IDENTIFIER_PATTERN.fullmatch(point.run_id) is not None for point in points
    )
    assert len({point.run_id for point in points}) == 2


def test_unknown_instance_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a configured strategy instance"):
        SweepSpec.grid(base_configuration(), "missing", {"lookback": [2]})


def test_empty_space_and_empty_levels_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least one parameter"):
        SweepSpec.grid(base_configuration(), INSTANCE_ID, {})
    with pytest.raises(ValueError, match="at least one value"):
        SweepSpec.grid(base_configuration(), INSTANCE_ID, {"lookback": []})


def test_float_space_values_are_rejected() -> None:
    float_levels: list[ConfigurationValue] = [0.05]  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        SweepSpec.grid(base_configuration(), INSTANCE_ID, {"threshold": float_levels})


def test_mode_and_seed_combinations_are_validated() -> None:
    base = base_configuration()

    with pytest.raises(ValueError, match="must not carry a seed"):
        SweepSpec(
            base=base,
            instance_id=INSTANCE_ID,
            space={"lookback": (2, 3)},
            mode=SweepMode.GRID,
            count=2,
            seed=7,
        )
    with pytest.raises(ValueError, match="require a recorded seed"):
        SweepSpec(
            base=base,
            instance_id=INSTANCE_ID,
            space={"lookback": (2, 3)},
            mode=SweepMode.RANDOM,
            count=2,
            seed=None,
        )
    with pytest.raises(ValueError, match="at least one draw"):
        SweepSpec.random(base, INSTANCE_ID, {"lookback": [2]}, count=0, seed=1)
    with pytest.raises(ValueError, match="non-negative integer seed"):
        SweepSpec.random(base, INSTANCE_ID, {"lookback": [2]}, count=1, seed=-1)


def test_grid_count_is_derived_from_the_level_product() -> None:
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"lookback": [2, 3], "quantity": [10, 20]},
    )

    assert spec.count == 4
    assert spec.mode is SweepMode.GRID
    assert spec.seed is None


def test_decimal_space_values_round_trip_exactly() -> None:
    spec = SweepSpec.grid(
        base_configuration(),
        INSTANCE_ID,
        {"threshold": [Decimal("0.05")]},
    )

    (point,) = spec.expand()

    assert point.parameters["threshold"] == Decimal("0.05")
