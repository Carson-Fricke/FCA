"""Semantic embedder behavior, fallback, and search provenance wiring."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from typing import cast

import numpy as np
import pytest
from tests.thesis.knowledge.test_embedding_operations import Repositories, write_page

from thesis.knowledge import embeddings
from thesis.knowledge.embeddings import (
    MODEL_DIMENSIONS,
    MODEL_ID,
    FallbackEmbedder,
    MiniLmEmbedder,
    default_embedder,
)
from thesis.knowledge.search import HashingEmbedder
from thesis.knowledge.service import KnowledgeService
from thesis.workspaces.repository import WorkspaceRepositoryProvider


class _FakeInput:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEncoding:
    ids = [101, 7, 102]
    attention_mask = [1, 1, 0]
    type_ids = [0, 0, 0]


class _FakeTokenizer:
    def __init__(self) -> None:
        self.max_length: int | None = None

    def enable_truncation(self, max_length: int) -> None:
        self.max_length = max_length

    def encode(self, sequence: str) -> _FakeEncoding:
        assert sequence
        return _FakeEncoding()


class _FakeSession:
    def __init__(self) -> None:
        self.calls = 0

    def get_inputs(self) -> list[object]:
        return [_FakeInput("input_ids"), _FakeInput("attention_mask")]

    def get_outputs(self) -> list[object]:
        return [_FakeInput("last_hidden_state")]

    def run(self, output_names: object, input_feed: dict[str, object]) -> list[object]:
        self.calls += 1
        assert getattr(input_feed["input_ids"], "shape", None) == (1, 3)
        # Two attended tokens carry [3, 0] and [0, 4]; the third is masked.
        hidden = np.array([[[3.0, 0.0], [0.0, 4.0], [999.0, 999.0]]])
        return [hidden]


def _embedder(session: _FakeSession | None = None) -> MiniLmEmbedder:
    return MiniLmEmbedder(
        session=session or _FakeSession(),
        tokenizer=_FakeTokenizer(),
    )


def test_minilm_embedder_mean_pools_and_normalizes() -> None:
    embedder = _embedder()
    vector = embedder.embed("momentum in equities")
    assert len(vector) == 2
    norm = sum(value * value for value in vector) ** 0.5
    assert norm == pytest.approx(1.0)
    assert vector[0] == pytest.approx(3.0 / 5.0)
    assert vector[1] == pytest.approx(4.0 / 5.0)


def test_minilm_embedder_identity_and_empty_text() -> None:
    embedder = _embedder()
    assert embedder.model_id == MODEL_ID
    assert embedder.dimensions == MODEL_DIMENSIONS
    assert embedder.embed("   ") == [0.0] * MODEL_DIMENSIONS


def test_minilm_embedder_memoizes_repeated_text() -> None:
    session = _FakeSession()
    embedder = _embedder(session)
    embedder.embed("variance ratio")
    embedder.embed("variance ratio")
    assert session.calls == 1


def test_fallback_embedder_degrades_to_lexical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Broken:
        model_id = MODEL_ID

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("model file missing")

    fallback = FallbackEmbedder(Broken(), HashingEmbedder())
    assert fallback.model_id == MODEL_ID
    assert fallback.embed("momentum")
    assert fallback.using_fallback
    assert fallback.model_id == "hashed-bag-of-words"
    assert "falling back" in capsys.readouterr().err


def test_default_embedder_is_minilm_and_respects_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THESIS_EMBEDDING_DISABLE", raising=False)
    embedder = default_embedder()
    assert isinstance(embedder, FallbackEmbedder)
    assert embedder.model_id == MODEL_ID
    monkeypatch.setenv("THESIS_EMBEDDING_DISABLE", "1")
    assert isinstance(default_embedder(), HashingEmbedder)


def test_search_reports_the_embedding_model(tmp_path: Path) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    write_page(shared)
    service = KnowledgeService(
        cast(WorkspaceRepositoryProvider, repositories),
        shared,
        _embedder(),
    )
    result = service.search("alpha", "momentum")
    assert result["retrieval_baseline"] == MODEL_ID
    assert result["results"]


@pytest.mark.parametrize("fails", [False, True])
def test_lazy_initialization_is_single_flight(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    entered = Event()
    release = Event()
    calls = 0

    def load(**kwargs: object) -> MiniLmEmbedder:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        if fails:
            raise RuntimeError("offline")
        return _embedder()

    monkeypatch.setattr(MiniLmEmbedder, "load", load)
    lazy = embeddings._LazyMiniLmEmbedder()
    backend = FallbackEmbedder(lazy, HashingEmbedder())
    start = Barrier(8)

    def embed() -> list[float]:
        start.wait(timeout=5)
        return backend.embed("momentum")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(embed) for _ in range(8)]
        assert entered.wait(5)
        release.set()
        vectors = [future.result(timeout=5) for future in futures]
    assert calls == 1
    assert len({tuple(vector) for vector in vectors}) == 1
    assert backend.using_fallback == fails
    assert (lazy._inner is None) == fails


def test_corrupt_download_and_unpinned_path_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(url: str, path: Path, digest: str) -> None:
        path.write_bytes(b"not the model")

    monkeypatch.setattr(embeddings, "_http_download", download)
    with pytest.raises(embeddings.EmbeddingModelError, match="checksum"):
        embeddings._ensure_file(tmp_path, "tokenizer.json")
    with pytest.raises(embeddings.EmbeddingModelError, match="unsupported"):
        embeddings._ensure_file(tmp_path, "../untrusted.onnx")


def test_inference_failure_does_not_publish_cache_entry() -> None:
    class FailingOnce(_FakeSession):
        def run(
            self, output_names: object, input_feed: dict[str, object]
        ) -> list[object]:
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("transient inference failure")
            return super().run(output_names, input_feed)

    session = FailingOnce()
    backend = _embedder(session)
    with pytest.raises(RuntimeError, match="transient"):
        backend.embed("momentum")
    assert not backend._cache
    assert backend.embed("momentum")[0] == pytest.approx(0.6)
    assert session.calls == 2
