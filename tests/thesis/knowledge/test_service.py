"""Knowledge retrieval preserves source, byte identity and workspace confinement."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.knowledge_tools import knowledge_registered_tools
from thesis.agent.runtime.session import SessionToolRegistry
from thesis.knowledge.corpus import CorpusError, KnowledgeCorpus
from thesis.knowledge.metadata import (
    PageMetadata,
    PageRelationship,
    RelationshipType,
    TrustState,
    TrustTier,
    render_front_matter,
)
from thesis.knowledge.service import (
    EXCERPT_CHARS,
    KnowledgeService,
    effective_sources,
    validate_scope,
)
from thesis.workspaces.local import LocalWorkspace, WorkspacePathError
from thesis.workspaces.manifest import WorkspaceManifest
from thesis.workspaces.repository import WorkspaceRepositoryProvider


class Repositories:
    """Gitless provider double delegates containment to the real workspace code."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str | Path, bool]] = []
        for workspace_id in ("alpha", "bravo"):
            (root / workspace_id).mkdir(parents=True)

    def resolve_path(
        self, workspace_id: str, relative: str | Path = ".", *, must_exist: bool = False
    ) -> Path:
        self.calls.append((workspace_id, relative, must_exist))
        if workspace_id not in ("alpha", "bravo"):
            raise WorkspacePathError("unknown workspace")
        workspace = LocalWorkspace(
            (self.root / workspace_id).resolve(), cast(WorkspaceManifest, None)
        )
        return workspace.resolve_path(relative, must_exist=must_exist)


def write_page(
    root: Path,
    page_id: str = "sample",
    body: str = "Momentum evidence.",
    *,
    path: str = "sample.md",
    links: tuple[str, ...] = (),
    relationships: tuple[PageRelationship, ...] = (),
    collections: tuple[str, ...] = (),
) -> Path:
    metadata = PageMetadata(
        page_id=page_id,
        title=page_id,
        version=3,
        created=date(2026, 1, 1),
        retrieved=date(2026, 2, 1),
        source_url="https://example.org/research",
        source_publisher="Example Research",
        source_content_hash="a" * 64,
        tags=(),
        collections=collections,
        links=links,
        relationships=relationships,
        trust_tier=TrustTier.CURATED,
        trust_state=TrustState.PUBLISHED,
    )
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_front_matter(metadata) + body, encoding="utf-8")
    return destination


@pytest.fixture
def environment(tmp_path: Path) -> tuple[KnowledgeService, Repositories, Path]:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    shared.mkdir()
    service = KnowledgeService(cast(WorkspaceRepositoryProvider, repositories), shared)
    return service, repositories, shared


def hits(result: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], result["results"])


def test_gitless_snapshot_tracks_actual_bytes(tmp_path: Path) -> None:
    page = write_page(tmp_path)
    first = KnowledgeCorpus(tmp_path).load()
    assert first.revision.startswith("sha256:")
    assert first.revision == KnowledgeCorpus(tmp_path).load().revision
    page.write_bytes(page.read_bytes().replace(b"Momentum", b"Reversal"))
    assert first.revision != KnowledgeCorpus(tmp_path).load().revision


def test_both_sources_keep_same_ids_and_full_provenance(environment) -> None:
    service, repositories, shared = environment
    write_page(shared, body="Momentum shared research.")
    write_page(
        repositories.root / "alpha" / "knowledge", body="Momentum private research."
    )
    results = hits(service.search("alpha", "momentum"))
    assert {hit["source"] for hit in results} == {"shared", "workspace"}
    assert len({hit["reference"] for hit in results}) == 2
    for hit in results:
        read = service.read("alpha", cast(str, hit["reference"]))
        assert read["version"] == 3
        assert read["data_only"] is True
        assert hit["excerpt"] == read["content"]
        assert (
            hit["content_hash"]
            == hashlib.sha256(cast(str, read["content"]).encode()).hexdigest()
        )
        provenance = cast(dict[str, object], read["provenance"])
        assert provenance["source_content_hash"] == "a" * 64
        assert provenance["trust_tier"] == "curated"
    assert ("alpha", "knowledge", False) in repositories.calls


