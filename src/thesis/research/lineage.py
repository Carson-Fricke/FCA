"""Append-only revision lineage for strategy research iterations.

Every recorded backtest or revision cycle becomes one immutable
:class:`RevisionNode` keyed by the job manifest identity.  Nodes chain to
their parent through the parent's content digest, so the lineage is
tamper-evident and append-only: an existing node is never rewritten, and a
revision must record a new experiment contract.  A failed hypothesis can
only become positive under a newly recorded claim that explicitly supersedes
the failed claim.

Canonical hashing follows the repository convention: node documents are
canonical JSON digested with :func:`thesis.persistence.journal.content_hash`,
the same helper used by the workspace command journal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from thesis.persistence.journal import content_hash

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_MAX_RATIONALE = 4_096


class LineageError(RuntimeError):
    """Raised when a lineage record or graph operation is invalid."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LineageError(f"{label} must be a safe lineage identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise LineageError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class RevisionOutcome(StrEnum):
    """The recorded verdict of one run or revision in the lineage."""

    FAILED = "failed"
    PASSED = "passed"


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """The durable identity of one harness job or its reproducibility manifest."""

    job_id: str
    manifest_id: str | None = None
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.job_id, "job_id")
        if self.manifest_id is not None:
            _identifier(self.manifest_id, "manifest_id")
        if self.manifest_sha256 is not None:
            _digest(self.manifest_sha256, "manifest_sha256")

    @property
    def identity_key(self) -> str:
        """Return the content-addressed key when a manifest digest exists."""
        if self.manifest_id is not None and self.manifest_sha256 is not None:
            return f"{self.manifest_id}@sha256:{self.manifest_sha256}"
        return f"job:{self.job_id}"

    def as_dict(self) -> dict[str, object]:
        """Return the detached job identity representation."""
        return {
            "identityKey": self.identity_key,
            "jobId": self.job_id,
            "manifestId": self.manifest_id,
            "manifestSha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class RevisionNode:
    """One immutable lineage record for a job or a revision of the strategy.

    Root nodes carry ``revision=0`` and no parent.  Every revision node
    chains to its parent through ``parent_node_sha256``, continues the
    parent's experiment contract through ``previous_contract_sha256``, and
    records its own new contract digest, so each material adaptation is an
    explicit experiment-contract transition.
    """

    node_id: str
    revision: int
    job: JobIdentity
    strategy_id: str
    strategy_version: str
    strategy_lineage_version: str
    experiment_id: str
    outcome: RevisionOutcome
    claim_id: str
    contract_sha256: str
    rationale: str
    created_at: datetime
    parent_node_id: str | None = None
    parent_node_sha256: str | None = None
    previous_contract_sha256: str | None = None
    failure_class: str | None = None
    confidence: float | None = None
    supersedes_claim_id: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    content_sha256: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if isinstance(self.revision, bool) or self.revision < 0:
            raise LineageError("revision must be a non-negative integer")
        object.__setattr__(self, "job", _job_identity(self.job))
        _identifier(self.strategy_id, "strategy_id")
        _identifier(self.strategy_version, "strategy_version")
        _identifier(self.strategy_lineage_version, "strategy_lineage_version")
        _identifier(self.experiment_id, "experiment_id")
        if not isinstance(self.outcome, RevisionOutcome):
            raise LineageError("outcome must be a RevisionOutcome")
        _identifier(self.claim_id, "claim_id")
        _digest(self.contract_sha256, "contract_sha256")
        if self.parent_node_id is None:
            if self.revision != 0:
                raise LineageError("root lineage nodes must record revision zero")
            if self.parent_node_sha256 is not None:
                raise LineageError("root lineage nodes cannot record a parent hash")
            if self.previous_contract_sha256 is not None:
                raise LineageError("root lineage nodes have no previous contract")
        else:
            _identifier(self.parent_node_id, "parent_node_id")
            if self.revision < 1:
                raise LineageError("revision nodes start at revision one")
            if self.parent_node_sha256 is None:
                raise LineageError("revision nodes must chain to the parent hash")
            if self.previous_contract_sha256 is None:
                raise LineageError("revision nodes must record the previous contract")
            _digest(self.parent_node_sha256, "parent_node_sha256")
            _digest(self.previous_contract_sha256, "previous_contract_sha256")
        if (self.outcome is RevisionOutcome.FAILED) != (self.failure_class is not None):
            raise LineageError("failed outcomes require a failure class and vice versa")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise LineageError("confidence must be between zero and one")
        if self.supersedes_claim_id is not None:
            _identifier(self.supersedes_claim_id, "supersedes_claim_id")
            if self.supersedes_claim_id == self.claim_id:
                raise LineageError("a claim cannot supersede itself")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise LineageError("rationale must be non-empty text")
        if len(self.rationale) > _MAX_RATIONALE or any(
            ord(character) < 32 for character in self.rationale
        ):
            raise LineageError("rationale contains unsupported characters")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise LineageError("created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if not isinstance(self.detail, Mapping):
            raise LineageError("detail must be a mapping")
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))
        object.__setattr__(self, "content_sha256", content_hash(self.document))

    @property
    def passed(self) -> bool:
        """Return whether this node recorded a passing verdict."""
        return self.outcome is RevisionOutcome.PASSED

    @property
    def contract_transition(self) -> str | None:
        """Return the recorded experiment-contract transition, if any."""
        if self.previous_contract_sha256 is None:
            return None
        return f"{self.previous_contract_sha256}..{self.contract_sha256}"

    @property
    def document(self) -> dict[str, object]:
        """Return the canonical document digested into ``content_sha256``."""
        return {
            "claimId": self.claim_id,
            "confidence": self.confidence,
            "contractSha256": self.contract_sha256,
            "createdAt": _timestamp(self.created_at),
            "detail": dict(self.detail),
            "experimentId": self.experiment_id,
            "failureClass": self.failure_class,
            "nodeId": self.node_id,
            "outcome": self.outcome.value,
            "parentNodeId": self.parent_node_id,
            "parentNodeSha256": self.parent_node_sha256,
            "previousContractSha256": self.previous_contract_sha256,
            "rationale": self.rationale,
            "revision": self.revision,
            "job": self.job.as_dict(),
            "strategyId": self.strategy_id,
            "strategyLineageVersion": self.strategy_lineage_version,
            "strategyVersion": self.strategy_version,
            "supersedesClaimId": self.supersedes_claim_id,
        }

    def as_dict(self) -> dict[str, object]:
        """Return the detached transport representation of this node."""
        return {
            **self.document,
            "contentSha256": self.content_sha256,
            "jobIdentityKey": self.job.identity_key,
        }


