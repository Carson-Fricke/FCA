"""Index/query retries never publish mixed vector spaces or source scores."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Barrier, Event
from typing import cast

import pytest

from thesis.knowledge.embeddings import FallbackEmbedder
from thesis.knowledge.index import (
    EmbeddingPort,
    KnowledgeIndexError,
    PageRecord,
    build_index,
    load_index,
)
from thesis.knowledge.metadata import PageMetadata, render_front_matter
from thesis.knowledge.search import (
    HashingEmbedder,
    KnowledgeSearch,
    KnowledgeSearchError,
    SearchQuery,
)
from thesis.knowledge.service import KnowledgeService
from thesis.workspaces.repository import WorkspaceRepositoryProvider


class Repositories:
    """Minimal source-path provider without importing agent runtime fixtures."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "alpha").mkdir(parents=True)

    def resolve_path(
        self, workspace_id: str, relative: str | Path, *, must_exist: bool = False
    ) -> Path:
        assert workspace_id == "alpha"
        assert relative == "knowledge"
        return self.root / workspace_id / relative


def write_page(root: Path, body: str = "Momentum evidence.") -> None:
    root.mkdir(parents=True, exist_ok=True)
    metadata = PageMetadata(
        page_id="sample",
        title="Sample",
        version=1,
        created=date(2026, 1, 1),
        retrieved=date(2026, 1, 1),
        source_url="https://example.org",
        source_content_hash="a" * 64,
    )
    (root / "sample.md").write_text(render_front_matter(metadata) + body)


class LateFailure:
    model_id = "semantic-stub"

    def __init__(self, failure_text: str) -> None:
        self.failure_text = failure_text
        self.calls: list[str] = []
        self.query_calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if text == self.failure_text:
            self.query_calls += 1
            if self.query_calls < 2:
                return [1.0] + [0.0] * 383
            raise RuntimeError("late query inference failure")
        if self.failure_text == "workspace-query":
            return [1.0] + [0.0] * 383
        if self.failure_text in text:
            raise RuntimeError("late inference failure")
        return [1.0] + [0.0] * 383


def pages() -> list[PageRecord]:
    first = PageRecord(
        page_id="first",
        path="first.md",
        revision="revision",
        version=1,
        title="First",
        body="momentum",
        created=date(2026, 1, 1),
        retrieved=date(2026, 1, 1),
        source_url="https://example.org",
        source_content_hash="a" * 64,
    )
    return [first, replace(first, page_id="second", title="Second")]


def test_late_page_failure_rebuilds_all_index_vectors() -> None:
    primary = LateFailure("Second")
    fallback = HashingEmbedder()
    backend = FallbackEmbedder(primary, fallback)
    index = build_index(pages(), backend, "revision")
    expected = build_index(pages(), fallback, "revision")
    assert index == expected
    assert load_index(index.serialize()) == expected
    assert {len(entry.embedding) for entry in index.entries} == {256}
    assert len(primary.calls) == 2


@pytest.mark.parametrize("failure_text", ["late-page", "workspace-query"])
def test_service_retries_entire_multi_source_operation(
    tmp_path: Path, failure_text: str
) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    write_page(shared, body="workspace-query evidence")
    write_page(
        repositories.root / "alpha" / "knowledge",
        body="late-page workspace-query evidence",
    )
    fallback = HashingEmbedder()
    primary = LateFailure(failure_text)
    provider = cast(WorkspaceRepositoryProvider, repositories)
    service = KnowledgeService(provider, shared, FallbackEmbedder(primary, fallback))
    expected = KnowledgeService(provider, shared, fallback).search(
        "alpha", "workspace-query"
    )
    assert service.search("alpha", "workspace-query") == expected
    assert service.search("alpha", "workspace-query") == expected


def test_standalone_search_rejects_fallback_against_semantic_index() -> None:
    primary = LateFailure("query")
    backend = FallbackEmbedder(primary, HashingEmbedder())
    index = build_index(pages(), backend, "revision")
    with pytest.raises(KnowledgeSearchError, match="model does not match"):
        search = KnowledgeSearch(index, backend)
        search.search(SearchQuery(text="query"))
        search.search(SearchQuery(text="query"))


def test_search_rejects_different_model_with_same_dimensions() -> None:
    primary = LateFailure("never")
    index = build_index(pages(), primary, "revision")
    primary.model_id = "different-semantic-model"
    with pytest.raises(KnowledgeSearchError, match="model does not match"):
        KnowledgeSearch(index, primary).search(SearchQuery(text="momentum"))


def test_index_rejects_mixed_dimensions() -> None:
    index = build_index(pages(), HashingEmbedder(), "revision")
    with pytest.raises(KnowledgeIndexError, match="mixed embedding dimensions"):
        replace(
            index,
            entries=(index.entries[0], replace(index.entries[1], embedding=(1.0,))),
        )


def test_concurrent_failure_does_not_switch_successful_operation() -> None:
    rendezvous = Barrier(2)
    failed = Event()

    class ConcurrentPrimary:
        model_id = "semantic-stub"

        def embed(self, text: str) -> list[float]:
            if text in ("success-start", "failure-start"):
                rendezvous.wait(timeout=5)
            if text == "failure-start":
                raise RuntimeError("offline")
            if text == "success-end":
                assert failed.wait(5)
            return [1.0] * 384

    backend = FallbackEmbedder(ConcurrentPrimary(), HashingEmbedder())

    def operation(start: str) -> tuple[str, list[int]]:
        def run(bound: EmbeddingPort) -> tuple[str, list[int]]:
            widths = [len(bound.embed(start)), len(bound.embed("success-end"))]
            return str(getattr(bound, "model_id", "injected-embedder")), widths

        result = backend.run_operation(run)
        if start == "failure-start":
            failed.set()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        success = pool.submit(operation, "success-start")
        failure = pool.submit(operation, "failure-start")
        assert failure.result(timeout=5) == ("hashed-bag-of-words", [256, 256])
        assert success.result(timeout=5) == ("semantic-stub", [384, 384])
    assert backend.using_fallback
