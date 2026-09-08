"""Global Knowledge Library: corpus, metadata, and link layers.

The canonical corpus is Git-versioned Markdown under ``knowledge/``.  This
package holds the corpus-facing structures:

- :mod:`thesis.knowledge.metadata` — strict front-matter parsing and
  canonical rendering (:class:`PageMetadata`, :func:`parse_front_matter`,
  :func:`render_front_matter`);
- :mod:`thesis.knowledge.links` — the link and relationship graph
  (:class:`LinkGraph`) with backlink, supersession, and contradiction
  accessors; and
- :mod:`thesis.knowledge.corpus` — corpus loading with Git revision capture
  (:class:`KnowledgeCorpus`, :class:`CorpusSnapshot`).

The sibling index and search modules are the rebuildable, derived index
layer and are deliberately independent of these structures.
"""

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

__all__ = [
    "CorpusError",
    "CorpusPage",
    "CorpusSnapshot",
    "KnowledgeCorpus",
    "LinkError",
    "LinkGraph",
    "MetadataError",
    "PageMetadata",
    "PageNode",
    "PageRelationship",
    "RelationshipType",
    "TrustState",
    "TrustTier",
    "normalize_corpus_path",
    "parse_front_matter",
    "relationship_type",
    "render_front_matter",
]
