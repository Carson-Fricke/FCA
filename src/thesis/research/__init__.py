"""Research-domain records for the Thesis vertical.

The first capability in this package is the append-only strategy revision
lineage (:mod:`thesis.research.lineage`) that records which run and revision
descended from which, with the rationale and the experiment-contract
transition for every material adaptation.
"""

from thesis.research.lineage import (
    LineageError,
    RevisionLineage,
    RevisionNode,
    RevisionOutcome,
    RunIdentity,
)

__all__ = [
    "LineageError",
    "RevisionLineage",
    "RevisionNode",
    "RevisionOutcome",
    "RunIdentity",
]