@pytest.mark.parametrize("scope", ["shared", "workspace"])
def test_scoped_search_and_read(environment, scope) -> None:
    service, repositories, shared = environment
    write_page(shared)
    write_page(repositories.root / "alpha" / "knowledge")
    results = hits(service.search("alpha", "momentum", scope=scope))
    assert [hit["source"] for hit in results] == [scope]
    other = "workspace" if scope == "shared" else "shared"
    with pytest.raises(ValueError, match="forbidden"):
        service.read("alpha", results[0]["reference"], allowed_scope=other)
    assert len(hits(service.search("alpha", "momentum", allowed_scope=scope))) == 1
    with pytest.raises(ValueError, match="not allowed"):
        service.search("alpha", "momentum", scope=other, allowed_scope=scope)


def test_workspace_reference_cannot_cross_identical_workspaces(environment) -> None:
    service, repositories, _ = environment
    for workspace_id in ("alpha", "bravo"):
        write_page(repositories.root / workspace_id / "knowledge")
    first = hits(service.search("alpha", "momentum", scope="workspace"))[0]
    second = hits(service.search("bravo", "momentum", scope="workspace"))[0]
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["reference"] != second["reference"]
    with pytest.raises(ValueError, match="stale"):
        service.read("bravo", first["reference"])


def test_workspace_search_is_isolated(environment) -> None:
    service, repositories, _ = environment
    write_page(repositories.root / "alpha" / "knowledge", body="Momentum alpha-only.")
    assert hits(service.search("bravo", "momentum")) == []
    with pytest.raises(WorkspacePathError):
        service.search("../alpha", "momentum", scope="workspace")


def test_read_rejects_dirty_snapshot_and_paths(environment) -> None:
    service, _, shared = environment
    page = write_page(shared)
    reference = hits(service.search("alpha", "momentum"))[0]["reference"]
    page.write_bytes(page.read_bytes() + b" Changed without version bump.")
    with pytest.raises(ValueError, match="stale"):
        service.read("alpha", reference)
    for reference in ("sample", "sample.md", "../sample.md", str(page)):
        with pytest.raises(ValueError, match="reference"):
            service.read("alpha", reference)


def test_empty_and_missing_sources(environment) -> None:
    service, repositories, shared = environment
    assert hits(service.search("alpha", "momentum")) == []
    assert not (repositories.root / "alpha" / "knowledge").exists()
    shared.rmdir()
    assert hits(service.search("alpha", "momentum")) == []
    shared.write_text("not a directory")
    with pytest.raises(CorpusError, match="directory"):
        service.search("alpha", "momentum")


def test_path_links_and_typed_relations_expand_search(environment) -> None:
    service, _, shared = environment
    write_page(
        shared,
        "origin",
        "Momentum origin.",
        path="origin.md",
        links=("nested/target.md",),
        relationships=(
            PageRelationship(RelationshipType.SUPERSEDES, "nested/target.md"),
        ),
    )
    write_page(shared, "target", "Unrelated words.", path="nested/target.md")
    results = hits(service.search("alpha", "momentum", scope="shared"))
    target = next(hit for hit in results if hit["page_id"] == "target")
    assert "hyperlink" in cast(list[str], target["matched_via"])
    origin = next(hit for hit in results if hit["page_id"] == "origin")
    relation = cast(list[dict[str, object]], origin["relationships"])[0]
    assert relation["type"] == "supersedes"
    assert relation["target_page_id"] == "target"
    assert (
        service.read("alpha", cast(str, relation["reference"]))["page_id"] == "target"
    )


def test_bounded_actual_excerpts_and_read(environment) -> None:
    service, _, shared = environment
    body = "prefix " * 1000 + "momentum " + "suffix " * 1000
    write_page(shared, body=body)
    result = hits(service.search("alpha", "momentum"))[0]
    excerpt = cast(str, result["excerpt"])
    assert len(excerpt) == EXCERPT_CHARS
    assert "momentum" in excerpt
    assert excerpt in body
    assert result["truncated"] is True
    read = service.read("alpha", result["reference"], max_chars=50)
    assert read["content"] == body[:50]
    assert read["truncated"] is True
    assert read["total_chars"] == len(body)
    assert (
        service.search("alpha", "momentum")["retrieval_baseline"]
        == "hashed-bag-of-words"
    )


