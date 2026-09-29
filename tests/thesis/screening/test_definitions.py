"""Definition hashing, canonicalization, and validation tests."""

from __future__ import annotations

from typing import Any

import pytest

from thesis.screening import ScreenerDefinition, ScreenerDefinitionError
from thesis.screening.definitions import BAR_INTERVALS


def test_equivalent_inputs_produce_one_definition_id() -> None:
    left = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=252,
        bar_interval="1d",
        rank_metric="momentum_12_1",
        universe_revision="sp500-2026-09",
        source="fn screen(ctx) {\r\n  ctx.rows()\r\n}\r\n",
        parameters={"threshold": 0.05, "sectors": ["tech", "energy"]},
    )
    right = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=252,
        bar_interval="1d",
        rank_metric="momentum_12_1",
        universe_revision="sp500-2026-09",
        source="fn screen(ctx) {\n  ctx.rows()\n}\n",
        parameters={"sectors": ["tech", "energy"], "threshold": 0.05},
    )

    assert left.definition_id == right.definition_id
    assert left.definition_hash == right.definition_hash
    assert left.canonical_bytes == right.canonical_bytes
    assert left.source_sha256 == right.source_sha256
    assert left.source == "fn screen(ctx) {\n  ctx.rows()\n}\n"
    assert left.document()["definitionId"] == left.definition_id


def test_parameters_participate_in_the_content_address() -> None:
    base = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=252,
        bar_interval="1d",
        rank_metric="momentum_12_1",
        source="fn screen(ctx) { ctx.rows() }",
        parameters={"threshold": 0.05},
    )
    changed = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=252,
        bar_interval="1d",
        rank_metric="momentum_12_1",
        source="fn screen(ctx) { ctx.rows() }",
        parameters={"threshold": 0.06},
    )
    shifted = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=252,
        bar_interval="1d",
        rank_metric="momentum_12_1",
        source="fn screen(ctx) { ctx.rows() }",
        parameters={"threshold": "0.05"},
    )

    assert base.definition_id != changed.definition_id
    assert base.definition_id != shifted.definition_id


def test_source_hash_is_the_harness_bundle_hash() -> None:
    definition = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=63,
        bar_interval="1min",
        rank_metric="relative_volume",
        source="fn screen(ctx) { ctx.rows() }",
    )
    config = definition.materialize_config(as_of="2026-09-28")

    assert definition.bar_interval in BAR_INTERVALS
    assert config["schema_version"] == 1
    assert config["shell"] == "screen"
    assert config["bundle_hash"] == definition.source_sha256
    assert config["source_sha256"] == definition.source_sha256
    assert config["definition_id"] == definition.definition_id
    assert config["definition_hash"] == definition.definition_sha256
    assert config["as_of"] == "2026-09-28"
    assert config["lookback_days"] == 63
    assert config["bar_interval"] == "1min"
    assert config["rank_metric"] == "relative_volume"
    assert config["parameters"] == {}


def test_materialize_config_requires_an_as_of() -> None:
    definition = ScreenerDefinition(
        name="Momentum leaders",
        lookback_days=63,
        bar_interval="1d",
        rank_metric="momentum",
        source="fn screen(ctx) { ctx.rows() }",
    )
    with pytest.raises(ScreenerDefinitionError):
        definition.materialize_config(as_of="  ")


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"source": ""},
        {"source": "   \n "},
        {"lookback_days": 0},
        {"lookback_days": -5},
        {"lookback_days": True},
        {"lookback_days": "252"},
        {"bar_interval": "5min"},
        {"bar_interval": "1h"},
        {"rank_metric": ""},
        {"universe_revision": ""},
        {"parameters": {"threshold": {1: "not-a-string-key"}}},
        {"parameters": ["not", "a", "mapping"]},
    ],
)
def test_invalid_definitions_fail_closed(overrides: dict[str, object]) -> None:
    values: dict[str, Any] = {
        "name": "Momentum leaders",
        "lookback_days": 252,
        "bar_interval": "1d",
        "rank_metric": "momentum_12_1",
        "source": "fn screen(ctx) { ctx.rows() }",
    }
    values.update(overrides)

    with pytest.raises(ScreenerDefinitionError):
        ScreenerDefinition(**values)
