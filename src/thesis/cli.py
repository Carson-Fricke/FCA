"""Thesis backend command-line facade and process boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Annotated

import typer

from harness.configuration import load_yaml_mapping
from thesis.workspaces import load_workspace_manifest

BACKEND_CAPABILITIES: tuple[str, ...] = (
    "agent",
    "artifacts",
    "auth",
    "chat",
    "cleo",
    "execution",
    "http",
    "integrations",
    "persistence",
    "providers",
    "strategies",
    "workspaces",
)

_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|connection[_-]?string|credential|password|"
    r"private[_-]?key|secret|token)(?:$|_)",
    re.IGNORECASE,
)
_ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_NON_SECRET_POLICY_KEYS = frozenset({"credential_mode"})

app = typer.Typer(
    add_completion=False,
    help="Run the Thesis backend process.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def show_help(context: typer.Context) -> None:
    """Show the Thesis process modes when no mode is selected."""
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@app.command()
def serve() -> None:
    """Serve the Thesis API process when its application is composed."""
    typer.echo(
        "The Thesis API process is scaffolded but not implemented yet.",
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def compose(
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Runtime profile to compose."),
    ] = None,
) -> None:
    """Report the composed Thesis backend capability surface."""
    config_dir = thesis_config_dir()
    runtime_path = config_dir / "runtime.yaml"
    if not runtime_path.is_file():
        typer.echo(
            f"Thesis runtime configuration is missing: {runtime_path}",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        runtime = load_yaml_mapping(runtime_path)
        _reject_runtime_secrets(runtime)
    except (OSError, ValueError) as error:
        typer.echo(
            f"Thesis runtime configuration is not secret-free: {error}",
            err=True,
        )
        raise typer.Exit(code=2) from None
    profiles = runtime.get("runtime_profiles")
    profile_id = (
        profile if profile is not None else runtime.get("default_runtime_profile")
    )
    selected = profiles.get(profile_id) if isinstance(profiles, dict) else None
    if not isinstance(profile_id, str) or not isinstance(selected, dict):
        typer.echo(f"Unknown Thesis runtime profile: {profile_id}", err=True)
        raise typer.Exit(code=2)
    unavailable = [
        capability
        for capability in BACKEND_CAPABILITIES
        if not _capability_importable(capability)
    ]
    if unavailable:
        typer.echo(
            "Thesis backend capabilities are not importable: " + ", ".join(unavailable),
            err=True,
        )
        raise typer.Exit(code=2)
    template_path = config_dir / "workspace.yaml"
    try:
        load_workspace_manifest(template_path)
    except ValueError as error:
        typer.echo(
            f"Thesis workspace template is missing or invalid: {error}",
            err=True,
        )
        raise typer.Exit(code=2) from None
    typer.echo("backend: thesis")
    typer.echo(f"config_root: {config_dir}")
    typer.echo(f"runtime_config: {runtime_path}")
    typer.echo(f"workspace_template: {template_path}")
    typer.echo(f"runtime_profile: {profile_id}")
    typer.echo(f"runtime_profile_version: {selected.get('version')}")
    typer.echo(f"runtime_bind_host: {selected.get('bind_host')}")
    typer.echo(f"runtime_authentication: {selected.get('authentication')}")
    typer.echo(f"runtime_provider_profile: {selected.get('provider_profile')}")
    typer.echo("api_application: not-composed")
    typer.echo("capabilities: " + ",".join(BACKEND_CAPABILITIES))


def thesis_config_dir(root: str | Path | None = None) -> Path:
    """Resolve the versioned Thesis configuration directory."""
    if root is None:
        configured_root = os.environ.get("THESIS_CONFIG_ROOT")
        root_path = (
            Path(configured_root)
            if configured_root
            else Path(__file__).resolve().parents[2]
        )
    else:
        root_path = Path(root)
    if root_path.name == "thesis" and root_path.parent.name == "config":
        return root_path
    return root_path / "config" / "thesis"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Thesis backend command line."""
    try:
        app(args=None if argv is None else list(argv), prog_name="thesis")
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0


def _capability_importable(capability: str) -> bool:
    try:
        import_module(f"thesis.{capability}")
    except Exception:
        return False
    return True


def _is_reference_tree(value: object) -> bool:
    if isinstance(value, str):
        return _ENVIRONMENT_REFERENCE_PATTERN.fullmatch(value) is not None
    if isinstance(value, Mapping):
        return bool(value) and all(_is_reference_tree(item) for item in value.values())
    if isinstance(value, list | tuple):
        return bool(value) and all(_is_reference_tree(item) for item in value)
    return False


def _reject_runtime_secrets(value: object, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            if (
                isinstance(key, str)
                and key not in _NON_SECRET_POLICY_KEYS
                and _SECRET_KEY_PATTERN.search(key)
                and not _is_reference_tree(item)
            ):
                raise ValueError(
                    f"{item_path} must contain only ${{ENV_VAR}} references"
                )
            _reject_runtime_secrets(item, item_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_runtime_secrets(item, f"{path}.{index}")
