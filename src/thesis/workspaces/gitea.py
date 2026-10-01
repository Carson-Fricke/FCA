"""Private Gitea repositories with rebuildable, isolated native-Git checkouts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from thesis.workspaces.local import (
    LocalWorkspace,
    LocalWorkspaceProvider,
    WorkspaceNotFoundError,
    WorkspacePathError,
    WorkspaceRepositoryError,
    _git_environment,
    _resolve_beneath,
    _workspace_id,
)
from thesis.workspaces.manifest import (
    WorkspaceManifest,
    load_workspace_manifest,
    manifest_sha256,
    parse_workspace_manifest,
)


def _ownership_marker(manifest: WorkspaceManifest) -> str:
    identity = json.dumps(
        [
            manifest.workspace_id,
            manifest.owner.identity_provider,
            manifest.owner.owner_id,
            manifest_sha256(manifest),
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return "fca-thesis-workspace:v1:" + hashlib.sha256(identity).hexdigest()


def _remove_readonly(
    function: Callable[..., Any], path: str, error: BaseException
) -> None:
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


class GiteaWorkspaceProvider:
    """Reconcile owned private remotes without discarding local work or secrets."""

    def __init__(
        self,
        cache_root: str | Path,
        base_url: str,
        organization: str,
        token: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        try:
            endpoint = urlsplit(base_url)
            valid = (
                endpoint.scheme == "https"
                or endpoint.scheme == "http"
                and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (
                not valid
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
                or any(character.isspace() for character in base_url)
                or "\\" in base_url
            ):
                raise ValueError
            _ = endpoint.port
        except ValueError:
            raise WorkspaceRepositoryError("invalid Gitea base endpoint") from None
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", organization) is None:
            raise WorkspacePathError("invalid Gitea organization")
        if not token or any(ord(character) < 33 for character in token):
            raise WorkspaceRepositoryError("invalid Gitea credential")
        self._base_url = base_url.rstrip("/")
        self._organization = organization
        self._token = token
        self._client = client if client is not None else httpx.Client(timeout=30)
        self._root = LocalWorkspaceProvider(cache_root).root

    @property
    def root(self) -> Path:
        if self._root.is_symlink() or self._root.resolve() != self._root:
            raise WorkspacePathError("workspace cache root was redirected")
        return self._root

    def repository_uri(self, workspace_id: str) -> str:
        """Return a credential-free locator derived only from validated settings."""
        return (
            f"{self._base_url}/{self._organization}/{_workspace_id(workspace_id)}.git"
        )

    def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                self._base_url + "/api/v1/" + path,
                headers={"Authorization": "token " + self._token},
                json=body,
                follow_redirects=False,
                timeout=30,
            )
        except httpx.HTTPError:
            raise WorkspaceRepositoryError("Gitea API request failed") from None

    def _repository(self, workspace_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"repos/{self._organization}/{workspace_id}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise WorkspaceRepositoryError("Gitea repository lookup failed")
        try:
            values = response.json()
        except ValueError:
            raise WorkspaceRepositoryError(
                "invalid Gitea repository response"
            ) from None
        if not isinstance(values, dict):
            raise WorkspaceRepositoryError("invalid Gitea repository response")
        return values

    def _validate_remote(
        self,
        workspace_id: str,
        repository: dict[str, Any],
        manifest: WorkspaceManifest | None = None,
    ) -> str:
        owner = repository.get("owner")
        branch = repository.get("default_branch")
        description = repository.get("description")
        if (
            repository.get("private") is not True
            or repository.get("name") != workspace_id
            or not isinstance(owner, dict)
            or owner.get("login") != self._organization
            or repository.get("clone_url") != self.repository_uri(workspace_id)
            or not isinstance(description, str)
            or re.fullmatch(r"fca-thesis-workspace:v1:[0-9a-f]{64}", description)
            is None
            or not isinstance(branch, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) is None
        ):
            raise WorkspaceRepositoryError("Gitea repository integrity check failed")
        self._git(self.root, "check-ref-format", "--branch", branch)
        if manifest is not None and (
            description != _ownership_marker(manifest)
            or branch != manifest.repository_identity.default_branch
        ):
            raise WorkspaceRepositoryError(
                "Gitea repository ownership, branch, or manifest mismatch"
            )
        return branch

    def _git(self, repository: Path, *arguments: str) -> str:
        environment = _git_environment()
        authorization = base64.b64encode(
            ("oauth2:" + self._token).encode("utf-8")
        ).decode("ascii")
        settings = {
            f"http.{self._base_url}/.extraHeader": "Authorization: Basic "
            + authorization,
            "http.followRedirects": "false",
            "credential.helper": "",
            "core.hooksPath": os.devnull,
            "protocol.file.allow": "never",
        }
        environment["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings.items()):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), *arguments],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            # Git diagnostics can echo HTTP credentials, even when URLs are clean.
            raise WorkspaceRepositoryError("Gitea Git operation failed") from None
        return result.stdout.strip()

    def _validate_manifest(
        self, manifest: WorkspaceManifest, workspace_id: str
    ) -> None:
        if (
            manifest.workspace_id != workspace_id
            or manifest.repository_identity.provider != "azure"
            or manifest.repository_identity.repository_id != workspace_id
        ):
            raise WorkspaceRepositoryError(
                "invalid Azure workspace repository identity"
            )

    def _read_manifest(
        self, root: Path, revision: str | None = None
    ) -> WorkspaceManifest:
        try:
            if revision is not None:
                return parse_workspace_manifest(
                    self._git(root, "show", revision + ":.thesis/workspace.yaml")
                )
            return load_workspace_manifest(
                _resolve_beneath(root, ".thesis/workspace.yaml", must_exist=True)
            )
        except ValueError:
            raise WorkspaceRepositoryError("invalid workspace manifest") from None

    def _checkout(
        self, root: Path, workspace_id: str, repository: dict[str, Any]
    ) -> LocalWorkspace:
        if (
            root.is_symlink()
            or root.resolve() != root
            or root.parent != self.root
            or not root.is_dir()
        ):
            raise WorkspacePathError("workspace is not an isolated cache child")
        git_directory = root / ".git"
        if (
            git_directory.is_symlink()
            or not git_directory.is_dir()
            or git_directory.resolve().parent != root
        ):
            raise WorkspaceRepositoryError(
                "workspace must own a separate .git directory"
            )
        if (git_directory / "config").is_symlink():
            raise WorkspaceRepositoryError("unsafe workspace Git configuration")
        if (
            any(path.is_symlink() for path in git_directory.rglob("*"))
            or (git_directory / "objects/info/alternates").exists()
        ):
            raise WorkspaceRepositoryError("redirected workspace Git metadata")
        # Reject local configuration capable of rewriting URLs or invoking helpers.
        allowed = re.compile(
            r"(?:core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|"
            r"symlinks|ignorecase|longpaths)|remote\.origin\.(?:url|fetch)|"
            r"branch\.[A-Za-z0-9._/-]+\.(?:remote|merge))"
        )
        for line in self._git(root, "config", "--local", "--list").splitlines():
            key, separator, _ = line.partition("=")
            if not separator or allowed.fullmatch(key) is None:
                raise WorkspaceRepositoryError("unsafe workspace Git configuration")
        if self._git(root, "remote", "get-url", "origin") != self.repository_uri(
            workspace_id
        ):
            raise WorkspaceRepositoryError("workspace remote locator mismatch")
        if Path(self._git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise WorkspaceRepositoryError("workspace Git root mismatch")
        manifest = self._read_manifest(root)
        self._validate_manifest(manifest, workspace_id)
        self._validate_remote(workspace_id, repository, manifest)
        if (
            self._git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
            != manifest.repository_identity.default_branch
        ):
            raise WorkspaceRepositoryError("workspace Git branch mismatch")
        committed = self._read_manifest(root, "HEAD")
        if committed != manifest:
            raise WorkspaceRepositoryError("workspace identity has uncommitted changes")
        return LocalWorkspace(root, manifest)

    def create(self, manifest: WorkspaceManifest) -> LocalWorkspace:
        """Create or reconcile the same manifest, including an owned empty remote."""
        workspace_id = _workspace_id(manifest.workspace_id)
        self._validate_manifest(manifest, workspace_id)
        repository = self._repository(workspace_id)
        if repository is None:
            response = self._request(
                "POST",
                f"orgs/{self._organization}/repos",
                body={
                    "name": workspace_id,
                    "private": True,
                    "auto_init": False,
                    "description": _ownership_marker(manifest),
                    "default_branch": manifest.repository_identity.default_branch,
                },
            )
            if response.status_code not in {201, 409, 422}:
                raise WorkspaceRepositoryError("Gitea repository creation failed")
            repository = self._repository(workspace_id)
        if repository is None:
            raise WorkspaceRepositoryError(
                "Gitea repository creation was not confirmed"
            )
        branch = self._validate_remote(workspace_id, repository, manifest)
        target = self.root / workspace_id
        if target.exists() or target.is_symlink():
            workspace = self.open(workspace_id)
            if workspace.manifest != manifest:
                raise WorkspaceRepositoryError("workspace creation manifest mismatch")
            self.publish(workspace_id)
            return workspace
        uri = self.repository_uri(workspace_id)
        heads = self._git(self.root, "ls-remote", "--heads", uri)
        if heads:
            workspace = self.open(workspace_id)
            if workspace.manifest != manifest:
                raise WorkspaceRepositoryError("workspace creation manifest mismatch")
            return workspace
        staging = Path(
            tempfile.mkdtemp(prefix=f".{workspace_id}-", dir=self.root)
        ).resolve()
        try:
            LocalWorkspaceProvider._write_scaffold(staging, manifest)
            self._git(staging, "init", "--quiet", f"--initial-branch={branch}")
            self._git(staging, "remote", "add", "origin", uri)
            self._git(staging, "add", "--all")
            self._git(
                staging,
                "-c",
                "user.name=FCA Thesis",
                "-c",
                "user.email=thesis@localhost",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                "Initialize Thesis workspace",
            )
            self._checkout(staging, workspace_id, repository)
            self._git(staging, "push", uri, f"HEAD:refs/heads/{branch}")
            if target.exists() or target.is_symlink():
                raise WorkspaceRepositoryError("workspace cache creation conflict")
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, onexc=_remove_readonly)
        return self.open(workspace_id)

    def open(self, workspace_id: str) -> LocalWorkspace:
        """Rebuild missing caches; fast-forward clean checkouts, never reset work."""
        workspace_id = _workspace_id(workspace_id)
        repository = self._repository(workspace_id)
        if repository is None:
            raise WorkspaceNotFoundError("Gitea workspace repository not found")
        branch = self._validate_remote(workspace_id, repository)
        if not self._git(
            self.root, "ls-remote", "--heads", self.repository_uri(workspace_id)
        ):
            raise WorkspaceNotFoundError("Gitea workspace repository is not seeded")
        target = self.root / workspace_id
        if not target.exists() and not target.is_symlink():
            staging = Path(
                tempfile.mkdtemp(prefix=f".{workspace_id}-", dir=self.root)
            ).resolve()
            try:
                self._git(
                    self.root,
                    "clone",
                    "--quiet",
                    "--no-local",
                    "--single-branch",
                    "--branch",
                    branch,
                    self.repository_uri(workspace_id),
                    str(staging),
                )
                self._checkout(staging, workspace_id, repository)
                if target.exists() or target.is_symlink():
                    raise WorkspaceRepositoryError("workspace cache creation conflict")
                os.rename(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, onexc=_remove_readonly)
        workspace = self._checkout(target, workspace_id, repository)
        heads = self._git(
            target, "ls-remote", "--heads", self.repository_uri(workspace_id)
        )
        if not heads:
            return workspace
        if not any(
            line.endswith("\trefs/heads/" + branch) for line in heads.splitlines()
        ):
            raise WorkspaceRepositoryError("remote workspace branch is missing")
        if self._git(target, "status", "--porcelain"):
            remote_head = next(
                line.split("\t")[0]
                for line in heads.splitlines()
                if line.endswith("\trefs/heads/" + branch)
            )
            if remote_head not in self._git(target, "rev-list", "HEAD").splitlines():
                raise WorkspaceRepositoryError(
                    "remote changed while workspace is dirty"
                )
            return workspace
        self._git(
            target,
            "fetch",
            "--quiet",
            "--no-tags",
            self.repository_uri(workspace_id),
            f"refs/heads/{branch}",
        )
        remote_manifest = self._read_manifest(target, "FETCH_HEAD")
        if remote_manifest != workspace.manifest:
            raise WorkspaceRepositoryError("remote workspace manifest conflict")
        counts = self._git(
            target, "rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"
        ).split()
        if len(counts) != 2 or (counts[0] != "0" and counts[1] != "0"):
            raise WorkspaceRepositoryError("remote workspace history conflict")
        if counts[1] != "0":
            self._git(target, "merge", "--ff-only", "--no-edit", "FETCH_HEAD")
        return self._checkout(target, workspace_id, repository)

    def resolve_path(
        self, workspace_id: str, relative: str | Path = ".", *, must_exist: bool = False
    ) -> Path:
        return self.open(workspace_id).resolve_path(relative, must_exist=must_exist)

    def publish(self, workspace_id: str) -> str:
        """Push existing commits only; dirty files are neither staged nor committed."""
        workspace = self.open(workspace_id)
        head = self._git(workspace.root, "rev-parse", "HEAD")
        self._git(
            workspace.root,
            "push",
            self.repository_uri(workspace_id),
            f"HEAD:refs/heads/{workspace.manifest.repository_identity.default_branch}",
        )
        return head
