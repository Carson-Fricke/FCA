"""The bounded candidate-evaluation contract for the optimization phase.

The optimization phase composes a bounded batch of parameter assignments for
the designed strategy, submits them once through the workspace batch boundary,
inspects each run, and hands forward the whole evaluation: the committed
strategy receipt, the batch identity, every assignment with its honest state
and recorded metrics, a shortlist whose parameter vectors come from the
submitted assignments, the selection rule that produced it, and the honest
limitations.  :meth:`CandidateEvaluation.from_dict` is both the rebuild fold's
decoder and the completion validator's drift guard, so a stored or submitted
payload either composes exactly here or is rejected with a
:class:`~.errors.WorkflowError`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from thesis.agent.runtime.schema import (
    render_violations,
    validate_arguments,
    violations,
)
from thesis.workflows.research_models import PROGRAM_REFERENCE_SCHEMA

from ..errors import WorkflowError
from ..primitives import _text

#: The states one submitted candidate assignment may honestly report.
CANDIDATE_ASSIGNMENT_STATES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "running",
    "cancelled",
    "unknown",
)

#: Text caps mirrored from the candidate evaluation payload schema.
MAX_BATCH_POINTS = 16
_MAX_JOB_ID = 256
_MAX_BATCH_ID = 256
_MAX_ERROR = 8_192
_MAX_RATIONALE = 2_048
_MAX_SELECTION_RULE = 4_096
_MAX_LIMITATIONS = 4_096


def _field(payload: Mapping[str, object], *keys: str) -> Any:
    """Return the first present spelling of one wire field."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _canonical_parameters(parameters: Mapping[str, object]) -> str:
    """Return the canonical JSON identity of one parameter vector."""
    if not all(isinstance(name, str) for name in parameters):
        raise WorkflowError("parameter names must be strings")
    try:
        return json.dumps(
            parameters,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise WorkflowError(
            f"parameter values must be finite JSON scalars: {error}"
        ) from error


def _metric_errors(metrics: Mapping[str, object], label: str) -> list[str]:
    """Collect every metric value that is not a finite number when numeric."""
    errors: list[str] = []
    for name, value in metrics.items():
        if not isinstance(name, str):
            errors.append(f"{label}: metric names must be strings")
        elif isinstance(value, bool) or (
            isinstance(value, (int, float)) and not math.isfinite(value)
        ):
            errors.append(f"{label}.{name}: metric values must be finite numbers")
    return errors


def _checked_parameters(parameters: object, label: str) -> dict[str, object]:
    """Validate and copy one parameter mapping."""
    if not isinstance(parameters, Mapping):
        raise WorkflowError(f"{label} must be an object mapping parameter names")
    copied = dict(parameters)
    _canonical_parameters(copied)
    return copied


def _checked_metrics(metrics: object, label: str) -> dict[str, object] | None:
    """Validate and copy one optional metrics mapping."""
    if metrics is None:
        return None
    if not isinstance(metrics, Mapping):
        raise WorkflowError(f"{label} must be an object mapping metric names")
    errors = _metric_errors(metrics, label)
    if errors:
        raise WorkflowError("; ".join(errors))
    return dict(metrics)


@dataclass(frozen=True, slots=True)
class CandidateAssignment:
    """One submitted parameter point with its honest run state and metrics."""

    parameters: Mapping[str, object]
    job_id: str
    state: str
    metrics: Mapping[str, object] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            _checked_parameters(self.parameters, "candidate assignment parameters"),
        )
        _text(self.job_id, "candidate assignment job_id", max_length=_MAX_JOB_ID)
        if self.state not in CANDIDATE_ASSIGNMENT_STATES:
            raise WorkflowError(
                "candidate assignment state must be one of "
                f"{', '.join(CANDIDATE_ASSIGNMENT_STATES)}"
            )
        object.__setattr__(
            self,
            "metrics",
            _checked_metrics(self.metrics, "candidate assignment metrics"),
        )
        if self.error is not None:
            _text(self.error, "candidate assignment error", max_length=_MAX_ERROR)

    def as_dict(self) -> dict[str, object]:
        """Return the portable assignment representation."""
        return {
            "parameters": dict(self.parameters),
            "jobId": self.job_id,
            "state": self.state,
            "metrics": None if self.metrics is None else dict(self.metrics),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateAssignment:
        """Rebuild one assignment from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("candidate assignment must be an object")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise WorkflowError("candidate assignment requires a parameters object")
        metrics = payload.get("metrics")
        if metrics is not None and not isinstance(metrics, Mapping):
            raise WorkflowError("candidate assignment metrics must be an object")
        error = _field(payload, "error")
        if error is not None and not isinstance(error, str):
            raise WorkflowError("candidate assignment error must be text")
        return cls(
            parameters=parameters,
            job_id=_field(payload, "jobId", "job_id"),
            state=_field(payload, "state"),
            metrics=metrics,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class CandidateShortlistEntry:
    """One selected candidate with the rationale that selected it."""

    parameters: Mapping[str, object]
    job_id: str
    rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            _checked_parameters(self.parameters, "candidate shortlist parameters"),
        )
        _text(self.job_id, "candidate shortlist job_id", max_length=_MAX_JOB_ID)
        _text(
            self.rationale, "candidate shortlist rationale", max_length=_MAX_RATIONALE
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable shortlist-entry representation."""
        return {
            "parameters": dict(self.parameters),
            "jobId": self.job_id,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateShortlistEntry:
        """Rebuild one shortlist entry from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("candidate shortlist entry must be an object")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise WorkflowError(
                "candidate shortlist entry requires a parameters object"
            )
        return cls(
            parameters=parameters,
            job_id=_field(payload, "jobId", "job_id"),
            rationale=_field(payload, "rationale"),
        )


def _strategy_reference_schema() -> dict[str, object]:
    """Return the committed strategy receipt schema with kind pinned."""
    properties = cast(dict[str, object], PROGRAM_REFERENCE_SCHEMA["properties"])
    return {
        **PROGRAM_REFERENCE_SCHEMA,
        "properties": {
            **properties,
            "kind": {"type": "string", "enum": ["strategy"]},
        },
    }


_ASSIGNMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "parameters": {"type": "object"},
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "state": {"type": "string", "enum": list(CANDIDATE_ASSIGNMENT_STATES)},
        "metrics": {"type": ["object", "null"]},
        "error": {"type": ["string", "null"], "maxLength": _MAX_ERROR},
    },
    "required": ["parameters", "jobId", "state"],
}

_SHORTLIST_ENTRY_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "parameters": {"type": "object"},
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "rationale": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_RATIONALE,
            "pattern": r"\S",
        },
    },
    "required": ["parameters", "jobId", "rationale"],
}

#: The advertised wire shape of one candidate-evaluation payload.
CANDIDATE_EVALUATION_PAYLOAD_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "strategy": _strategy_reference_schema(),
        "batchId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_BATCH_ID,
            "pattern": r"\S",
        },
        "assignments": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_BATCH_POINTS,
            "items": _ASSIGNMENT_SCHEMA,
        },
        "shortlist": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_BATCH_POINTS,
            "items": _SHORTLIST_ENTRY_SCHEMA,
        },
        "selectionRule": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_SELECTION_RULE,
            "pattern": r"\S",
        },
        "limitations": {
            "type": ["string", "null"],
            "maxLength": _MAX_LIMITATIONS,
        },
    },
    "required": [
        "strategy",
        "batchId",
        "assignments",
        "shortlist",
        "selectionRule",
    ],
}


def candidate_evaluation_payload_errors(
    payload: Mapping[str, object],
) -> list[str]:
    """Collect structural misses and the candidate-evaluation cross-checks."""
    if not isinstance(payload, Mapping):
        return ["candidateEvaluation must be an object"]
    errors = [
        miss.render()
        for miss in violations(
            CANDIDATE_EVALUATION_PAYLOAD_SCHEMA, payload, "candidateEvaluation"
        )
    ]
    strategy = payload.get("strategy")
    if isinstance(strategy, Mapping) and strategy.get("kind") != "strategy":
        errors.append("candidateEvaluation.strategy.kind must be 'strategy'")
    assignment_vectors: set[str] = set()
    assignments = payload.get("assignments")
    if isinstance(assignments, Sequence) and not isinstance(assignments, (str, bytes)):
        for index, item in enumerate(assignments):
            if not isinstance(item, Mapping):
                continue
            parameters = item.get("parameters")
            if isinstance(parameters, Mapping):
                try:
                    assignment_vectors.add(_canonical_parameters(parameters))
                except WorkflowError as error:
                    errors.append(
                        f"candidateEvaluation.assignments[{index}].parameters: {error}"
                    )
            metrics = item.get("metrics")
            if isinstance(metrics, Mapping):
                errors.extend(
                    _metric_errors(
                        metrics,
                        f"candidateEvaluation.assignments[{index}].metrics",
                    )
                )
    shortlist = payload.get("shortlist")
    if isinstance(shortlist, Sequence) and not isinstance(shortlist, (str, bytes)):
        for index, item in enumerate(shortlist):
            if not isinstance(item, Mapping):
                continue
            parameters = item.get("parameters")
            if not isinstance(parameters, Mapping):
                continue
            try:
                vector = _canonical_parameters(parameters)
            except WorkflowError as error:
                errors.append(
                    f"candidateEvaluation.shortlist[{index}].parameters: {error}"
                )
                continue
            if vector not in assignment_vectors:
                errors.append(
                    f"candidateEvaluation.shortlist[{index}].parameters must match "
                    "one of the submitted assignment parameter vectors"
                )
    return errors


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """The accepted bounded evaluation: assignments, shortlist, and reasoning."""

    strategy: Mapping[str, object]
    batch_id: str
    assignments: tuple[CandidateAssignment, ...]
    shortlist: tuple[CandidateShortlistEntry, ...]
    selection_rule: str
    limitations: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, Mapping):
            raise WorkflowError("candidate evaluation strategy must be an object")
        misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, self.strategy)
        if misses:
            raise WorkflowError(
                "invalid candidate evaluation strategy: " + render_violations(misses)
            )
        if self.strategy.get("kind") != "strategy":
            raise WorkflowError(
                "candidate evaluation must reference a strategy program"
            )
        object.__setattr__(self, "strategy", dict(self.strategy))
        _text(self.batch_id, "candidate evaluation batch_id", max_length=_MAX_BATCH_ID)
        if isinstance(self.assignments, str) or not isinstance(self.assignments, tuple):
            raise WorkflowError("candidate evaluation assignments must be a tuple")
        if not 1 <= len(self.assignments) <= MAX_BATCH_POINTS:
            raise WorkflowError(
                f"candidate evaluation requires 1 to {MAX_BATCH_POINTS} assignments"
            )
        if any(not isinstance(item, CandidateAssignment) for item in self.assignments):
            raise WorkflowError(
                "candidate evaluation assignments must be CandidateAssignment values"
            )
        if isinstance(self.shortlist, str) or not isinstance(self.shortlist, tuple):
            raise WorkflowError("candidate evaluation shortlist must be a tuple")
        if not 1 <= len(self.shortlist) <= MAX_BATCH_POINTS:
            raise WorkflowError(
                f"candidate evaluation requires 1 to {MAX_BATCH_POINTS} shortlist "
                "entries"
            )
        if any(
            not isinstance(item, CandidateShortlistEntry) for item in self.shortlist
        ):
            raise WorkflowError(
                "candidate evaluation shortlist must be CandidateShortlistEntry values"
            )
        _text(
            self.selection_rule,
            "candidate evaluation selection_rule",
            max_length=_MAX_SELECTION_RULE,
        )
        if self.limitations is not None:
            _text(
                self.limitations,
                "candidate evaluation limitations",
                max_length=_MAX_LIMITATIONS,
            )
        vectors = {_canonical_parameters(item.parameters) for item in self.assignments}
        for index, entry in enumerate(self.shortlist):
            if _canonical_parameters(entry.parameters) not in vectors:
                raise WorkflowError(
                    f"candidate evaluation shortlist[{index}] parameters must match "
                    "one of the submitted assignment parameter vectors"
                )

    def as_dict(self) -> dict[str, object]:
        """Return the portable candidate-evaluation representation."""
        return {
            "strategy": dict(self.strategy),
            "batchId": self.batch_id,
            "assignments": [item.as_dict() for item in self.assignments],
            "shortlist": [item.as_dict() for item in self.shortlist],
            "selectionRule": self.selection_rule,
            "limitations": self.limitations,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateEvaluation:
        """Rebuild one candidate evaluation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("candidate evaluation must be an object")
        strategy = payload.get("strategy")
        if not isinstance(strategy, Mapping):
            raise WorkflowError("candidate evaluation requires a strategy receipt")
        assignments = payload.get("assignments")
        if isinstance(assignments, str) or not isinstance(assignments, Sequence):
            raise WorkflowError("candidate evaluation requires an assignments list")
        shortlist = payload.get("shortlist")
        if isinstance(shortlist, str) or not isinstance(shortlist, Sequence):
            raise WorkflowError("candidate evaluation requires a shortlist list")
        return cls(
            strategy=strategy,
            batch_id=_field(payload, "batchId", "batch_id"),
            assignments=tuple(
                CandidateAssignment.from_dict(cast(Mapping[str, object], item))
                for item in assignments
            ),
            shortlist=tuple(
                CandidateShortlistEntry.from_dict(cast(Mapping[str, object], item))
                for item in shortlist
            ),
            selection_rule=_field(payload, "selectionRule", "selection_rule"),
            limitations=_field(payload, "limitations"),
        )


__all__ = [
    "CANDIDATE_ASSIGNMENT_STATES",
    "CANDIDATE_EVALUATION_PAYLOAD_SCHEMA",
    "CandidateAssignment",
    "CandidateEvaluation",
    "CandidateShortlistEntry",
    "candidate_evaluation_payload_errors",
]
