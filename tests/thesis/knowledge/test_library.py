"""The authored method pages parse, validate, and are retrievable."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from tests.thesis.knowledge.test_service import Repositories, hits

from thesis.knowledge.corpus import KnowledgeCorpus
from thesis.knowledge.service import KnowledgeService
from thesis.workspaces.repository import WorkspaceRepositoryProvider

LIBRARY = Path("src/thesis/knowledge/library")
PAGE_IDS = {
    "single-security-price-regularity-tests",
    "roll-bid-ask-bounce",
    "robust-autocorrelation-inference",
    "tradability-break-even-and-oos",
}
TITLES = {
    "single-security-price-regularity-tests": (
        "Single-security price-regularity tests"
    ),
    "roll-bid-ask-bounce": "Roll bid-ask bounce",
    "robust-autocorrelation-inference": (
        "Robust autocorrelation and variance-ratio inference"
    ),
    "tradability-break-even-and-oos": ("Tradability, break-even, and out-of-sample"),
}

#: Paraphrased questions the semantic model must route to the right page.
SEMANTIC_QUERIES = {
    "short-horizon mean reversion might just be a microstructure artefact": (
        "roll-bid-ask-bounce"
    ),
    "correct standard errors for autocorrelation of intraday returns under "
    "volatility clustering": "robust-autocorrelation-inference",
    "when is a statistically significant edge actually tradable after costs": (
        "tradability-break-even-and-oos"
    ),
    "how do I choose a statistical test for a regularity in one stock's price": (
        "single-security-price-regularity-tests"
    ),
    "the spread eats the profit from signal-based trading": (
        "spread-and-execution-cost-estimation"
    ),
    "does one shuffle give a p-value": "robust-autocorrelation-inference",
}


def _cached_model_available() -> bool:
    configured = (os.environ.get("THESIS_EMBEDDING_DIR") or "").strip()
    base = (
        Path(configured)
        if configured
        else Path.home() / ".cache" / "thesis" / "embeddings"
    )
    return (base / "onnx" / "model_quantized.onnx").is_file()


def test_authored_method_pages_are_valid_corpus_pages() -> None:
    snapshot = KnowledgeCorpus(LIBRARY).load()
    assert snapshot.is_unique_ids
    assert snapshot.validate() == []
    assert PAGE_IDS.issubset(snapshot.pages)
    for page_id in PAGE_IDS:
        page = snapshot.pages[page_id]
        assert page.metadata.title == TITLES[page_id]
        assert page.metadata.trust_tier.value == "curated"
        assert page.metadata.trust_state.value == "published"
        assert page.metadata.source_content_hash


def test_page_titles_retrieve_themselves_with_the_offline_baseline(
    tmp_path: Path,
) -> None:
    service = KnowledgeService(
        cast(WorkspaceRepositoryProvider, Repositories(tmp_path / "workspaces")),
        LIBRARY,
    )
    for page_id, title in TITLES.items():
        result = service.search("alpha", title, scope="shared", limit=6)
        ranked = [cast(str, hit["page_id"]) for hit in hits(result)]
        assert ranked, title
        assert page_id in ranked[:5], (title, ranked)
