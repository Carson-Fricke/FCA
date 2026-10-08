"""Injected Gitea API and real native-Git lifecycle against local bare remotes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from thesis.workspaces.gitea import GiteaWorkspaceProvider
from thesis.workspaces.local import (
    WorkspacePathError,
    WorkspaceRepositoryError,
    _git,
)
from thesis.workspaces.manifest import WorkspaceManifest, load_workspace_manifest

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