@pytest.mark.parametrize("value", [0, -1, True, 21, 2.5])
def test_invalid_limits(environment, value) -> None:
    service, _, _ = environment
    with pytest.raises(ValueError, match="limit"):
        service.search("alpha", "momentum", limit=value)


def symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")


@pytest.mark.parametrize("directory", [False, True])
def test_corpus_rejects_symlink_escape(
    environment, tmp_path: Path, directory: bool
) -> None:
    service, _, shared = environment
    outside = tmp_path / "outside"
    page = write_page(outside)
    symlink(
        shared / ("escaped" if directory else "escaped.md"),
        outside if directory else page,
        directory=directory,
    )
    with pytest.raises(CorpusError, match="escapes"):
        service.search("alpha", "momentum", scope="shared")


def test_workspace_knowledge_directory_escape(environment, tmp_path: Path) -> None:
    service, repositories, _ = environment
    outside = tmp_path / "outside"
    write_page(outside)
    symlink(repositories.root / "alpha" / "knowledge", outside, directory=True)
    with pytest.raises(WorkspacePathError, match="escapes"):
        service.search("alpha", "momentum", scope="workspace")


def test_duplicate_ids_within_one_root_fail(environment) -> None:
    service, _, shared = environment
    write_page(shared)
    write_page(shared, path="duplicate.md")
    with pytest.raises(CorpusError, match="duplicate"):
        service.search("alpha", "momentum", scope="shared")


def test_scope_helpers() -> None:
    assert validate_scope("workspace") == "workspace"
    assert effective_sources("both", "workspace") == ("workspace",)
    assert effective_sources("both", "shared") == ("shared",)
    with pytest.raises(ValueError):
        validate_scope("all")
    with pytest.raises(ValueError):
        effective_sources("shared", "workspace")


def test_tools_default_to_trusted_scope_and_reject_authority_arguments(
    environment,
) -> None:
    service, repositories, shared = environment
    write_page(shared)
    write_page(repositories.root / "alpha" / "knowledge")
    context = ToolContext("alpha", "chat", "conversation", "actor")
    tools = knowledge_registered_tools(service, context, allowed_scope="workspace")
    registry = SessionToolRegistry(tools)
    assert [tool.name for tool in tools] == ["search_knowledge", "read_knowledge"]
    schema = tools[0].input_schema
    properties = cast(dict[str, dict[str, object]], schema["properties"])
    assert properties["scope"]["default"] == "workspace"
    for arguments in ({"query": "momentum"}, {"query": "momentum", "scope": "both"}):
        result = registry.dispatch("search_knowledge", arguments)
        assert result.ok
        assert result.structured["sources"] == ["workspace"]
        reference = hits(dict(result.structured))[0]["reference"]
        assert registry.dispatch("read_knowledge", {"reference": reference}).ok
    rejected = registry.dispatch(
        "search_knowledge", {"query": "momentum", "scope": "shared"}
    )
    assert not rejected.ok
    assert rejected.failure is not None
    assert rejected.failure["kind"] == "knowledge"
    assert not registry.dispatch(
        "search_knowledge",
        {
            "query": "momentum",
            "workspace_id": "bravo",
        },
    ).ok
    assert not registry.dispatch(
        "read_knowledge", {"reference": str(shared / "sample.md")}
    ).ok


def test_injectable_embedder(environment) -> None:
    _, repositories, shared = environment
    write_page(shared)

    class Embedder:
        def embed(self, text: str) -> list[float]:
            return [1.0]

    service = KnowledgeService(
        cast(WorkspaceRepositoryProvider, repositories), shared, Embedder()
    )
    result = service.search("alpha", "momentum")
    assert result["retrieval_baseline"] == "injected-embedder"
    assert len(hits(result)) == 1
