"""Frozen experiment contracts with canonical hashing and audited deviations.

An experiment contract is the pre-registration of one experiment: the
hypothesis it tests, the primary endpoint and comparison basis, the data
partitions, the cost model, the candidate-selection rule, the stopping rule,
and the permitted follow-up work.  Contracts are frozen dataclasses whose
identity is a domain-separated SHA-256 over a canonical JSON document,
following the bundle and evidence-report conventions in this repository.

Deviations from a contract are never silent.  ``detect_deviations`` compares
observed values against the canonical document and reports every modified or
undeclared field; ``record_deviation`` turns a finding into an explicit,
reasoned, content-hashed :class:`ContractDeviation` that can be appended to
the attempt ledger.  A deviation without a reason is rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

_SCHEMA = "fca-thesis-experiment-contract-v1"
_HASH_DOMAIN = b"fca:thesis-experiment-contract:v1\0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ExperimentContractError(ValueError):
    """Raised when an experiment contract would be incomplete or ambiguous."""


class ClaimKind(StrEnum):
    """What kind of claim the contract classifies its result as."""

    DESCRIPTIVE = "descriptive"
    PREDICTIVE = "predictive"
    CAUSAL = "causal"


class StoppingRuleKind(StrEnum):
    """The declared family of the sequential stopping rule."""

    FIXED_SAMPLE = "fixed_sample"
    MAX_LOOKS = "max_looks"
    SEQUENTIAL_BOUNDARY = "sequential_boundary"


class PartitionRole(StrEnum):
    """Time-ordered role of one declared data partition."""

    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class DeviationKind(StrEnum):
    """Whether a contract field changed or was never declared."""

    MODIFIED_FIELD = "modified_field"
    UNDECLARED_FIELD = "undeclared_field"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ExperimentContractError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise ExperimentContractError(
            "workspace_id must be a safe workspace identifier"
        )
    return value


def _version(value: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ExperimentContractError("version must be a semantic version string")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExperimentContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentContractError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ExperimentContractError(f"{label} contains unsupported characters")
    return value


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ExperimentContractError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperimentContractError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        )
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _jsonable(value: object) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExperimentContractError("contract mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise ExperimentContractError("contract values must be JSON-shaped")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _jsonable(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ExperimentContractError(
            "contract values must be canonical JSON"
        ) from error


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError(f"{label} must be a JSON-shaped mapping")
    if not all(isinstance(key, str) for key in value):
        raise ExperimentContractError(f"{label} must have string keys")
    frozen = MappingProxyType(dict(value))
    _canonical_json(_jsonable(dict(frozen)))
    return frozen


@dataclass(frozen=True, slots=True)
class TestingExposure:
    """Multiple-testing exposure of one experiment against its stopping rule."""

    observed_looks: int
    allowed_looks: int | None
    exceeds_budget: bool
    governed_by: str


@dataclass(frozen=True, slots=True)
class StoppingRule:
    """The pre-declared stopping rule for one experiment."""

    kind: StoppingRuleKind
    description: str
    max_looks: int | None = None
    look_interval_ns: int | None = None
    boundary: str | None = None

    def __post_init__(self) -> None:
        _text(self.description, "stopping rule description")
        if self.max_looks is not None:
            if isinstance(self.max_looks, bool) or not isinstance(self.max_looks, int):
                raise ExperimentContractError("max_looks must be an integer")
            if self.max_looks < 1:
                raise ExperimentContractError("max_looks must be at least one")
        if self.look_interval_ns is not None:
            if isinstance(self.look_interval_ns, bool) or not isinstance(
                self.look_interval_ns, int
            ):
                raise ExperimentContractError("look_interval_ns must be an integer")
            if self.look_interval_ns <= 0:
                raise ExperimentContractError("look_interval_ns must be positive")
        if self.boundary is not None:
            _text(self.boundary, "stopping rule boundary")
        if self.kind is StoppingRuleKind.FIXED_SAMPLE:
            if self.max_looks is not None and self.max_looks != 1:
                raise ExperimentContractError(
                    "fixed-sample rules allow at most one look"
                )
            if self.boundary is not None:
                raise ExperimentContractError("fixed-sample rules declare no boundary")
        elif self.kind is StoppingRuleKind.MAX_LOOKS:
            if self.max_looks is None:
                raise ExperimentContractError("max-looks rules must declare max_looks")
            if self.boundary is not None:
                raise ExperimentContractError("max-looks rules declare no boundary")
        elif self.kind is StoppingRuleKind.SEQUENTIAL_BOUNDARY:
            if self.boundary is None:
                raise ExperimentContractError(
                    "sequential-boundary rules must declare a boundary"
                )
        else:
            raise ExperimentContractError("stopping rule kind is not supported")

    def exposure(self, observed_looks: int) -> TestingExposure:
        """Return the multiple-testing exposure for a number of result looks.

        Fixed-sample rules allow a single look, max-looks rules allow
        ``max_looks`` looks, and sequential-boundary rules are governed by the
        declared boundary, so the look count is reported without a numeric
        budget and never exceeds it here.
        """
        if isinstance(observed_looks, bool) or not isinstance(observed_looks, int):
            raise ExperimentContractError("observed_looks must be an integer")
        if observed_looks < 0:
            raise ExperimentContractError("observed_looks must not be negative")
        if self.kind is StoppingRuleKind.FIXED_SAMPLE:
            return TestingExposure(
                observed_looks=observed_looks,
                allowed_looks=1,
                exceeds_budget=observed_looks > 1,
                governed_by=StoppingRuleKind.FIXED_SAMPLE.value,
            )
        if self.kind is StoppingRuleKind.MAX_LOOKS:
            return TestingExposure(
                observed_looks=observed_looks,
                allowed_looks=self.max_looks,
                exceeds_budget=observed_looks > int(self.max_looks or 0),
                governed_by=StoppingRuleKind.MAX_LOOKS.value,
            )
        return TestingExposure(
            observed_looks=observed_looks,
            allowed_looks=None,
            exceeds_budget=False,
            governed_by=StoppingRuleKind.SEQUENTIAL_BOUNDARY.value,
        )


@dataclass(frozen=True, slots=True)
class Partition:
    """One time-ordered data partition with purge and embargo allowances."""

    role: PartitionRole
    start_ns: int
    end_ns: int
    purged_ns: int = 0
    embargoed_ns: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.start_ns, bool) or not isinstance(self.start_ns, int):
            raise ExperimentContractError("partition boundaries must be integers")
        if isinstance(self.end_ns, bool) or not isinstance(self.end_ns, int):
            raise ExperimentContractError("partition boundaries must be integers")
        if self.start_ns >= self.end_ns:
            raise ExperimentContractError("partition start must precede its end")
        for name in ("purged_ns", "embargoed_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExperimentContractError(f"partition {name} must not be negative")


@dataclass(frozen=True, slots=True)
class DeviationFinding:
    """One deterministic difference between an observation and a contract."""

    field: str
    deviation_kind: DeviationKind
    declared: object
    observed: object


@dataclass(frozen=True, slots=True)
class ContractDeviation:
    """An explicit, reasoned, content-hashed deviation from a contract."""

    contract_sha256: str
    field: str
    deviation_kind: DeviationKind
    declared: object
    observed: object
    reason: str
    actor: str
    recorded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_sha256", _digest(self.contract_sha256, "contract_sha256")
        )
        _text(self.field, "deviation field")
        object.__setattr__(self, "reason", _text(self.reason, "deviation reason"))
        object.__setattr__(self, "actor", _identifier(self.actor, "deviation actor"))
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this deviation."""
        return {
            "actor": self.actor,
            "contractSha256": self.contract_sha256,
            "declared": _jsonable(self.declared),
            "deviationKind": self.deviation_kind.value,
            "field": self.field,
            "observed": _jsonable(self.observed),
            "reason": self.reason,
            "recordedAt": self.recorded_at,
            "schema": "fca-thesis-contract-deviation-v1",
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this deviation record."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentContract:
    """The frozen pre-registration of one experiment.

    Every field is locked at creation and the whole contract is content
    addressed by :attr:`contract_sha256`.  Changing any field creates a new
    contract instead of mutating the pre-registration.
    """

    workspace_id: str
    experiment_id: str
    version: str
    title: str
    claim_kind: ClaimKind
    hypothesis: str
    primary_endpoint: str
    stopping_rule: StoppingRule
    partitions: tuple[Partition, ...]
    created_by: str
    created_at: str
    null_hypothesis: str | None = None
    alternative_hypothesis: str | None = None
    minimum_effect: str | None = None
    uncertainty_method: str | None = None
    multiplicity_family: str | None = None
    selection_rule: str | None = None
    missingness_policy: str | None = None
    comparison_basis: str | None = None
    identification_design: str | None = None
    release_budget: int | None = None
    cost_model: Mapping[str, object] = field(default_factory=dict)
    permitted_followups: tuple[str, ...] = ()
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(self, "version", _version(self.version))
        object.__setattr__(self, "title", _text(self.title, "title"))
        if not isinstance(self.claim_kind, ClaimKind):
            raise ExperimentContractError("claim_kind must be a ClaimKind")
        object.__setattr__(self, "hypothesis", _text(self.hypothesis, "hypothesis"))
        object.__setattr__(
            self, "primary_endpoint", _text(self.primary_endpoint, "primary_endpoint")
        )
        if not isinstance(self.stopping_rule, StoppingRule):
            raise ExperimentContractError("stopping_rule must be a StoppingRule")
        for name in (
            "null_hypothesis",
            "alternative_hypothesis",
            "minimum_effect",
            "uncertainty_method",
            "multiplicity_family",
            "selection_rule",
            "missingness_policy",
            "comparison_basis",
            "identification_design",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.claim_kind is ClaimKind.CAUSAL and self.identification_design is None:
            raise ExperimentContractError(
                "causal claims must declare an identification design"
            )
        if self.release_budget is not None:
            if isinstance(self.release_budget, bool) or not isinstance(
                self.release_budget, int
            ):
                raise ExperimentContractError("release_budget must be an integer")
            if self.release_budget < 1:
                raise ExperimentContractError("release_budget must be at least one")
        object.__setattr__(
            self, "cost_model", _freeze_mapping(self.cost_model, "cost_model")
        )
        object.__setattr__(
            self, "created_by", _identifier(self.created_by, "created_by")
        )
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, "created_at")
        )
        object.__setattr__(self, "partitions", _partitions(self.partitions))
        object.__setattr__(
            self, "permitted_followups", _followups(self.permitted_followups)
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the contract hash covers."""
        return {
            "alternativeHypothesis": self.alternative_hypothesis,
            "claimKind": self.claim_kind.value,
            "comparisonBasis": self.comparison_basis,
            "costModel": _jsonable(dict(self.cost_model)),
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "experimentId": self.experiment_id,
            "hypothesis": self.hypothesis,
            "identificationDesign": self.identification_design,
            "minimumEffect": self.minimum_effect,
            "missingnessPolicy": self.missingness_policy,
            "multiplicityFamily": self.multiplicity_family,
            "nullHypothesis": self.null_hypothesis,
            "partitions": [
                {
                    "embargoedNs": partition.embargoed_ns,
                    "endNs": partition.end_ns,
                    "purgedNs": partition.purged_ns,
                    "role": partition.role.value,
                    "startNs": partition.start_ns,
                }
                for partition in self.partitions
            ],
            "permittedFollowups": list(self.permitted_followups),
            "primaryEndpoint": self.primary_endpoint,
            "releaseBudget": self.release_budget,
            "schema": self.schema,
            "selectionRule": self.selection_rule,
            "stoppingRule": {
                "boundary": self.stopping_rule.boundary,
                "description": self.stopping_rule.description,
                "kind": self.stopping_rule.kind.value,
                "lookIntervalNs": self.stopping_rule.look_interval_ns,
                "maxLooks": self.stopping_rule.max_looks,
            },
            "title": self.title,
            "uncertaintyMethod": self.uncertainty_method,
            "version": self.version,
            "workspaceId": self.workspace_id,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return the deterministic UTF-8 contract representation."""
        return _canonical_json(self.canonical_document).encode("utf-8")

    @property
    def contract_sha256(self) -> str:
        """Return the domain-separated content address of this contract."""
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()

    @property
    def contract_id(self) -> str:
        """Return the self-describing contract identity."""
        return f"ect-{self.contract_sha256[:48]}"


def _partitions(values: Sequence[Partition]) -> tuple[Partition, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ExperimentContractError("partitions must be a sequence of partitions")
    prepared = tuple(values)
    if not prepared:
        raise ExperimentContractError("contracts must declare at least one partition")
    for partition in prepared:
        if not isinstance(partition, Partition):
            raise ExperimentContractError("partitions must contain Partition values")
    ordered = tuple(sorted(prepared, key=lambda item: (item.start_ns, item.end_ns)))
    for index in range(1, len(ordered)):
        if ordered[index - 1].end_ns > ordered[index].start_ns:
            raise ExperimentContractError(
                "partitions must be time-ordered and non-overlapping"
            )
    return ordered


def _followups(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ExperimentContractError("permitted follow-ups must be a sequence")
    normalized: list[str] = []
    for value in values:
        normalized.append(_text(value, "permitted follow-up"))
    return tuple(sorted(dict.fromkeys(normalized)))


def detect_deviations(
    contract: ExperimentContract, observed: Mapping[str, object]
) -> tuple[DeviationFinding, ...]:
    """Compare observed values against the contract canonical document.

    A field present in both documents whose canonical JSON differs is a
    ``modified_field`` deviation; an observed key that the contract never
    declares is an ``undeclared_field`` deviation.  Findings are returned in
    sorted field order.
    """
    if not isinstance(observed, Mapping):
        raise ExperimentContractError("observed values must be a mapping")
    document = contract.canonical_document
    findings: list[DeviationFinding] = []
    for key in sorted(observed):
        value = observed[key]
        if key not in document:
            findings.append(
                DeviationFinding(
                    field=key,
                    deviation_kind=DeviationKind.UNDECLARED_FIELD,
                    declared=None,
                    observed=value,
                )
            )
            continue
        declared = document[key]
        if _canonical_json(declared) != _canonical_json(value):
            findings.append(
                DeviationFinding(
                    field=key,
                    deviation_kind=DeviationKind.MODIFIED_FIELD,
                    declared=declared,
                    observed=value,
                )
            )
    return tuple(findings)


def record_deviation(
    contract: ExperimentContract,
    finding: DeviationFinding,
    *,
    reason: str,
    actor: str,
    recorded_at: str,
) -> ContractDeviation:
    """Turn a detected deviation into an explicit audited record.

    The reason is mandatory: a deviation without a stated reason is rejected
    instead of being recorded silently.
    """
    if not isinstance(finding, DeviationFinding):
        raise ExperimentContractError("finding must be a DeviationFinding")
    return ContractDeviation(
        contract_sha256=contract.contract_sha256,
        field=finding.field,
        deviation_kind=finding.deviation_kind,
        declared=finding.declared,
        observed=finding.observed,
        reason=reason,
        actor=actor,
        recorded_at=recorded_at,
    )


__all__ = [
    "ClaimKind",
    "ContractDeviation",
    "DeviationFinding",
    "DeviationKind",
    "ExperimentContract",
    "ExperimentContractError",
    "Partition",
    "PartitionRole",
    "StoppingRule",
    "StoppingRuleKind",
    "TestingExposure",
    "detect_deviations",
    "record_deviation",
]
