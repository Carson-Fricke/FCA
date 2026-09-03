from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from harness.configuration import load_yaml_mapping
from thesis.auth import BrowserSecurity, RuntimeSecurity
from thesis.providers import (
    EntraCredential,
    FoundryProvider,
    FoundryProviderProfile,
    ProviderCapabilitySnapshot,
    runtime_capability_preflight,
)

ROOT = Path(__file__).parents[3]
SECRET = "secret-access-token-must-not-escape"


@dataclass(frozen=True)
class FakeAccessToken:
    token: str = SECRET
    expires_on: int = 2_000_000_000


class FakeCredential:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str) -> FakeAccessToken:
        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return FakeAccessToken()


class FakeClient:
    def __init__(self, available: bool) -> None:
        self.available = available
        self.probes: list[tuple[str, str]] = []

    def deployment_available(self, *, project_name: str, deployment_name: str) -> bool:
        self.probes.append((project_name, deployment_name))
        return self.available


class FakeClientFactory:
    def __init__(
        self, client: FakeClient | None = None, error: Exception | None = None
    ) -> None:
        self.client = FakeClient(True) if client is None else client
        self.error = error
        self.credentials: list[object] = []

    def __call__(
        self, *, profile: FoundryProviderProfile, credential: EntraCredential
    ) -> FakeClient:
        self.credentials.append(credential)
        if self.error is not None:
            raise self.error
        assert profile.endpoint.startswith("https://")
        return self.client


def _profile(*, enabled: bool = True) -> FoundryProviderProfile:
    return FoundryProviderProfile(
        profile_id="developer",
        endpoint="https://example.services.ai.azure.com",
        project_name="thesis-project",
        model_deployment="reasoning-model",
        credential_mode="default_azure_credential",
        enabled=enabled,
    )


def test_foundry_preflight_uses_injected_backend_credential_and_client() -> None:
    credential = FakeCredential()
    client = FakeClient(True)
    factory = FakeClientFactory(client)

    report = runtime_capability_preflight(
        FoundryProvider(_profile(), credential, factory)
    )

    assert credential.scopes == [("https://ai.azure.com/.default",)]
    assert factory.credentials == [credential]
    assert client.probes == [("thesis-project", "reasoning-model")]
    assert report.provider.available
    assert report.provider.authenticated
    assert report.models[0].available
    assert report.model_backed_capabilities_available
    assert report.offline_workspace_available
    assert SECRET not in repr(report)
    assert SECRET not in str(asdict(report))


def test_unavailable_model_is_reported_without_disabling_offline_workspace() -> None:
    report = runtime_capability_preflight(
        FoundryProvider(
            _profile(),
            FakeCredential(),
            FakeClientFactory(FakeClient(False)),
        )
    )

    assert report.provider.available
    assert report.models[0].reason == "model_unavailable"
    assert not report.models[0].available
    assert not report.model_backed_capabilities_available
    assert report.offline_workspace_available


def test_authentication_failure_is_redacted_and_skips_provider_client() -> None:
    credential = FakeCredential(RuntimeError(f"credential failed: {SECRET}"))
    factory = FakeClientFactory()
    provider = FoundryProvider(_profile(), credential, factory)

    report = runtime_capability_preflight(provider)

    assert report.provider.reason == "authentication_unavailable"
    assert not report.provider.authenticated
    assert report.models[0].reason == "provider_unavailable"
    assert report.offline_workspace_available
    assert factory.credentials == []
    assert SECRET not in repr(provider)
    assert SECRET not in repr(report)


def test_provider_failure_and_unconfigured_provider_fail_closed() -> None:
    unreachable = runtime_capability_preflight(
        FoundryProvider(
            _profile(),
            FakeCredential(),
            FakeClientFactory(error=RuntimeError(f"network failed: {SECRET}")),
        )
    )
    unconfigured = runtime_capability_preflight(None)
    disabled = runtime_capability_preflight(
        FoundryProvider(_profile(enabled=False), FakeCredential(), FakeClientFactory())
    )

    assert unreachable.provider.reason == "provider_unreachable"
    assert unreachable.provider.authenticated
    assert SECRET not in repr(unreachable)
    assert unconfigured.provider.reason == "not_configured"
    assert unconfigured.models == ()
    assert disabled.provider.reason == "not_configured"
    assert all(
        report.offline_workspace_available
        for report in (unreachable, unconfigured, disabled)
    )


class ExplodingProvider:
    provider_name = "injected"
    profile_id = "exploding"

    def preflight(self) -> ProviderCapabilitySnapshot:
        raise RuntimeError(f"unexpected provider error: {SECRET}")


def test_runtime_boundary_redacts_unexpected_injected_provider_failures() -> None:
    report = runtime_capability_preflight(ExplodingProvider())

    assert report.provider.reason == "provider_unreachable"
    assert not report.provider.available
    assert report.models[0].reason == "provider_unavailable"
    assert report.offline_workspace_available
    assert SECRET not in repr(report)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.services.ai.azure.com",
        "https://user:secret@example.services.ai.azure.com",
        "https://example.services.ai.azure.com?token=secret",
    ],
)
def test_foundry_profile_rejects_non_https_or_credential_bearing_urls(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="HTTPS service URL"):
        FoundryProviderProfile(
            profile_id="developer",
            endpoint=endpoint,
            project_name="thesis-project",
            model_deployment="reasoning-model",
            credential_mode="default_azure_credential",
        )


def test_runtime_profiles_validate_and_contain_no_committed_credentials() -> None:
    path = ROOT / "config" / "thesis" / "runtime.yaml"
    values = load_yaml_mapping(path)
    runtimes = values["runtime_profiles"]
    providers = values["provider_profiles"]
    assert isinstance(runtimes, dict)
    assert isinstance(providers, dict)
    assert values["default_runtime_profile"] == "local-loopback"

    for profile_id, runtime in runtimes.items():
        assert isinstance(runtime, dict)
        assert runtime["profile_id"] == profile_id
        assert runtime["version"] == "1.0.0"
        browser = runtime["browser"]
        assert isinstance(browser, dict)
        RuntimeSecurity(
            bind_host=runtime["bind_host"],
            tls_enabled=runtime["tls_enabled"],
            authentication=runtime["authentication"],
            browser=BrowserSecurity(
                allowed_origins=tuple(browser["allowed_origins"]),
                csrf_protection=browser["csrf_protection"],
                http_only_cookies=browser["http_only_cookies"],
                secure_cookies=browser["secure_cookies"],
                same_site=browser["same_site"],
            ),
        )
    for profile in providers.values():
        assert isinstance(profile, dict)
        FoundryProviderProfile(**profile)

    lowered = path.read_text(encoding="utf-8").lower()
    assert "access_token" not in lowered
    assert "api_key" not in lowered
    assert "client_secret" not in lowered
