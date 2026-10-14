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
    "from-market-idea-to-trading-construction",
    "investigating-representations-and-episodes",
    "developing-a-candidate-through-evidence",
    "single-security-price-regularity-tests",
    "roll-bid-ask-bounce",
    "robust-autocorrelation-inference",
    "tradability-break-even-and-oos",
}
TITLES = {
    "from-market-idea-to-trading-construction": "From market idea to trading construction",
    "investigating-representations-and-episodes": "Investigating representations through episodes",
    "developing-a-candidate-through-evidence": "Developing a candidate through evidence",
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


def test_construction_questions_retrieve_readable_discovery_cards(
    tmp_path: Path,
) -> None:
    service = KnowledgeService(
        cast(WorkspaceRepositoryProvider, Repositories(tmp_path / "workspaces")),
        LIBRARY,
    )
    questions = {
        "market idea trading construction": "from-market-idea-to-trading-construction",
        "trend detector confirmation episodes remaining duration": (
            "investigating-representations-and-episodes"
        ),
        "research loop operator steering construction": (
            "developing-a-candidate-through-evidence"
        ),
    }
    for query, page_id in questions.items():
        results = hits(
            service.search(
                "alpha",
                query,
                scope="shared",
                limit=5,
                allowed_collections=("discovery",),
            )
        )
        selected = next((hit for hit in results if hit["page_id"] == page_id), None)
        assert selected is not None, (query, results)
        read = service.read(
            "alpha",
            cast(str, selected["reference"]),
            allowed_scope="shared",
            max_chars=6000,
            allowed_collections=("discovery",),
        )
        assert read["page_id"] == page_id
        assert read["truncated"] is False
        assert read["data_only"] is True
        assert read["content"] == KnowledgeCorpus(LIBRARY).load().page(page_id).body
