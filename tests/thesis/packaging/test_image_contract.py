"""Thesis backend image and process packaging contract.

The contract is pinned without building a container: entrypoint registration,
fail-clear process behavior, configuration-path resolution, and the
``Dockerfile.thesis`` runtime-image shape are checked directly. The first real
image build happens in the CI image-publication workflow, not in these tests.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from harness.configuration import load_yaml_mapping
from thesis import cli
from thesis.chat.service import ChatService

runner = CliRunner()

ROOT = Path(__file__).parents[3]
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE_THESIS = ROOT / "Dockerfile.thesis"
RUNTIME_CONFIG = ROOT / "config" / "thesis" / "runtime.yaml"

HARNESS_ENTRYPOINTS = {
    "lake": "lake.cli:main",
    "harness": "harness.cli:main",
    "api": "harness.cli.api:main",
    "backtest-job": "harness.cli.backtest_job:main",
    "trader": "harness.cli.trader:main",
}
BACKEND_CAPABILITY_PACKAGES = ("workspaces", "chat", "execution", "integrations")

_IMAGE_SECRET_LINE = re.compile(
    r"^\s*(?:ENV|ARG)\b.*\b(?:password|secret|token|api[_-]?key)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _project_scripts() -> dict[str, str]:
    scripts: dict[str, str] = {}
    in_scripts = False
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            in_scripts = line.strip() == "[project.scripts]"
            continue
        if in_scripts and "=" in line:
            name, _, target = line.partition("=")
            scripts[name.strip()] = target.strip().strip('"')
    return scripts


def test_thesis_entrypoint_is_registered_and_harness_ownership_is_unchanged() -> None:
    scripts = _project_scripts()

    assert scripts == {
        "lake": "lake.cli:main",
        "harness": "harness.cli:main",
        "api": "harness.cli.api:main",
        "backtest-job": "harness.cli.backtest_job:main",
        "trader": "harness.cli.trader:main",
        "thesis": "thesis.cli:main",
    }


def test_thesis_backend_package_is_built_into_the_wheel() -> None:
    pyproject = PYPROJECT.read_text(encoding="utf-8")

    assert '"thesis*"' in pyproject


def test_backend_capability_packages_cover_the_thesis_backend() -> None:
    assert set(BACKEND_CAPABILITY_PACKAGES) <= set(cli.BACKEND_CAPABILITIES)


def test_thesis_help_runs_the_process_boundary() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.stdout
    assert "compose" in result.stdout


@pytest.mark.parametrize(
    ("mode", "description"),
    [
        ("serve", "Serve the Thesis API process"),
        ("compose", "Report the composed Thesis backend capability surface"),
    ],
)
def test_process_mode_help(mode: str, description: str) -> None:
    result = runner.invoke(cli.app, [mode, "--help"])

    assert result.exit_code == 0
    assert description in result.stdout


def test_module_entrypoint_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "thesis", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "compose" in result.stdout
    assert result.stderr == ""


_AUTH_ENVIRONMENT = (
    "THESIS_API__BEARER_TOKEN",
    "THESIS_API__ENTRA_TENANT_ID",
    "THESIS_API__ENTRA_AUDIENCE",
    "THESIS_API__ENTRA_ROLE",
    "THESIS_SERVE_HOST",
    "THESIS_SERVE_PORT",
)


def _clear_serve_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AUTH_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


def _capture_uvicorn_run(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_run(application: object, **kwargs: object) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return captured


def _stub_cosmos_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose serve without the Cosmos emulator.

    ``serving_app_from_environment`` opens the Cosmos-backed chat stack at
    startup, which means a live emulator at localhost:8081.  These packaging
    tests pin the process and route contract, not the durable store, so the
    stack is replaced with the in-memory chat service (and no timeline) that
    ``create_app`` already supports.
    """
    monkeypatch.setattr(
        "thesis.cosmos.stack.open_cosmos_chat_stack",
        lambda: (ChatService(), None),
    )


