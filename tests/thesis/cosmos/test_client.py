"""Cosmos connection settings: emulator key fallback and managed-identity auth."""

from __future__ import annotations

from typing import cast

import pytest

from thesis.cosmos.client import EMULATOR_KEY, CosmosSettings, open_cosmos_client

_SETTING_NAMES = (
    "THESIS_COSMOS_ENDPOINT",
    "THESIS_COSMOS_KEY",
    "THESIS_COSMOS_DATABASE",
)


def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_absent_settings_default_to_the_local_emulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings(monkeypatch)

    settings = CosmosSettings.from_environment()

    assert settings.endpoint == "https://localhost:8081"
    assert settings.key == EMULATOR_KEY
    assert settings.database == "thesis"
    assert settings.connection_verify is False


def test_loopback_endpoint_without_a_key_uses_the_emulator_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings(monkeypatch)
    monkeypatch.setenv("THESIS_COSMOS_ENDPOINT", "https://127.0.0.1:8081")

    assert CosmosSettings.from_environment().key == EMULATOR_KEY


def test_deployed_endpoint_without_a_key_selects_the_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings(monkeypatch)
    monkeypatch.setenv(
        "THESIS_COSMOS_ENDPOINT", "https://thesis.documents.azure.com:443/"
    )
    monkeypatch.setenv("THESIS_COSMOS_DATABASE", "thesis-live")

    settings = CosmosSettings.from_environment()

    assert settings.key is None
    assert settings.database == "thesis-live"
    assert settings.connection_verify is True


def test_an_explicit_key_wins_on_a_deployed_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_settings(monkeypatch)
    monkeypatch.setenv(
        "THESIS_COSMOS_ENDPOINT", "https://thesis.documents.azure.com:443/"
    )
    monkeypatch.setenv("THESIS_COSMOS_KEY", "account-key")

    assert CosmosSettings.from_environment().key == "account-key"


class _RecordingClient:
    """Captures the CosmosClient constructor arguments instead of connecting."""

    def __init__(self, endpoint: str, *, credential: object, **kwargs: object) -> None:
        self.endpoint = endpoint
        self.credential = credential
        self.connection_verify = kwargs.get("connection_verify")


def test_open_cosmos_client_uses_the_key_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis.cosmos.client.CosmosClient", _RecordingClient)

    client = cast(
        "_RecordingClient",
        open_cosmos_client(
            CosmosSettings(
                endpoint="https://thesis.documents.azure.com:443/", key="account-key"
            )
        ),
    )

    assert client.credential == "account-key"
    assert client.connection_verify is True


def test_open_cosmos_client_uses_default_azure_credential_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("thesis.cosmos.client.CosmosClient", _RecordingClient)
    sentinel = object()
    monkeypatch.setattr("thesis.cosmos.client.DefaultAzureCredential", lambda: sentinel)

    client = cast(
        "_RecordingClient",
        open_cosmos_client(
            CosmosSettings(endpoint="https://thesis.documents.azure.com:443/", key=None)
        ),
    )

    assert client.credential is sentinel
