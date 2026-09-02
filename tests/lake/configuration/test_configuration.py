from pathlib import Path

import pytest
from pydantic import ValidationError

from lake.configuration import load_settings, load_universes
from lake.contracts import DataType

ROOT = Path(__file__).parents[3]


def test_committed_settings_are_typed_and_environment_overrides_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKE_PROVIDER__REQUEST__MAX_CONCURRENCY", "8")
    monkeypatch.setenv("LAKE_PROVIDER__API_KEY", "test-api-key")
    monkeypatch.setenv(
        "LAKE_API__BEARER_TOKEN",
        "@Microsoft.KeyVault(SecretUri=https://vault/secrets/api)",
    )

    settings = load_settings(ROOT)

    assert settings.schema_version == 1
    assert settings.provider.request.max_concurrency == 8
    assert settings.provider.api_key is not None
    assert settings.provider.api_key.get_secret_value() == "test-api-key"
    assert settings.api.bearer_token is not None
    assert settings.api.bearer_token.get_secret_value().startswith(
        "@Microsoft.KeyVault("
    )
    assert settings.storage.capacity_bytes == 1_099_511_627_776


def test_committed_universes_are_normalized() -> None:
    universes = load_universes(ROOT)

    assert universes.equities.soak.symbols == ("AAPL", "SPY")
    assert universes.equities.soak.data_types == (DataType.TRADE, DataType.QUOTE)
    assert set(universes.equities.soak.history) == {
        DataType.TRADE,
        DataType.QUOTE,
    }
    assert universes.options.soak.underlyings == ("SPY", "QQQ", "AAPL")
    assert universes.options.selection.expiration_count == 2
    assert universes.crypto.pairs == ("BTC/USD", "ETH/USD")
    assert universes.crypto.data_types == (DataType.TRADE, DataType.QUOTE)


def test_environment_can_override_universe_values_without_process_globals() -> None:
    universes = load_universes(
        ROOT,
        environ={
            "LAKE_OPTIONS__SOAK__UNDERLYINGS": '["qqq", "QQQ", "aapl"]',
            "LAKE_CRYPTO__ROLLOUT__TARGET_PAIR_COUNT": "25",
        },
    )

    assert universes.options.soak.underlyings == ("QQQ", "AAPL")
    assert universes.crypto.rollout.target_pair_count == 25


def test_invalid_environment_values_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE_STORAGE__CAPACITY_BYTES", "not-a-number")

    with pytest.raises(ValidationError, match="capacity_bytes"):
        load_settings(ROOT)


def test_secret_bearing_yaml_keys_are_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "market-lake"
    config_dir.mkdir(parents=True)
    (config_dir / "settings.yaml").write_text(
        "schema_version: 1\nprovider:\n  api_key: accidentally-committed\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="secret-bearing YAML key"):
        load_settings(tmp_path)


def test_direct_config_directory_is_a_supported_root() -> None:
    assert load_settings(ROOT / "config" / "market-lake").provider.name == "alpaca"


def test_explicit_runtime_config_root_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKE_CONFIG_ROOT", str(ROOT))

    assert load_settings().provider.name == "alpaca"


def test_invalid_universe_values_fail_fast() -> None:
    with pytest.raises(ValidationError, match="crypto pair"):
        load_universes(
            ROOT,
            environ={"LAKE_CRYPTO__PAIRS": '["BTC-USD"]'},
        )