def test_serve_starts_uvicorn_with_the_composed_asgi_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_serve_environment(monkeypatch)
    _stub_cosmos_stack(monkeypatch)
    monkeypatch.setenv("THESIS_API__BEARER_TOKEN", "serving-token")
    captured = _capture_uvicorn_run(monkeypatch)

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    application = captured["application"]
    assert isinstance(application, FastAPI)
    paths = set(application.openapi()["paths"])
    assert "/health" in paths
    assert "/v1/auth/check" in paths
    assert any(path.endswith("/events/stream") for path in paths)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000


def test_serve_honors_the_non_secret_binding_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_serve_environment(monkeypatch)
    _stub_cosmos_stack(monkeypatch)
    monkeypatch.setenv("THESIS_API__BEARER_TOKEN", "serving-token")
    monkeypatch.setenv("THESIS_SERVE_HOST", "0.0.0.0")
    monkeypatch.setenv("THESIS_SERVE_PORT", "9001")
    captured = _capture_uvicorn_run(monkeypatch)

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9001


def test_serve_fails_closed_without_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_serve_environment(monkeypatch)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not run without auth"),
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 2
    assert "authentication is not configured" in result.stderr


def test_serve_rejects_half_configured_entra_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_serve_environment(monkeypatch)
    monkeypatch.setenv("THESIS_API__ENTRA_TENANT_ID", "tenant")
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not run without auth"),
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 2
    assert "must be configured together" in result.stderr


def test_serve_accepts_entra_authentication_without_a_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_serve_environment(monkeypatch)
    _stub_cosmos_stack(monkeypatch)
    monkeypatch.setenv("THESIS_API__ENTRA_TENANT_ID", "tenant")
    monkeypatch.setenv("THESIS_API__ENTRA_AUDIENCE", "audience")
    captured = _capture_uvicorn_run(monkeypatch)

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 0, result.output
    assert isinstance(captured["application"], FastAPI)


def test_serve_rejects_an_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_serve_environment(monkeypatch)
    monkeypatch.setenv("THESIS_API__BEARER_TOKEN", "serving-token")
    monkeypatch.setenv("THESIS_SERVE_PORT", "not-a-port")
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not run on bad config"),
    )

    result = runner.invoke(cli.app, ["serve"])

    assert result.exit_code == 2
    assert "THESIS_SERVE_PORT" in result.stderr


