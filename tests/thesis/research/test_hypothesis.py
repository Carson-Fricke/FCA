"""Workspace isolation and real-Git hypothesis restart/publication contracts."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from thesis.persistence.git import GitCoordinator
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
def test_local_restart_idempotency_and_immutable_projection(
    provider: LocalWorkspaceProvider,
) -> None:
    first = WorkspaceHypothesisStore(provider)
    one = _document()
    relative = first.save(one, workspace_id="workspace-one")
    assert relative == f"hypotheses/h-shared/{one.content_sha256}.md"
    assert first.path_for(one.hypothesis_id, workspace_id="workspace-one") == (
        provider.resolve_path("workspace-one", relative)
    )
    restarted = WorkspaceHypothesisStore(LocalWorkspaceProvider(provider.root))
    assert restarted.load(one.hypothesis_id, workspace_id="workspace-one") == one
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


def test_bypassed_model_validation_is_rechecked_at_save() -> None:
    document = _document()
    invalid = HypothesisDocument(document.front_matter.model_copy(update={"claim": ""}))
    with pytest.raises(HypothesisArtifactError, match="front matter"):
        InMemoryHypothesisStore().save(invalid, workspace_id="workspace-one")
