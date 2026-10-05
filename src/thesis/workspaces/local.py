"""Filesystem and native-Git provider for isolated local workspaces."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from thesis.workspaces.manifest import (
    WorkspaceManifest,
    load_workspace_manifest,
    write_workspace_manifest,
)


class WorkspaceRepositoryError(RuntimeError):
    """Raised when a workspace repository fails an integrity check."""


class WorkspaceAlreadyExistsError(WorkspaceRepositoryError):
    """Raised when creation would replace an existing filesystem entry."""


class WorkspaceNotFoundError(WorkspaceRepositoryError):
    """Raised when a requested workspace does not exist."""


class WorkspacePathError(WorkspaceRepositoryError):
    """Raised when a path leaves its selected workspace boundary."""


WORKSPACE_DIRECTORIES = (
    ".thesis/events",
    ".thesis/journal",
    "chats",
    "investigations",
    "strategies",
    "experiments",
    "indicators",
    "models",
    "knowledge",
    "backtests",
    "uploads",
    "activations",
)
_WORKSPACE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")


def default_workspace_root(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the durable workspace root shared by local and serve runs.

    ``THESIS_WORKSPACE_ROOT`` wins when set; otherwise the operating system's
    local app-data directory is used so the same root serves every run mode.
    """
    values = os.environ if environ is None else environ
    override = (values.get("THESIS_WORKSPACE_ROOT") or "").strip()
    if override:
        return Path(override).expanduser()
    base = (values.get("LOCALAPPDATA") or values.get("XDG_DATA_HOME") or "").strip()
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "FCA" / "thesis-workspaces"


def _workspace_id(value: str) -> str:
    if _WORKSPACE_ID.fullmatch(value) is None:
        raise WorkspacePathError("workspace_id is not a safe directory name")
    return value


def _resolve_beneath(
    boundary: Path,
    relative_path: str | Path,
    *,
    must_exist: bool,
) -> Path:
    relative = Path(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if (
        relative.is_absolute()
        or relative.anchor
        or relative.drive
        or windows_path.is_absolute()
        or windows_path.anchor
        or windows_path.drive
    ):
        raise WorkspacePathError("workspace paths must be relative")
    if ".." in relative.parts:
        raise WorkspacePathError("workspace paths must not contain traversal")
    try:
        resolved = (boundary / relative).resolve(strict=must_exist)
    except OSError as error:
        raise WorkspacePathError(
            f"workspace path does not exist: {relative}"
        ) from error
    if not resolved.is_relative_to(boundary):
        raise WorkspacePathError("workspace path escapes the selected workspace")
    return resolved


def _git_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GIT_", "GCM_"))
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            timeout=30,
        )
    except FileNotFoundError as error:
        raise WorkspaceRepositoryError("native Git is required") from error
    except subprocess.TimeoutExpired as error:
        raise WorkspaceRepositoryError("Git operation timed out") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "Git command failed").strip()
        raise WorkspaceRepositoryError(detail) from error
    return result.stdout.strip()


