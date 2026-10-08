"""Workspace-qualified hypothesis persistence and publication contracts."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thesis.persistence.git import GitCoordinator
from thesis.research import hypothesis
from thesis.research.hypothesis import (
    HypothesisArtifactError,
    HypothesisDocument,
    HypothesisFrontMatter,
    HypothesisStatus,
    InMemoryHypothesisStore,
    WorkspaceHypothesisStore,
)
from thesis.workspaces.local import (
    LocalWorkspaceProvider,
    WorkspaceNotFoundError,
    WorkspacePathError,
)
from thesis.workspaces.manifest import WorkspaceManifest


def _manifest(workspace_id: str) -> WorkspaceManifest:
    return WorkspaceManifest.model_validate(
        {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "created_by": "operator",
            "owner": {
                "owner_id": "operator",
                "identity_provider": "local",
                "display_name": "Operator",
            },
            "charter": {
                "family_id": "research",
                "title": "Research",
                "objective": "Test hypotheses",
                "markets": ("SPY",),
                "horizons": ("daily",),
                "relationship": "Related hypotheses",
                "out_of_scope": (),
            },
            "limits": {
                "max_repository_bytes": 10000000,
                "max_artifact_bytes": 1000000,
                "max_concurrent_executions": 1,
                "max_backtests_per_experiment": 10,
                "max_model_tokens_per_execution": 10000,
                "max_web_requests_per_execution": 0,
                "max_lake_rows_per_execution": 1000,
                "max_wall_time_seconds": 60,
                "max_cpu_cores": 1,
                "max_memory_mib": 256,
                "max_strategy_lineages": 1,
                "allowed_strategy_classes": ("momentum",),
                "allowed_activation_modes": ("paper",),
            },
            "runtime_profile": {
                "profile_id": "local",
                "version": "1.0.0",
                "mode": "local",
            },
            "platform_bundle": {
                "bundle_id": "test",
                "version": "1.0.0",
                "bundle_sha256": "a" * 64,
                "policy_sha256": "b" * 64,
            },
            "repository_identity": {
                "provider": "local",
                "repository_id": workspace_id,
                "default_branch": "main",
            },
            "secret_references": {},
        }
    )


def _document(
    identity: str = "h-shared", body: str = "Reasoning"
) -> HypothesisDocument:
    return HypothesisDocument(
        HypothesisFrontMatter(
            hypothesis_id=identity,
            status=HypothesisStatus.ACTIVE,
            claim="Momentum persists",
            disagreement="Market underprices persistence",
            risk_factor="Momentum",
            horizon="daily",
            universe=("SPY",),
        ),
        body,
    )


@pytest.fixture
def provider(tmp_path: Path) -> LocalWorkspaceProvider:
    repositories = LocalWorkspaceProvider(tmp_path / "repositories")
    repositories.create(_manifest("workspace-one"))
    repositories.create(_manifest("workspace-two"))
    return repositories


def test_memory_is_workspace_keyed_and_requires_explicit_seed_context() -> None:
    store = InMemoryHypothesisStore()
    one, two = _document(), _document(body="Other workspace")
    store.save(one, workspace_id="workspace-one")
    store.save(two, workspace_id="workspace-two")
    assert store.load(one.hypothesis_id, workspace_id="workspace-one") == one
    assert store.load(two.hypothesis_id, workspace_id="workspace-two") == two
    assert store.list(workspace_id="workspace-three") == ()
    with pytest.raises(HypothesisArtifactError, match="not found"):
        store.load(one.hypothesis_id, workspace_id="workspace-three")
    with pytest.raises(HypothesisArtifactError, match="seed documents"):
        InMemoryHypothesisStore((one,))
    seeded = InMemoryHypothesisStore((one,), workspace_id="workspace-one")
    assert seeded.list(workspace_id="workspace-one") == (one.hypothesis_id,)


@pytest.mark.slow
def test_local_restart_two_instances_isolation_and_projection(
    provider: LocalWorkspaceProvider,
) -> None:
    first = WorkspaceHypothesisStore(provider)
    one, two = _document(), _document(body="Other workspace")
    relative = first.save(one, workspace_id="workspace-one")
    assert relative == f"hypotheses/h-shared/{one.content_sha256}.md"
    first.save(two, workspace_id="workspace-two")
    restarted = WorkspaceHypothesisStore(LocalWorkspaceProvider(provider.root))
    assert restarted.load(one.hypothesis_id, workspace_id="workspace-one") == one
    assert restarted.load(two.hypothesis_id, workspace_id="workspace-two") == two
    assert first.list(workspace_id="workspace-one") == (one.hypothesis_id,)
    root = provider.open("workspace-one").root
    coordinator = GitCoordinator(root)
    original_head = coordinator.head()
    assert restarted.save(one, workspace_id="workspace-one") == relative
    assert coordinator.head() == original_head
    projected = one.projected(status=HypothesisStatus.REFUTED)
    restarted.save(projected, workspace_id="workspace-one")
    assert first.load(one.hypothesis_id, workspace_id="workspace-one") == projected
    assert (
        provider.resolve_path("workspace-one", relative).read_bytes() == one.to_bytes()
    )
    assert first.load(two.hypothesis_id, workspace_id="workspace-two") == two


@pytest.mark.slow
def test_tamper_is_rejected_on_load_and_save(provider: LocalWorkspaceProvider) -> None:
    store = WorkspaceHypothesisStore(provider)
    document = _document()
    store.save(document, workspace_id="workspace-one")
    path = store.path_for(document.hypothesis_id, workspace_id="workspace-one")
    path.write_bytes(_document(body="Tampered").to_bytes())
    for operation in (
        lambda: store.load(document.hypothesis_id, workspace_id="workspace-one"),
        lambda: store.save(document, workspace_id="workspace-one"),
    ):
        with pytest.raises(HypothesisArtifactError, match="content hash"):
            operation()
    assert path.read_bytes() == _document(body="Tampered").to_bytes()


@pytest.mark.slow
def test_manifest_tamper_is_rejected(provider: LocalWorkspaceProvider) -> None:
    store = WorkspaceHypothesisStore(provider)
    document = _document()
    store.save(document, workspace_id="workspace-one")
    path = provider.resolve_path("workspace-one", ".thesis/hypotheses/h-shared.json")
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(HypothesisArtifactError, match="committed content"):
        store.load(document.hypothesis_id, workspace_id="workspace-one")


@pytest.mark.slow
def test_legacy_reads_only_selected_verified_workspace(
    provider: LocalWorkspaceProvider,
) -> None:
    document = _document()
    (provider.root / "hypotheses").mkdir()
    (provider.root / document.relative_path).write_bytes(document.to_bytes())
    store = WorkspaceHypothesisStore(provider)
    assert store.list(workspace_id="workspace-one") == ()
    with pytest.raises(HypothesisArtifactError, match="not found"):
        store.load(document.hypothesis_id, workspace_id="workspace-one")
    legacy = provider.resolve_path("workspace-one", document.relative_path)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(document.to_bytes())
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == document
    assert store.list(workspace_id="workspace-one") == (document.hypothesis_id,)
    with pytest.raises(HypothesisArtifactError, match="not found"):
        store.load(document.hypothesis_id, workspace_id="workspace-two")
    store.save(document, workspace_id="workspace-one")
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == document


@pytest.mark.slow
def test_slug_collision_rejected_without_overwrite(
    provider: LocalWorkspaceProvider,
) -> None:
    store = WorkspaceHypothesisStore(provider)
    first, second = _document("h_shared"), _document("h-shared")
    store.save(first, workspace_id="workspace-one")
    head = GitCoordinator(provider.open("workspace-one").root).head()
    with pytest.raises(HypothesisArtifactError, match="manifest"):
        store.save(second, workspace_id="workspace-one")
    assert GitCoordinator(provider.open("workspace-one").root).head() == head
    assert store.load(first.hypothesis_id, workspace_id="workspace-one") == first


class _RemoteRepositories(LocalWorkspaceProvider):
    """Fake remote observes committed artifacts and can reject a push."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.published: list[str] = []
        self.reject = True

    def publish(self, workspace_id: str) -> str:
        head = super().publish(workspace_id)
        self.published.append(head)
        if self.reject:
            raise RuntimeError("remote history conflict")
        return head


