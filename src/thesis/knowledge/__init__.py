"""Global Knowledge Library: corpus, trust, and projection layers.

The canonical corpus is Git-versioned Markdown under ``knowledge/``.  This
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
  only, never mutating global corpus content; and
- :mod:`thesis.knowledge.cleo_projection` — the optional, rebuildable CLEO
  initialization projection (:func:`build_projection`,
  :class:`ProjectionManifest`): a compact citation manifest, never a second
  canonical copy and never a CLEO Nexus/Brain load.

The sibling index and search modules are the rebuildable, derived index
layer and are deliberately independent of these structures.
"""

from thesis.knowledge.cleo_projection import (
    EXCERPT_CHARS,
    NON_CANONICAL_GUARD,
    PROJECTION_IS_NOT_CANONICAL,
    ProjectionCorpus,
    ProjectionError,
    ProjectionManifest,
    ProjectionPage,
    ProjectionRecord,
    build_projection,
    rebuild,
)
from thesis.knowledge.corpus import (
    CorpusError,
    CorpusPage,
    CorpusSnapshot,
    KnowledgeCorpus,
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
    "EXCERPT_CHARS",
    "EffectiveTier",
    "KnowledgeCorpus",
    "LinkError",
    "LinkGraph",
    "MetadataError",
    "NON_CANONICAL_GUARD",
    "PageMetadata",
    "PageNode",
    "PageRelationship",
    "PROJECTION_IS_NOT_CANONICAL",
    "PROMOTABLE_TIERS",
    "PROMOTION_IS_INTERPRETATION_ONLY",
    "ProjectionCorpus",
    "ProjectionError",
    "ProjectionManifest",
    "ProjectionPage",
    "ProjectionRecord",
    "PromotionDecision",
    "RelationshipType",
    "TIER_PRECEDENCE",
    "TrustDecisionError",
    "TrustState",
    "TrustTier",
    "WorkspaceTrustOverlay",
    "build_projection",
    "default_read_access",
    "normalize_corpus_path",
    "parse_front_matter",
    "rebuild",
    "relationship_type",
    "render_front_matter",
    "resolve_effective_tier",
]
