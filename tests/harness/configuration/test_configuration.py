from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from harness.configuration import (
    ApiConfig,
    BacktestConfig,
    ConfigurationSecretError,
    TraderConfig,
    canonical_configuration_json,
    configuration_hash,
    harness_config_dir,
    load_configuration,
    load_example,
    load_yaml_mapping,
    resolve_environment_reference,
)

ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parent / "fixtures"


def test_every_execution_shell_example_validates() -> None:
    api = load_example("api", ROOT)
    backtest = load_example("backtest", ROOT)
    trader = load_example("trader", ROOT)

    assert isinstance(api, ApiConfig)
    assert isinstance(backtest, BacktestConfig)
    assert isinstance(trader, TraderConfig)
    assert backtest.start == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert backtest.data.symbols == ("SPY",)
    assert backtest.strategy_instances[0].implementation == (
        "strategies.harness_examples.intraday_momentum:IntradayMomentumStrategy"
    )
    assert backtest.strategy_instances[0].parameters["threshold"] == "0.05"
    assert trader.market_data.credentials["api_key"] == "${HARNESS_ALPACA_API_KEY}"


@pytest.mark.parametrize(
    ("filename", "field"),
    [("invalid-api.yaml", "port"), ("invalid-backtest.yaml", "initial_cash")],
)
def test_invalid_fixtures_report_precise_fields(filename: str, field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        load_configuration(FIXTURES / filename)

    locations = [error["loc"] for error in caught.value.errors()]
    assert any(location[-1] == field for location in locations)


def test_committed_secret_value_reports_its_precise_path() -> None:
    with pytest.raises(
        ConfigurationSecretError,
        match=r"market_data\.credentials\.api_key must contain only \$\{ENV_VAR\}",
    ):
        load_configuration(FIXTURES / "invalid-secret.yaml")


def test_secret_references_resolve_only_from_explicit_runtime_environment() -> None:
    secret = resolve_environment_reference(
        "${HARNESS_ALPACA_API_KEY}",
        {"HARNESS_ALPACA_API_KEY": "runtime-secret"},
    )

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "runtime-secret"
    assert "runtime-secret" not in repr(secret)

    with pytest.raises(ValueError, match="HARNESS_ALPACA_API_KEY is not set"):
        resolve_environment_reference("${HARNESS_ALPACA_API_KEY}", {})
    with pytest.raises(ValueError, match=r"\$\{ENV_VAR\}"):
        resolve_environment_reference("runtime-secret", {})


def test_hash_is_independent_of_mapping_order_and_decimal_representation() -> None:
    first = {
        "strategy": {
            "parameters": {"slow": 20, "threshold": Decimal("0.0100")},
            "name": "momentum",
        },
        "seed": 7,
    }
    second = {
        "seed": 7,
        "strategy": {
            "name": "momentum",
            "parameters": {"threshold": Decimal("0.01"), "slow": 20},
        },
    }

    assert canonical_configuration_json(first) == canonical_configuration_json(second)
    assert configuration_hash(first) == configuration_hash(second)


def test_hash_normalizes_equivalent_datetimes_to_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    first = {"start": datetime(2024, 1, 2, 9, 30, tzinfo=eastern)}
    second = {"start": datetime(2024, 1, 2, 14, 30, tzinfo=UTC)}

    assert configuration_hash(first) == configuration_hash(second)
    assert "2024-01-02T14:30:00.000000Z" in canonical_configuration_json(first)


def test_hash_does_not_depend_on_configuration_file_location(tmp_path: Path) -> None:
    source = ROOT / "config" / "harness" / "backtest.yaml"
    first_path = tmp_path / "host-a.yaml"
    second_dir = tmp_path / "different-host"
    second_dir.mkdir()
    second_path = second_dir / "host-b.yaml"
    contents = source.read_text(encoding="utf-8")
    first_path.write_text(contents, encoding="utf-8")
    second_path.write_text(contents, encoding="utf-8")

    assert configuration_hash(load_configuration(first_path)) == configuration_hash(
        load_configuration(second_path)
    )


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("api", "3b1a01c125bb246c965d877fdac68fb0d12a78941a89c20a4c62df508b51b676"),
        (
            "backtest",
            "9076d70fa89c3624ce292824665f45f5c6e5afa74837f92084ee7d1bd629adf5",
        ),
        (
            "trader",
            "7774d9a90413bb24a2ce1c599443811d483d986e3847637e3975c1e71a9d466b",
        ),
    ],
)
def test_example_hashes_are_stable(shell: str, expected: str) -> None:
    assert (
        configuration_hash(
            load_configuration(harness_config_dir(ROOT) / f"{shell}.yaml")
        )
        == expected
    )


def test_hash_rejects_resolved_secrets_and_binary_floats() -> None:
    with pytest.raises(TypeError, match="resolved secrets"):
        configuration_hash({"credential": SecretStr("do-not-hash")})
    with pytest.raises(TypeError, match="binary floating-point"):
        configuration_hash({"threshold": 0.1})


def test_ordered_pipeline_steps_remain_hash_significant() -> None:
    first = {"rules": [{"rule_id": "position"}, {"rule_id": "loss"}]}
    second = {"rules": [{"rule_id": "loss"}, {"rule_id": "position"}]}

    assert configuration_hash(first) != configuration_hash(second)


def test_backtest_requires_deterministic_network_and_time_constraints() -> None:
    values = load_yaml_mapping(ROOT / "config" / "harness" / "backtest.yaml")
    values["clock"] = "wall_clock"

    with pytest.raises(ValidationError) as caught:
        BacktestConfig.model_validate(values)

    assert any(error["loc"] == ("clock",) for error in caught.value.errors())

    values = load_yaml_mapping(ROOT / "config" / "harness" / "backtest.yaml")
    deployment = values["deployment"]
    assert isinstance(deployment, dict)
    deployment["network_access"] = "runtime_only"
    with pytest.raises(ValidationError, match="disable network access"):
        BacktestConfig.model_validate(values)


def test_models_forbid_extra_fields_and_mutation() -> None:
    config = load_example("api", ROOT)
    assert isinstance(config, ApiConfig)
    values = load_yaml_mapping(ROOT / "config" / "harness" / "api.yaml")
    values["unknown"] = True

    with pytest.raises(ValidationError) as caught:
        ApiConfig.model_validate(values)
    assert any(error["loc"] == ("unknown",) for error in caught.value.errors())

    field = "shell"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(config, field, "api")


def test_yaml_subset_rejects_executable_tags(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.yaml"
    path.write_text("schema_version: !!python/object:example\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported YAML value"):
        load_yaml_mapping(path)
