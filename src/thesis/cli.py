"""Thesis backend command-line facade and process boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from harness.configuration import load_yaml_mapping
from thesis.http.application import serving_app_from_environment
from thesis.identity.invites import (
    InviteError,
    InviteListingUnavailableError,
    InviteService,
)
from thesis.identity.local import LocalInviteStore, default_local_identity_root
from thesis.identity.models import AccountRole, Invite
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

_DEFAULT_SERVE_HOST = "127.0.0.1"
_DEFAULT_SERVE_PORT = 8000
_DEFAULT_INVITE_LINK_BASE = "http://localhost:5173/signup"

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


class _ListableLocalInviteStore(LocalInviteStore):
    """Local invite store extended with the directory listing the CLI needs.

    The shared ``InviteStore`` port is intentionally write/consume only, so the
    composition root adds the read-only enumeration ``invite list`` needs here
    instead of changing the shared adapter and port owned by T302/T304.
    """

    def list(self) -> tuple[Invite, ...]:
        records = self._read().values()
        return tuple(sorted(records, key=lambda invite: (invite.created_at, invite.id)))


invite_app = typer.Typer(
    add_completion=False,
    help="Manage one-time Thesis invitation links.",
)
app.add_typer(invite_app, name="invite")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _local_invite_service(root: str | None = None) -> InviteService:
    """Compose the invite service over the local identity store."""
    directory = (
        Path(root).expanduser() if root is not None else default_local_identity_root()
    )
    store = _ListableLocalInviteStore(directory / "invites.json")
    return InviteService(store, clock=_utc_now)


def _invite_role(value: str) -> AccountRole:
    if value not in ("owner", "admin", "member"):
        raise typer.BadParameter("role must be one of owner, admin, or member")
    return value


def _invite_state(invite: Invite, at: datetime) -> str:
    if invite.consumed_at is not None:
        return "redeemed"
    if invite.revoked_at is not None:
        return "revoked"
    if at >= invite.expires_at:
        return "expired"
    return "active"


@invite_app.command("create")
def invite_create(
    email: Annotated[
        str | None,
        typer.Option("--email", help="Restrict redemption to this address."),
    ] = None,
    role: Annotated[
        str,
        typer.Option("--role", help="Role the redeemed account receives."),
    ] = "member",
    expires_in_days: Annotated[
        int,
        typer.Option("--expires-in-days", help="Days the link stays valid."),
    ] = 7,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Free-form note stored with the invite."),
    ] = None,
    created_by: Annotated[
        str,
        typer.Option("--created-by", help="Operator recorded as the creator."),
    ] = "operator",
    invite_link_base: Annotated[
        str | None,
        typer.Option(
            "--invite-link-base",
            help="Signup URL the one-time token is appended to.",
        ),
    ] = None,
    root: Annotated[
        str | None,
        typer.Option("--root", help="Identity store root; defaults to .local."),
    ] = None,
) -> None:
    """Create one invite and print its one-time signup link."""
    base = (
        invite_link_base
        or os.environ.get("THESIS_INVITE_LINK_BASE")
        or _DEFAULT_INVITE_LINK_BASE
    )
    service = _local_invite_service(root)
    try:
        issued = service.create_invite(
            email=email,
            role=_invite_role(role),
            expires_in=timedelta(days=expires_in_days),
            note=note,
            created_by=created_by,
            invite_link_base=base,
        )
    except (InviteError, ValueError) as error:
        typer.echo(f"thesis invite create failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"invite_id: {issued.invite.id}")
    typer.echo(f"expires_at: {issued.invite.expires_at.isoformat()}")
    typer.echo(f"invite_link: {issued.invite_link}")


@invite_app.command("list")
def invite_list(
    root: Annotated[
        str | None,
        typer.Option("--root", help="Identity store root; defaults to .local."),
    ] = None,
) -> None:
    """List invites without exposing token material."""
    service = _local_invite_service(root)
    try:
        invites = service.list_invites()
    except InviteListingUnavailableError as error:
        typer.echo(f"thesis invite list failed: {error}", err=True)
        raise typer.Exit(code=2) from None
    if not invites:
        typer.echo("no invites")
        return
    now = _utc_now()
    for invite in invites:
        email = invite.bound_email or "-"
        typer.echo(
            f"{invite.id}\t{email}\t{invite.role}\t"
            f"{_invite_state(invite, now)}\t{invite.expires_at.isoformat()}"
        )


@invite_app.command("revoke")
def invite_revoke(
    invite_id: Annotated[
        str, typer.Argument(help="Identifier of the invite to revoke.")
    ],
    root: Annotated[
        str | None,
        typer.Option("--root", help="Identity store root; defaults to .local."),
    ] = None,
) -> None:
    """Revoke an unconsumed invite before it is redeemed."""
    service = _local_invite_service(root)
    if service.revoke_invite(invite_id, at=_utc_now()):
        typer.echo(f"revoked: {invite_id}")
        return
    typer.echo(f"not revoked: {invite_id}", err=True)
    raise typer.Exit(code=1)


@app.command()
def serve(
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Bind host; defaults to THESIS_SERVE_HOST or 127.0.0.1.",
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Bind port; defaults to THESIS_SERVE_PORT or 8000.",
        ),
    ] = None,
) -> None:
    """Serve the Thesis API process over HTTP."""
    try:
        bind_host, bind_port = serve_binding(host=host, port=port)
        application = serving_app_from_environment()
    except ValueError as error:
        typer.echo(f"thesis serve cannot start: {error}", err=True)
        raise typer.Exit(code=2) from None
    uvicorn.run(
        application,
        host=bind_host,
        port=bind_port,
        log_level="info",
    )


def serve_binding(
    *, host: str | None = None, port: int | None = None
) -> tuple[str, int]:
    """Resolve the non-secret serve bind address from options or environment.

    The host falls back to ``THESIS_SERVE_HOST`` and then to loopback, while
    the port falls back to ``THESIS_SERVE_PORT`` and then to 8000.  An
    unparseable or out-of-range port fails closed with a clear error.
    """
    resolved_host = host if host is not None else os.environ.get("THESIS_SERVE_HOST")
    resolved_host = resolved_host.strip() if resolved_host is not None else ""
    if not resolved_host:
        resolved_host = _DEFAULT_SERVE_HOST

    resolved_port = port
    if resolved_port is None:
        raw_port = os.environ.get("THESIS_SERVE_PORT")
        if raw_port is None or not raw_port.strip():
            resolved_port = _DEFAULT_SERVE_PORT
        else:
            try:
                resolved_port = int(raw_port.strip())
            except ValueError as error:
                raise ValueError(
                    f"THESIS_SERVE_PORT must be an integer, got {raw_port!r}"
                ) from error
    if not 1 <= resolved_port <= 65535:
        raise ValueError(f"serve port must be between 1 and 65535, got {resolved_port}")
    return resolved_host, resolved_port


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
