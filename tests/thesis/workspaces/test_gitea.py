"""Injected Gitea API and real native-Git lifecycle against local bare remotes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from thesis.workspaces.gitea import GiteaWorkspaceProvider, _remove_readonly
from thesis.workspaces.local import (
    WorkspacePathError,
    WorkspaceRepositoryError,
    _git,
)
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest
from thesis.workspaces.repository import WorkspaceRepositoryProvider

TOKEN = "test-only-sensitive-token"
BASE = "https://gitea.example.test"


@pytest.fixture
def manifest() -> WorkspaceManifest:
    values = load_workspace_manifest("config/thesis/workspace.yaml").model_dump(
        mode="json"
    )
    values["repository_identity"]["provider"] = "azure"
    values["runtime_profile"]["mode"] = "azure"
    return WorkspaceManifest.model_validate(values)


class Service:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repositories: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == "token " + TOKEN
        assert TOKEN not in str(request.url)
        if request.method == "POST":
            values = json.loads(request.content)
            assert values["private"] is True
            assert values["auto_init"] is False
            name = values["name"]
            if name in self.repositories:
                return httpx.Response(409)
            _git(
                self.root,
                "init",
                "--bare",
                "--quiet",
                "--initial-branch=main",
                str(self.root / (name + ".git")),
            )
            self.repositories[name] = {
                **values,
                "owner": {"login": "workspaces"},
                "clone_url": BASE + "/workspaces/" + name + ".git",
            }
            return httpx.Response(201, json=self.repositories[name])
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in self.repositories:
            return httpx.Response(404)
        return httpx.Response(200, json=self.repositories[name])


@pytest.fixture
def service(tmp_path: Path) -> Service:
    root = tmp_path / "remotes"
    root.mkdir()
    return Service(root)


@pytest.fixture
def provider(
    tmp_path: Path, service: Service, monkeypatch: pytest.MonkeyPatch
) -> GiteaWorkspaceProvider:
    provider = GiteaWorkspaceProvider(
        tmp_path / "cache",
        BASE,
        "workspaces",
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(service.handle)),
    )

    def git(repository: Path, *arguments: str) -> str:
        # Replace only network locators; clone/fetch/merge/push still use native Git.
        translated = tuple(
            str(service.root / argument.rsplit("/", 1)[-1])
            if argument.startswith(BASE + "/workspaces/")
            else argument
            for argument in arguments
        )
        result = _git(repository, *translated)
        if arguments == ("remote", "get-url", "origin"):
            return BASE + "/workspaces/" + Path(result).name
        return result

    monkeypatch.setattr(provider, "_git", git)
    return provider


@pytest.mark.slow
def test_create_reconcile_publish_and_rebuild(
    provider: GiteaWorkspaceProvider,
    service: Service,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary: WorkspaceRepositoryProvider = provider
    workspace = boundary.create(manifest)
    assert workspace.manifest == manifest
    assert "Rust strategy source" in (workspace.root / "README.md").read_text()
    assert boundary.create(manifest).root == workspace.root
    assert sum(request.method == "POST" for request in service.requests) == 1
    assert TOKEN not in (workspace.root / ".git/config").read_text()
    assert boundary.publish(manifest.workspace_id) == _git(
        workspace.root, "rev-parse", "HEAD"
    )
    second = GiteaWorkspaceProvider(
        provider.root.parent / "second",
        BASE,
        "workspaces",
        TOKEN,
        client=provider._client,
    )
    monkeypatch.setattr(second, "_git", provider._git)
    reopened = second.open(manifest.workspace_id)
    assert reopened.manifest == manifest
    assert reopened.root != workspace.root
    (reopened.root / "chats/accepted.json").write_text("{}")
    _git(reopened.root, "add", "chats/accepted.json")
    _git(
        reopened.root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        "Accepted journal commit",
    )
    second.publish(manifest.workspace_id)
    assert (boundary.open(manifest.workspace_id).root / "chats/accepted.json").is_file()
    shutil.rmtree(workspace.root, onexc=_remove_readonly)
    assert boundary.open(manifest.workspace_id).manifest == manifest


@pytest.mark.slow
def test_empty_remote_is_not_listable_until_seeded(
    provider: GiteaWorkspaceProvider,
    service: Service,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = provider._git

    def fail_push(root: Path, *arguments: str) -> str:
        if arguments[0] == "push":
            raise WorkspaceRepositoryError("push failed")
        return git(root, *arguments)

    monkeypatch.setattr(provider, "_git", fail_push)
    with pytest.raises(WorkspaceRepositoryError):
        provider.create(manifest)
    monkeypatch.setattr(provider, "_git", git)
    from thesis.workspaces.local import WorkspaceNotFoundError

    with pytest.raises(WorkspaceNotFoundError, match="not seeded"):
        provider.open(manifest.workspace_id)
    assert provider.create(manifest).manifest == manifest


@pytest.mark.parametrize(
    "field,value",
    [
        ("private", False),
        ("default_branch", "--upload-pack=evil"),
        ("clone_url", "https://attacker.test/workspaces/equity-momentum.git"),
        (
            "clone_url",
            "https://token@gitea.example.test/workspaces/equity-momentum.git",
        ),
        ("clone_url", BASE + "/workspaces/equity-momentum.git?token=" + TOKEN),
    ],
)
@pytest.mark.slow
def test_remote_integrity_fail_closed(
    provider: GiteaWorkspaceProvider,
    service: Service,
    manifest: WorkspaceManifest,
    field: str,
    value: object,
) -> None:
    provider.create(manifest)
    service.repositories[manifest.workspace_id][field] = value
    with pytest.raises(WorkspaceRepositoryError) as error:
        provider.open(manifest.workspace_id)
    assert TOKEN not in str(error.value)


@pytest.mark.slow
def test_owner_and_manifest_reconciliation(
    provider: GiteaWorkspaceProvider, manifest: WorkspaceManifest
) -> None:
    provider.create(manifest)
    values = manifest.model_dump(mode="json")
    values["owner"]["owner_id"] = "someone-else"
    with pytest.raises(WorkspaceRepositoryError, match="ownership"):
        provider.create(WorkspaceManifest.model_validate(values))
    values = manifest.model_dump(mode="json")
    values["charter"]["title"] = "Different manifest"
    with pytest.raises(WorkspaceRepositoryError, match="manifest mismatch"):
        provider.create(WorkspaceManifest.model_validate(values))


@pytest.mark.slow
def test_owned_empty_remote_recovers_after_push_failure(
    provider: GiteaWorkspaceProvider,
    manifest: WorkspaceManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = provider._git

    def fail_push(root: Path, *arguments: str) -> str:
        if arguments[0] == "push":
            raise WorkspaceRepositoryError("injected push failure")
        return git(root, *arguments)

    monkeypatch.setattr(provider, "_git", fail_push)
    with pytest.raises(WorkspaceRepositoryError, match="push failure"):
        provider.create(manifest)
    assert not (provider.root / manifest.workspace_id).exists()
    monkeypatch.setattr(provider, "_git", git)
    assert provider.create(manifest).manifest == manifest


@pytest.mark.slow
def test_history_conflict_does_not_reset(
    provider: GiteaWorkspaceProvider, service: Service, manifest: WorkspaceManifest
) -> None:
    workspace = provider.create(manifest)
    remote_checkout = provider.root.parent / "writer"
    _git(
        service.root,
        "clone",
        str(service.root / (manifest.workspace_id + ".git")),
        str(remote_checkout),
    )
    for root, name in [(workspace.root, "local"), (remote_checkout, "remote")]:
        (root / "chats" / name).write_text(name)
        _git(root, "add", "--all")
        _git(
            root,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            name,
        )
    local_head = _git(workspace.root, "rev-parse", "HEAD")
    _git(remote_checkout, "push", "origin", "main")
    with pytest.raises(WorkspaceRepositoryError, match="history conflict"):
        provider.open(manifest.workspace_id)
    assert _git(workspace.root, "rev-parse", "HEAD") == local_head
    (workspace.root / "chats/dirty").write_text("dirty")
    with pytest.raises(WorkspaceRepositoryError, match="dirty"):
        provider.open(manifest.workspace_id)
    assert (workspace.root / "chats/dirty").read_text() == "dirty"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://external.test",
        "https://user:password@example.test",
        "https://example.test/path",
        "https://example.test?token=secret",
        "https://example.test/#fragment",
    ],
)
def test_invalid_endpoints(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(WorkspaceRepositoryError):
        GiteaWorkspaceProvider(tmp_path / "cache", endpoint, "workspaces", TOKEN)


def test_transient_credentials_and_sanitized_git_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = GiteaWorkspaceProvider(tmp_path / "cache", BASE, "workspaces", TOKEN)

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert TOKEN not in " ".join(arguments)
        environment = kwargs["env"]
        assert environment["GIT_CONFIG_COUNT"] == "5"
        assert "Authorization: Basic " in environment["GIT_CONFIG_VALUE_0"]
        assert environment["GIT_CONFIG_VALUE_1"] == "false"
        raise subprocess.CalledProcessError(1, arguments, stderr=TOKEN)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(WorkspaceRepositoryError) as error:
        provider._git(
            provider.root, "ls-remote", provider.repository_uri("test-workspace")
        )
    assert TOKEN not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "status,body", [(302, {}), (500, {"message": TOKEN}), (200, []), (200, "invalid")]
)
def test_api_errors_are_sanitized(tmp_path: Path, status: int, body: object) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    )
    provider = GiteaWorkspaceProvider(
        tmp_path / "cache", BASE, "workspaces", TOKEN, client=client
    )
    with pytest.raises(WorkspaceRepositoryError) as error:
        provider.open("test-workspace")
    assert TOKEN not in str(error.value)


@pytest.mark.slow
def test_workspace_isolation_and_manifest_tampering(
    provider: GiteaWorkspaceProvider, manifest: WorkspaceManifest
) -> None:
    first = provider.create(manifest)
    values = manifest.model_dump(mode="json")
    values["workspace_id"] = "second-workspace"
    values["repository_identity"]["repository_id"] = "second-workspace"
    values["owner"]["owner_id"] = "second-owner"
    second = provider.create(WorkspaceManifest.model_validate(values))
    assert first.root != second.root
    assert provider.repository_uri(manifest.workspace_id) != provider.repository_uri(
        "second-workspace"
    )
    manifest_path = first.root / ".thesis/workspace.yaml"
    manifest_path.write_text((second.root / ".thesis/workspace.yaml").read_text())
    with pytest.raises(WorkspaceRepositoryError, match="identity"):
        provider.open(manifest.workspace_id)
    assert provider.open("second-workspace").manifest.owner.owner_id == "second-owner"


@pytest.mark.parametrize(
    "organization", ["../other", "-option", "owner/name", "user@host", ""]
)
def test_invalid_organization(tmp_path: Path, organization: str) -> None:
    with pytest.raises(WorkspacePathError):
        GiteaWorkspaceProvider(tmp_path / "cache", BASE, organization, TOKEN)


def test_loopback_endpoint_and_api_transport_failure(tmp_path: Path) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(TOKEN, request=request)

    provider = GiteaWorkspaceProvider(
        tmp_path / "cache",
        "http://127.0.0.1:3000",
        "workspaces",
        TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )
    assert (
        provider.repository_uri("test-workspace")
        == "http://127.0.0.1:3000/workspaces/test-workspace.git"
    )
    with pytest.raises(WorkspaceRepositoryError) as error:
        provider.open("test-workspace")
    assert TOKEN not in str(error.value)