def _is_inside_git_repository(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def _workspace_readme(workspace_id: str) -> str:
    return f"""# Thesis Workspace `{workspace_id}`

This is an isolated, Git-backed FCA Thesis strategy-family workspace.

The canonical identity, charter, limits, runtime profile, platform bundle, and
secret references are in `.thesis/workspace.yaml`. Secret values must never be
written to this repository.

## Repository Map

- `.thesis/events/`: durable user-visible execution events
- `.thesis/journal/`: accepted commands and publication outbox
- `chats/`: thread records and summaries
- `investigations/`: objectives, decisions, and finding indexes
- `strategies/`: Rust strategy source and tests
- `experiments/`: contracts, analysis, and evidence claims
- `indicators/`: workspace-authored, content-addressed indicator definitions
- `models/`: workspace-authored statistical research models
- `knowledge/`: workspace-local reference pages queried alongside shared Thesis knowledge
- `backtests/`: manifests, metrics, traces, and reports
- `uploads/`: hashed user evidence and provenance metadata
- `activations/`: bundles, consent references, and outcomes
"""


def _workspace_agents(workspace_id: str) -> str:
    return f"""# Workspace Instructions

Operate only inside workspace `{workspace_id}`.

- Treat `.thesis/workspace.yaml` as the immutable workspace identity boundary.
- Do not read or write sibling workspace directories.
- Do not commit credentials, tokens, private keys, connection strings, or
  resolved secret values. Persist only validated environment or Key Vault
  references.
- Keep durable evidence and generated artifacts in this Git repository.
- Do not weaken the platform policy pinned by `.thesis/platform.lock`.
- Require an explicit audited export/import before moving workspace content.
"""


def _workspace_gitignore() -> str:
    return """# Secret values and local credentials are never workspace content.
.env
.env.*
*.credentials.json
credentials.json
credentials.yaml
credentials.yml
secrets/
*.pem
*.key
*.p12
*.pfx

# Rebuildable local process state.
__pycache__/
.pytest_cache/
.ruff_cache/
*.log
*.pid
"""


def _platform_lock(manifest: WorkspaceManifest) -> str:
    values = {
        "schema_version": 1,
        "workspace_id": manifest.workspace_id,
        "runtime_profile": manifest.runtime_profile.model_dump(mode="json"),
        "platform_bundle": manifest.platform_bundle.model_dump(mode="json"),
    }
    return json.dumps(values, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class LocalWorkspace:
    """An opened repository whose filesystem boundary has been verified."""

    root: Path
    manifest: WorkspaceManifest

    def resolve_path(
        self,
        relative_path: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve a path only when it remains inside this workspace."""
        return _resolve_beneath(self.root, relative_path, must_exist=must_exist)


class LocalWorkspaceProvider:
    """Create and open one independently initialized Git repository per ID."""

    def __init__(self, workspace_root: str | Path) -> None:
        configured_root = Path(workspace_root).expanduser().resolve(strict=False)
        if _is_inside_git_repository(configured_root):
            raise WorkspacePathError(
                "workspace root must be outside an existing Git repository"
            )
        configured_root.mkdir(parents=True, exist_ok=True)
        root = configured_root.resolve(strict=True)
        if not root.is_dir():
            raise WorkspacePathError("workspace root must be a directory")
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def create(self, manifest: WorkspaceManifest) -> LocalWorkspace:
        """Atomically scaffold, initialize, and commit a local workspace."""
        workspace_id = _workspace_id(manifest.workspace_id)
        if manifest.repository_identity.provider != "local":
            raise WorkspaceRepositoryError(
                "local provider requires a local repository identity"
            )
        if manifest.repository_identity.repository_id != workspace_id:
            raise WorkspaceRepositoryError(
                "local repository identity must match workspace_id"
            )
        target = self._root / workspace_id
        if target.exists() or target.is_symlink():
            raise WorkspaceAlreadyExistsError(
                f"workspace already exists: {workspace_id}"
            )

        staging = Path(
            tempfile.mkdtemp(prefix=f".{workspace_id}-", dir=self._root)
        ).resolve(strict=True)
        try:
            self._write_scaffold(staging, manifest)
            _git(
                staging,
                "init",
                "--quiet",
                f"--initial-branch={manifest.repository_identity.default_branch}",
            )
            _git(staging, "add", "--all")
            _git(
                staging,
                "-c",
                "user.name=FCA Thesis",
                "-c",
                "user.email=thesis@localhost",
                "-c",
                "core.hooksPath=.git/no-hooks",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                "Initialize Thesis workspace",
            )
            try:
                os.replace(staging, target)
            except OSError as error:
                if target.exists() or target.is_symlink():
                    raise WorkspaceAlreadyExistsError(
                        f"workspace already exists: {workspace_id}"
                    ) from error
                raise
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return self.open(workspace_id)

    def open(self, workspace_id: str) -> LocalWorkspace:
        """Open a direct child workspace and verify its identity and Git root."""
        workspace_id = _workspace_id(workspace_id)
        target = self._root / workspace_id
        if not target.exists() and not target.is_symlink():
            raise WorkspaceNotFoundError(f"workspace not found: {workspace_id}")
        if target.is_symlink():
            raise WorkspacePathError("workspace directory must not be a symlink")
        try:
            resolved = target.resolve(strict=True)
        except OSError as error:
            raise WorkspaceNotFoundError(
                f"workspace not found: {workspace_id}"
            ) from error
        if resolved.parent != self._root or not resolved.is_dir():
            raise WorkspacePathError("workspace is not a direct root child")

        manifest_path = _resolve_beneath(
            resolved, ".thesis/workspace.yaml", must_exist=True
        )
        manifest = load_workspace_manifest(manifest_path)
        if manifest.workspace_id != workspace_id:
            raise WorkspaceRepositoryError(
                "workspace directory and manifest identities do not match"
            )
        if manifest.repository_identity.provider != "local":
            raise WorkspaceRepositoryError(
                "workspace manifest does not identify a local repository"
            )
        if manifest.repository_identity.repository_id != workspace_id:
            raise WorkspaceRepositoryError(
                "local repository identity does not match workspace_id"
            )
        self._verify_repository(resolved, manifest)
        return LocalWorkspace(root=resolved, manifest=manifest)

    def resolve_path(
        self,
        workspace_id: str,
        relative: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve a selected workspace path without accepting a raw root path.

        The parameter names match ``WorkspacePathProvider`` so the provider
        satisfies the protocol when passed as a keyword-capable boundary.
        """
        return self.open(workspace_id).resolve_path(relative, must_exist=must_exist)

    @staticmethod
    def _write_scaffold(root: Path, manifest: WorkspaceManifest) -> None:
        for relative in WORKSPACE_DIRECTORIES:
            directory = root / relative
            directory.mkdir(parents=True, exist_ok=False)
            (directory / ".gitkeep").write_text("", encoding="utf-8")
        write_workspace_manifest(root / ".thesis/workspace.yaml", manifest)
        (root / ".thesis/platform.lock").write_text(
            _platform_lock(manifest), encoding="utf-8"
        )
        (root / "AGENTS.md").write_text(
            _workspace_agents(manifest.workspace_id), encoding="utf-8"
        )
        (root / "README.md").write_text(
            _workspace_readme(manifest.workspace_id), encoding="utf-8"
        )
        (root / ".gitignore").write_text(_workspace_gitignore(), encoding="utf-8")

    def publish(self, workspace_id: str) -> str:
        """Return the existing commit; local publication is handled by the journal."""
        return _git(self.open(workspace_id).root, "rev-parse", "HEAD")

    def repository_uri(self, workspace_id: str) -> str:
        """Return the verified local repository's file locator."""
        return self.open(workspace_id).root.as_uri()

    @staticmethod
    def _verify_repository(root: Path, manifest: WorkspaceManifest) -> None:
        git_directory = root / ".git"
        if git_directory.is_symlink() or not git_directory.is_dir():
            raise WorkspaceRepositoryError(
                "workspace must own a separate .git directory"
            )
        top_level_text = _git(root, "rev-parse", "--show-toplevel")
        try:
            top_level = Path(top_level_text).resolve(strict=True)
        except OSError as error:
            raise WorkspaceRepositoryError("Git reported an invalid root") from error
        if top_level != root:
            raise WorkspaceRepositoryError(
                "workspace Git root does not match the selected workspace"
            )
        branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != manifest.repository_identity.default_branch:
            raise WorkspaceRepositoryError(
                "workspace Git branch does not match repository identity"
            )


__all__ = [
    "LocalWorkspace",
    "LocalWorkspaceProvider",
    "WORKSPACE_DIRECTORIES",
    "WorkspaceAlreadyExistsError",
    "WorkspaceNotFoundError",
    "WorkspacePathError",
    "WorkspaceRepositoryError",
    "default_workspace_root",
]
