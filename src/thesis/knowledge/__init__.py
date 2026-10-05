"""Global Knowledge Library: corpus and trust layers.

The shared corpus is Markdown under ``thesis/knowledge/library/``; each
workspace owns a separate ``knowledge/`` corpus. This
package holds the corpus-facing structures:

- :mod:`thesis.knowledge.metadata` — strict front-matter parsing and
  canonical rendering (:class:`PageMetadata`, :func:`parse_front_matter`,
  :func:`render_front_matter`);
- :mod:`thesis.knowledge.links` — the link and relationship graph
  (:class:`LinkGraph`) with backlink, supersession, and contradiction
  accessors;
- :mod:`thesis.knowledge.corpus` — corpus loading with Git revision capture
  (:class:`KnowledgeCorpus`, :class:`CorpusSnapshot`);
- :mod:`thesis.knowledge.trust` — workspace-local trust overlays
  (:class:`WorkspaceTrustOverlay`) that promote pages by interpretation
  only, never mutating global corpus content.

The sibling index and search modules are the rebuildable, derived index
layer and are deliberately independent of these structures.
"""

from thesis.knowledge.corpus import (
    CorpusError,
    CorpusPage,
    CorpusSnapshot,
    KnowledgeCorpus,
)
from thesis.knowledge.embeddings import (
    MODEL_DIMENSIONS,
    MODEL_ID,
    EmbeddingModelError,
    FallbackEmbedder,
    MiniLmEmbedder,
    default_embedder,
)
from thesis.knowledge.links import (
    LinkError,
    LinkGraph,
    PageNode,
    normalize_corpus_path,
    relationship_type,
)
from thesis.knowledge.metadata import (
    MetadataError,
    PageMetadata,
    PageRelationship,
    RelationshipType,
    TrustState,
    TrustTier,
    parse_front_matter,
    render_front_matter,
)
from thesis.knowledge.service import KnowledgeScope, KnowledgeService
from thesis.knowledge.trust import (
    DEFAULT_READ_ACCESS,
    PROMOTABLE_TIERS,
    PROMOTION_IS_INTERPRETATION_ONLY,
    TIER_PRECEDENCE,
    EffectiveTier,
    PromotionDecision,
    TrustDecisionError,
    WorkspaceTrustOverlay,
    default_read_access,
    resolve_effective_tier,
)

__all__ = [
    "CorpusError",
    "CorpusPage",
    "CorpusSnapshot",
    "DEFAULT_READ_ACCESS",
    "EffectiveTier",
    "EmbeddingModelError",
    "FallbackEmbedder",
    "KnowledgeCorpus",
    "KnowledgeScope",
    "KnowledgeService",
    "MiniLmEmbedder",
    "MODEL_DIMENSIONS",
    "MODEL_ID",
    "LinkError",
    "LinkGraph",
    "MetadataError",
    "PageMetadata",
    "PageNode",
    "PageRelationship",
    "PROMOTABLE_TIERS",
    "PROMOTION_IS_INTERPRETATION_ONLY",
    "PromotionDecision",
    "RelationshipType",
    "TIER_PRECEDENCE",
    "TrustDecisionError",
    "TrustState",
    "TrustTier",
    "WorkspaceTrustOverlay",
    "default_embedder",
    "default_read_access",
    "normalize_corpus_path",
    "parse_front_matter",
    "relationship_type",
    "render_front_matter",
    "resolve_effective_tier",
]
