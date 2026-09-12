"""Research-domain records for the Thesis vertical.

The first capability in this package is the append-only strategy revision
lineage (:mod:`thesis.research.lineage`) that records which run and revision
descended from which, with the rationale and the experiment-contract
transition for every material adaptation.
"""

from thesis.research.hypothesis import (
    FALSIFIER_CONDITIONS,
    FRONT_MATTER_DELIMITER,
    HYPOTHESIS_DIRECTORY,
    SCHEMA_VERSION,
    HypothesisArtifactError,
    HypothesisDocument,
    HypothesisFalsifier,
    HypothesisFrontMatter,
    HypothesisLinks,
    HypothesisStatus,
    HypothesisStore,
    InMemoryHypothesisStore,
    WorkspaceHypothesisStore,
    parse_hypothesis,
    render_hypothesis,
    slugify,
)
from thesis.research.lineage import (
    LineageError,
    RevisionLineage,
    RevisionNode,
    RevisionOutcome,
    RunIdentity,
)

__all__ = [
    "FALSIFIER_CONDITIONS",
    "FRONT_MATTER_DELIMITER",
    "HYPOTHESIS_DIRECTORY",
    "SCHEMA_VERSION",
    "HypothesisArtifactError",
    "HypothesisDocument",
    "HypothesisFalsifier",
    "HypothesisFrontMatter",
    "HypothesisLinks",
    "HypothesisStatus",
    "HypothesisStore",
    "InMemoryHypothesisStore",
    "LineageError",
    "RevisionLineage",
    "RevisionNode",
    "RevisionOutcome",
    "RunIdentity",
    "WorkspaceHypothesisStore",
    "parse_hypothesis",
    "render_hypothesis",
    "slugify",
]