def test_module_entrypoint_fails_closed_without_authentication() -> None:
    environment = {
        key: value for key, value in os.environ.items() if key not in _AUTH_ENVIRONMENT
    }
    result = subprocess.run(
        [sys.executable, "-m", "thesis", "serve"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 2
    assert "authentication is not configured" in result.stderr


def test_compose_reports_startup_capabilities_and_configuration() -> None:
    result = runner.invoke(cli.app, ["compose"])

    assert result.exit_code == 0, result.output
    assert "backend: thesis" in result.stdout
    assert "runtime_profile: local-loopback" in result.stdout
    assert "runtime_authentication: loopback" in result.stdout
    assert "api_application: not-composed" in result.stdout
    for capability in BACKEND_CAPABILITY_PACKAGES:
        assert capability in result.stdout
    assert f"config_root: {cli.thesis_config_dir()}" in result.stdout
    assert (
        f"runtime_config: {cli.thesis_config_dir() / 'runtime.yaml'}" in result.stdout
    )


def test_compose_reports_the_azure_profile_under_the_same_contract() -> None:
    result = runner.invoke(cli.app, ["compose", "--profile", "azure-managed"])

    assert result.exit_code == 0, result.output
    assert "runtime_profile: azure-managed" in result.stdout
    assert "runtime_authentication: entra" in result.stdout
    for capability in BACKEND_CAPABILITY_PACKAGES:
        assert capability in result.stdout


def test_compose_rejects_an_unknown_runtime_profile() -> None:
    result = runner.invoke(cli.app, ["compose", "--profile", "not-a-profile"])

    assert result.exit_code == 2
    assert "not-a-profile" in result.stderr


def test_compose_fails_clearly_without_runtime_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THESIS_CONFIG_ROOT", str(tmp_path))

    result = runner.invoke(cli.app, ["compose"])

    assert result.exit_code == 2
    assert "missing" in result.stderr
    assert str(tmp_path / "config" / "thesis") in result.stderr


def test_compose_rejects_secret_bearing_runtime_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config" / "thesis"
    config_dir.mkdir(parents=True)
    (config_dir / "runtime.yaml").write_text(
        "schema_version: 1\n"
        "default_runtime_profile: local\n"
        "runtime_profiles:\n"
        "  local:\n"
        "    api_key: super-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THESIS_CONFIG_ROOT", str(tmp_path))

    result = runner.invoke(cli.app, ["compose"])

    assert result.exit_code == 2
    assert "secret-free" in result.stderr
    assert "super-secret" not in result.stdout


def test_config_dir_resolves_the_repository_contract() -> None:
    assert cli.thesis_config_dir() == ROOT / "config" / "thesis"


def test_config_dir_honors_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THESIS_CONFIG_ROOT", str(tmp_path))

    assert cli.thesis_config_dir() == tmp_path / "config" / "thesis"


def test_config_dir_honors_an_explicit_root(tmp_path: Path) -> None:
    nested = tmp_path / "config" / "thesis"

    assert cli.thesis_config_dir(tmp_path) == tmp_path / "config" / "thesis"
    assert cli.thesis_config_dir(nested) == nested


def test_runtime_profiles_share_the_backend_process_contract() -> None:
    runtime = load_yaml_mapping(RUNTIME_CONFIG)
    profiles = runtime["runtime_profiles"]

    assert runtime["default_runtime_profile"] == "local-loopback"
    assert set(profiles) == {"local-loopback", "azure-managed"}
    for profile in profiles.values():
        assert isinstance(profile, dict)
        assert {
            "profile_id",
            "version",
            "bind_host",
            "authentication",
            "provider_profile",
        } <= set(profile)


def test_thesis_image_builds_the_backend_with_the_shared_base_pattern() -> None:
    content = DOCKERFILE_THESIS.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert lines[0] == "FROM ghcr.io/astral-sh/uv:0.12.6 AS uv"
    assert "FROM python:3.12-slim AS builder" in lines
    assert "uv sync --locked --no-dev --no-install-project" in content
    assert "uv sync --locked --no-dev --no-editable" in content
    assert "test -x /opt/venv/bin/thesis" in content
    assert "COPY --from=builder /opt/venv /opt/venv" in lines


def test_thesis_image_entrypoint_and_runtime_contract() -> None:
    content = DOCKERFILE_THESIS.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert 'ENTRYPOINT ["thesis"]' in lines
    assert 'CMD ["--help"]' in lines
    assert "THESIS_CONFIG_ROOT=/app" in content
    assert "COPY config/thesis/ /app/config/thesis/" in lines
    assert "USER thesis" in lines


def test_thesis_image_excludes_native_and_ui_boundaries() -> None:
    content = DOCKERFILE_THESIS.read_text(encoding="utf-8")

    assert "zig" not in content.lower()
    assert "src/ui" not in content
    assert "COPY --from=native" not in content


def test_thesis_image_carries_no_secrets() -> None:
    content = DOCKERFILE_THESIS.read_text(encoding="utf-8")

    assert _IMAGE_SECRET_LINE.search(content) is None


def test_shared_image_entrypoint_remains_quant_harness_owned() -> None:
    main_lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()

    assert 'ENTRYPOINT ["lake"]' in main_lines
    assert 'ENTRYPOINT ["thesis"]' not in main_lines