def _job_identity(value: object) -> JobIdentity:
    if isinstance(value, JobIdentity):
        return value
    if isinstance(value, Mapping):
        return JobIdentity(
            job_id=str(value.get("jobId", "")),
            manifest_id=_optional(value.get("manifestId")),
            manifest_sha256=_optional(value.get("manifestSha256")),
        )
    raise LineageError("job must be a JobIdentity")


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


class RevisionLineage:
    """An append-only graph of :class:`RevisionNode` records.

    The graph accepts only chain-valid appends: the parent must already be
    present, the parent digest must match, the revision must increment, the
    experiment contract must change, and a failed hypothesis may only become
    positive under a new claim that supersedes the failed claim.
    """

    def __init__(self, nodes: Iterable[RevisionNode] = ()) -> None:
        self._nodes: list[RevisionNode] = []
        self._by_id: dict[str, RevisionNode] = {}
        for node in nodes:
            self._append(node)

    @property
    def nodes(self) -> tuple[RevisionNode, ...]:
        """Return every node in append order."""
        return tuple(self._nodes)

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return isinstance(node_id, str) and node_id in self._by_id

    def get(self, node_id: str) -> RevisionNode:
        """Return one node by identifier."""
        try:
            return self._by_id[node_id]
        except (KeyError, TypeError) as error:
            raise LineageError(f"unknown lineage node {node_id!r}") from error

    def append(self, node: RevisionNode) -> RevisionNode:
        """Validate and append one node to the lineage."""
        return self._append(node)

    def root(self, node_id: str) -> RevisionNode:
        """Return the root ancestor of the node."""
        return self.chain(node_id)[0]

    def ancestors(self, node_id: str) -> tuple[RevisionNode, ...]:
        """Return the strict ancestors of the node ordered root to parent."""
        return self.chain(node_id)[:-1]

    def chain(self, node_id: str) -> tuple[RevisionNode, ...]:
        """Return the full chain from the root to the node inclusive."""
        chain = []
        current = self.get(node_id)
        while True:
            chain.append(current)
            if current.parent_node_id is None:
                break
            current = self.get(current.parent_node_id)
        chain.reverse()
        return tuple(chain)

    def children(self, node_id: str) -> tuple[RevisionNode, ...]:
        """Return the immediate children of the node in append order."""
        self.get(node_id)
        return tuple(node for node in self._nodes if node.parent_node_id == node_id)

    def branch_factor(self, node_id: str) -> int:
        """Return the number of immediate children of the node."""
        return len(self.children(node_id))

    @property
    def max_branch_factor(self) -> int:
        """Return the widest fan-out anywhere in the graph."""
        return max(
            (self.branch_factor(node.node_id) for node in self._nodes), default=0
        )

    def verify(self) -> None:
        """Recompute every digest and parent link; raise on any tampering."""
        by_id: dict[str, RevisionNode] = {}
        for node in self._nodes:
            if content_hash(node.document) != node.content_sha256:
                raise LineageError(f"lineage node {node.node_id!r} was modified")
            if node.parent_node_id is not None:
                parent = by_id.get(node.parent_node_id)
                if parent is None or node.parent_node_sha256 != parent.content_sha256:
                    raise LineageError(
                        f"lineage node {node.node_id!r} has a broken parent chain"
                    )
            by_id[node.node_id] = node

    def _append(self, node: RevisionNode) -> RevisionNode:
        if not isinstance(node, RevisionNode):
            raise LineageError("only RevisionNode values join the lineage")
        if node.node_id in self._by_id:
            raise LineageError(f"lineage node {node.node_id!r} already exists")
        if node.parent_node_id is None:
            if node.revision != 0:
                raise LineageError("root lineage nodes must record revision zero")
        else:
            parent = self._by_id.get(node.parent_node_id)
            if parent is None:
                raise LineageError(
                    f"parent {node.parent_node_id!r} is not in the lineage"
                )
            if node.revision != parent.revision + 1:
                raise LineageError("a revision must increment the parent revision")
            if node.parent_node_sha256 != parent.content_sha256:
                raise LineageError("parent digest does not match the lineage chain")
            if node.previous_contract_sha256 != parent.contract_sha256:
                raise LineageError(
                    "experiment-contract transition must continue the parent contract"
                )
            if node.contract_sha256 == parent.contract_sha256:
                raise LineageError("a revision must record a new experiment contract")
            self._validate_claims(node, parent)
        self._nodes.append(node)
        self._by_id[node.node_id] = node
        return node

    @staticmethod
    def _validate_claims(node: RevisionNode, parent: RevisionNode) -> None:
        if node.supersedes_claim_id is not None:
            if parent.outcome is not RevisionOutcome.FAILED:
                raise LineageError("only a failed parent claim can be superseded")
            if node.supersedes_claim_id != parent.claim_id:
                raise LineageError(
                    "superseded claim must reference the failed parent claim"
                )
        if node.passed and parent.outcome is RevisionOutcome.FAILED:
            if node.claim_id == parent.claim_id:
                raise LineageError(
                    "a failed hypothesis cannot become positive without "
                    "recording a new claim"
                )
            if node.supersedes_claim_id != parent.claim_id:
                raise LineageError(
                    "a positive revision must record the superseded failed claim"
                )


__all__ = [
    "JobIdentity",
    "LineageError",
    "RevisionLineage",
    "RevisionNode",
    "RevisionOutcome",
]