@pytest.mark.slow
def test_remote_failure_retains_commit_and_retry_is_idempotent(tmp_path: Path) -> None:
    provider = _RemoteRepositories(tmp_path / "remote")
    provider.create(_manifest("workspace-one"))
    store = WorkspaceHypothesisStore(provider)
    document = _document()
    with pytest.raises(RuntimeError, match="history conflict"):
        store.save(document, workspace_id="workspace-one")
    head = GitCoordinator(provider.open("workspace-one").root).head()
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == document
    provider.reject = False
    WorkspaceHypothesisStore(provider).save(document, workspace_id="workspace-one")
    assert provider.published == [head, head]
    assert not tuple(
        provider.resolve_path("workspace-one", "hypotheses/h-shared").glob(
            ".hypothesis-*"
        )
    )


@pytest.mark.parametrize(
    "workspace_id",
    [
        "",
        "../workspace-one",
        "workspace-one/../workspace-two",
        "WORKSPACE",
        " workspace-one",
    ],
)
def test_workspace_id_validation(workspace_id: str) -> None:
    store = InMemoryHypothesisStore()
    with pytest.raises(HypothesisArtifactError, match="workspace_id"):
        store.save(_document(), workspace_id=workspace_id)
    with pytest.raises(HypothesisArtifactError, match="workspace_id"):
        store.load("h-shared", workspace_id=workspace_id)
    with pytest.raises(HypothesisArtifactError, match="workspace_id"):
        store.list(workspace_id=workspace_id)


def test_constructor_rejects_raw_root(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="WorkspaceRepositoryProvider"):
        WorkspaceHypothesisStore(tmp_path)  # ty: ignore[invalid-argument-type]


@pytest.mark.slow
def test_missing_workspace_is_not_created(provider: LocalWorkspaceProvider) -> None:
    store = WorkspaceHypothesisStore(provider)
    with pytest.raises(WorkspaceNotFoundError):
        store.save(_document(), workspace_id="workspace-missing")
    assert not (provider.root / "workspace-missing").exists()


@pytest.mark.slow
def test_symlink_escape_is_rejected(
    provider: LocalWorkspaceProvider, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = provider.open("workspace-one").root / "hypotheses"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks require OS permission")
    with pytest.raises(WorkspacePathError):
        WorkspaceHypothesisStore(provider).save(
            _document(), workspace_id="workspace-one"
        )
    assert not tuple(outside.iterdir())


def test_bypassed_model_validation_is_rechecked_at_save() -> None:
    document = _document()
    invalid = HypothesisDocument(document.front_matter.model_copy(update={"claim": ""}))
    with pytest.raises(HypothesisArtifactError, match="front matter"):
        InMemoryHypothesisStore().save(invalid, workspace_id="workspace-one")


@pytest.mark.slow
def test_atomic_write_failure_cleans_temporary_and_retries(
    provider: LocalWorkspaceProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkspaceHypothesisStore(provider)
    document = _document()
    head = GitCoordinator(provider.open("workspace-one").root).head()

    def fail_replace(*args: object) -> None:
        raise OSError("interrupted replace")

    with monkeypatch.context() as patch:
        patch.setattr(hypothesis.os, "replace", fail_replace)
        with pytest.raises(OSError, match="interrupted replace"):
            store.save(document, workspace_id="workspace-one")
    directory = provider.resolve_path("workspace-one", "hypotheses/h-shared")
    assert not tuple(directory.iterdir())
    assert GitCoordinator(provider.open("workspace-one").root).head() == head
    store.save(document, workspace_id="workspace-one")
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == document


@pytest.mark.slow
def test_clone_without_local_journal_can_load_and_retry(
    provider: LocalWorkspaceProvider, tmp_path: Path
) -> None:
    document = _document()
    WorkspaceHypothesisStore(provider).save(document, workspace_id="workspace-one")
    cloned = LocalWorkspaceProvider(tmp_path / "cloned")
    subprocess.run(
        (
            "git",
            "-c",
            "core.autocrlf=false",
            "clone",
            "--quiet",
            "--",
            str(provider.open("workspace-one").root),
            str(cloned.root / "workspace-one"),
        ),
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert not cloned.resolve_path(
        "workspace-one", ".thesis/hypotheses-journal.sqlite3"
    ).exists()
    store = WorkspaceHypothesisStore(cloned)
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == document
    head = GitCoordinator(cloned.open("workspace-one").root).head()
    store.save(document, workspace_id="workspace-one")
    assert GitCoordinator(cloned.open("workspace-one").root).head() == head
    projected = document.projected(status=HypothesisStatus.SUPPORTED)
    store.save(projected, workspace_id="workspace-one")
    assert store.load(document.hypothesis_id, workspace_id="workspace-one") == projected
