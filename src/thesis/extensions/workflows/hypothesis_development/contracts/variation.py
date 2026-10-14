"""Deterministic marginal and explicitly declared joint-parameter experiments.

The typed decoder owns vector identity, changed-key, interaction coverage and
honest run-state rules for completion and durable replay. There is no universal
point ceiling. Accepted vectors and interaction groups freeze before forward
exposure; missing and undefined financial metrics are never synthesized.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from thesis.research.evidence import metric_errors, parameter_errors, parameter_identity
from thesis.workflows.research_models import PROGRAM_REFERENCE_SCHEMA
from thesis_runtime.tools.schema import (
    render_violations,
    validate_arguments,
    violations,
)

from ..errors import WorkflowError
from ..primitives import _text

#: The states one submitted variation point may honestly report.
VARIATION_STATES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "running",
    "cancelled",
    "unknown",
)

_MAX_JOB_ID = 256
_MAX_BATCH_ID = 256
_MAX_PARAMETER_NAME = 128
_MAX_STABILITY = 4_096
_MAX_ERROR = 8_192
_MAX_LIMITATIONS = 4_096


def _wire_field(payload: Mapping[str, object], key: str) -> Any:
    """Return one camelCase wire field as decoded JSON, or ``None`` when absent.

    The typed constructors validate the value, so a missing or mistyped field is
    rejected there with its camelCase name.
    """
    return payload.get(key)


def canonical_parameter_identity(parameters: Mapping[str, object]) -> str:
    """Return the type-strict canonical identity of one parameter vector.

    This is the canonical JSON text the workspace parameter digest hashes
    (``programs.parameter_assignment_sha256``): keys sorted, compact separators,
    and no NaN or infinity.  The identity is type-strict, so integer ``5`` and
    float ``5.0`` are different parameter values, and so are ``true`` and ``1``.
    Two vectors are the same parameter vector exactly when their identities are
    equal.  Python equality on the mappings is not that test, because it treats
    ``5`` and ``5.0`` as equal.

    Raises :class:`WorkflowError` when a name is not a string or a value is not
    finite JSON.
    """
    try:
        return parameter_identity(parameters)
    except (TypeError, ValueError) as error:
        raise WorkflowError(
            f"parameter values must be finite JSON scalars: {error}"
        ) from error


def _value_identity(parameters: Mapping[str, object], name: str) -> str | None:
    """Return one parameter's identity, or ``None`` when the vector omits it."""
    if name not in parameters:
        return None
    return canonical_parameter_identity({name: parameters[name]})


def _differing_names(
    left: Mapping[str, object], right: Mapping[str, object]
) -> set[str]:
    """Return the parameter names whose values differ under the identity."""
    return {
        name
        for name in set(left) | set(right)
        if _value_identity(left, name) != _value_identity(right, name)
    }


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


#: A declared parameter vector: named keys with scalar values.  Bound and level
#: membership is the agent's construction duty; finiteness is cross-checked.
_PARAMETERS_SCHEMA: dict[str, object] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": {"type": ["string", "number", "boolean"]},
}

_CENTER_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "parameters": _PARAMETERS_SCHEMA,
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "state": {"type": "string", "enum": ["succeeded"]},
        "metrics": {"type": ["object", "null"]},
        "error": {"type": ["string", "null"], "maxLength": _MAX_ERROR},
    },
    "required": ["parameters", "jobId", "state", "metrics"],
}

_POINT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "parameters": _PARAMETERS_SCHEMA,
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "state": {"type": "string", "enum": list(VARIATION_STATES)},
        "metrics": {"type": ["object", "null"]},
        "error": {"type": ["string", "null"], "maxLength": _MAX_ERROR},
    },
    "required": ["parameters", "jobId", "state", "metrics"],
}

#: The advertised wire shape of one variation payload.
VARIATION_PAYLOAD_SCHEMA: dict[str, object] = {
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
        "center": _CENTER_SCHEMA,
        "method": {"type": "string", "enum": ["one_at_a_time", "joint"]},
        "interactions": {
            "type": "array",
            "uniqueItems": True,
            "items": {
                "type": "array",
                "minItems": 2,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
        "points": {
            "type": "array",
            "minItems": 1,
            "items": _POINT_SCHEMA,
        },
        "parameters": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_PARAMETER_NAME,
                "pattern": r"\S",
            },
        },
        "stability": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_STABILITY,
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
        "center",
        "points",
        "parameters",
        "stability",
        "method",
        "interactions",
    ],
}


def variation_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Collect structural misses and the frozen variation cross-checks."""
    if not isinstance(payload, Mapping):
        return ["variation must be an object"]
    errors = [
        miss.render()
        for miss in violations(VARIATION_PAYLOAD_SCHEMA, payload, "variation")
    ]
    if errors:
        return errors
    try:
        VariationEvaluation.from_dict(payload)
    except WorkflowError as error:
        errors.append(str(error))
    return errors


@dataclass(frozen=True, slots=True)
class VariationPoint:
    """One varied point with its honest run state and recorded metrics."""

    parameters: Mapping[str, object]
    job_id: str
    state: str
    metrics: Mapping[str, object] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, Mapping) or not self.parameters:
            raise WorkflowError("variation point parameters must be a non-empty object")
        errors = parameter_errors(self.parameters, "variation point parameters")
        if errors:
            raise WorkflowError("; ".join(errors))
        object.__setattr__(self, "parameters", dict(self.parameters))
        _text(self.job_id, "variation point jobId", max_length=_MAX_JOB_ID)
        if self.state not in VARIATION_STATES:
            raise WorkflowError(
                f"variation point state must be one of {', '.join(VARIATION_STATES)}"
            )
        if self.metrics is not None:
            if not isinstance(self.metrics, Mapping):
                raise WorkflowError("variation point metrics must be an object")
            errors = metric_errors(self.metrics, "variation point metrics")
            if errors:
                raise WorkflowError("; ".join(errors))
            object.__setattr__(self, "metrics", dict(self.metrics))
        if self.state == "succeeded" and self.metrics is None:
            raise WorkflowError(
                "variation point metrics must be an object for a succeeded point"
            )
        if self.error is not None:
            _text(self.error, "variation point error", max_length=_MAX_ERROR)

    def as_dict(self) -> dict[str, object]:
        """Return the portable point representation."""
        return {
            "parameters": dict(self.parameters),
            "jobId": self.job_id,
            "state": self.state,
            "metrics": None if self.metrics is None else dict(self.metrics),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> VariationPoint:
        """Rebuild one point from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("variation point must be an object")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise WorkflowError("variation point parameters must be an object")
        metrics = payload.get("metrics")
        if metrics is not None and not isinstance(metrics, Mapping):
            raise WorkflowError("variation point metrics must be an object")
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise WorkflowError("variation point error must be text")
        return cls(
            parameters=parameters,
            job_id=_wire_field(payload, "jobId"),
            state=_wire_field(payload, "state"),
            metrics=metrics,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class VariationCenter:
    """The succeeded center configuration the neighborhood varies around."""

    parameters: Mapping[str, object]
    job_id: str
    state: str
    metrics: Mapping[str, object] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, Mapping) or not self.parameters:
            raise WorkflowError(
                "variation center parameters must be a non-empty object"
            )
        errors = parameter_errors(self.parameters, "variation center parameters")
        if errors:
            raise WorkflowError("; ".join(errors))
        object.__setattr__(self, "parameters", dict(self.parameters))
        _text(self.job_id, "variation center jobId", max_length=_MAX_JOB_ID)
        if self.state != "succeeded":
            raise WorkflowError("variation center state must be 'succeeded'")
        if not isinstance(self.metrics, Mapping):
            raise WorkflowError(
                "variation center metrics must be an object for the succeeded center"
            )
        errors = metric_errors(self.metrics, "variation center metrics")
        if errors:
            raise WorkflowError("; ".join(errors))
        object.__setattr__(self, "metrics", dict(self.metrics))
        if self.error is not None:
            _text(self.error, "variation center error", max_length=_MAX_ERROR)

    def as_dict(self) -> dict[str, object]:
        """Return the portable center representation."""
        return {
            "parameters": dict(self.parameters),
            "jobId": self.job_id,
            "state": self.state,
            "metrics": dict(self.metrics) if self.metrics is not None else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> VariationCenter:
        """Rebuild one center from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("variation center must be an object")
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise WorkflowError("variation center parameters must be an object")
        metrics = payload.get("metrics")
        if not isinstance(metrics, Mapping):
            raise WorkflowError(
                "variation center metrics must be an object for the succeeded center"
            )
        error = payload.get("error")
        if error is not None and not isinstance(error, str):
            raise WorkflowError("variation center error must be text")
        return cls(
            parameters=parameters,
            job_id=_wire_field(payload, "jobId"),
            state=_wire_field(payload, "state"),
            metrics=metrics,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class VariationEvaluation:
    """The accepted variation: center, varied points, and stability prose."""

    strategy: Mapping[str, object]
    batch_id: str
    center: VariationCenter
    points: tuple[VariationPoint, ...]
    parameters: tuple[str, ...]
    stability: str
    limitations: str | None = None
    method: str = "one_at_a_time"
    interactions: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, Mapping):
            raise WorkflowError("variation evaluation strategy must be an object")
        misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, self.strategy)
        if misses:
            raise WorkflowError(
                "invalid variation evaluation strategy: " + render_violations(misses)
            )
        if self.strategy.get("kind") != "strategy":
            raise WorkflowError(
                "variation evaluation must reference a strategy program"
            )
        object.__setattr__(self, "strategy", dict(self.strategy))
        _text(self.batch_id, "variation evaluation batchId", max_length=_MAX_BATCH_ID)
        if not isinstance(self.center, VariationCenter):
            raise WorkflowError("variation evaluation center must be a VariationCenter")
        if isinstance(self.points, (str, bytes)) or not isinstance(self.points, tuple):
            raise WorkflowError("variation evaluation points must be a tuple")
        if not len(self.points):
            raise WorkflowError("variation evaluation requires at least one point")
        if any(not isinstance(item, VariationPoint) for item in self.points):
            raise WorkflowError(
                "variation evaluation points must be VariationPoint values"
            )
        if isinstance(self.parameters, (str, bytes)) or not isinstance(
            self.parameters, tuple
        ):
            raise WorkflowError("variation evaluation parameters must be a tuple")
        if not self.parameters:
            raise WorkflowError(
                "variation evaluation parameters must declare at least one name"
            )
        declared: set[str] = set()
        for name in self.parameters:
            _text(
                name,
                "variation evaluation parameter name",
                max_length=_MAX_PARAMETER_NAME,
            )
            if name in declared:
                raise WorkflowError(
                    "variation evaluation parameter names must be unique"
                )
            declared.add(name)
        if self.method not in ("one_at_a_time", "joint"):
            raise WorkflowError("variation method must be one_at_a_time or joint")
        groups = {frozenset(group) for group in self.interactions}
        if len(groups) != len(self.interactions) or any(
            len(group) < 2
            or len(set(group)) != len(group)
            or not set(group) <= declared
            for group in self.interactions
        ):
            raise WorkflowError(
                "variation interactions must be unique groups of at least two declared parameters"
            )
        if self.method == "one_at_a_time" and groups:
            raise WorkflowError("one_at_a_time variation cannot declare interactions")
        if self.method == "joint" and not groups:
            raise WorkflowError("joint variation requires declared interactions")
        _text(
            self.stability,
            "variation evaluation stability",
            max_length=_MAX_STABILITY,
        )
        if self.limitations is not None:
            _text(
                self.limitations,
                "variation evaluation limitations",
                max_length=_MAX_LIMITATIONS,
            )
        covered: set[str] = set()
        vectors: set[str] = set()
        center_key = canonical_parameter_identity(self.center.parameters)
        for index, point in enumerate(self.points):
            diff = _differing_names(self.center.parameters, point.parameters)
            if not diff:
                raise WorkflowError(
                    "variation evaluation points must not repeat the center parameter vector"
                )
            if len(diff) != 1 and frozenset(diff) not in groups:
                raise WorkflowError(
                    f"variation evaluation point[{index}] must differ from the "
                    "center in exactly one parameter or a declared interaction"
                )
            if not diff <= declared:
                raise WorkflowError(
                    f"variation evaluation point[{index}] differs in parameter "
                    f"{sorted(diff - declared)!r} which is not declared in parameters"
                )
            covered.update(diff)
            vector = canonical_parameter_identity(point.parameters)
            if vector in vectors:
                raise WorkflowError(
                    "variation evaluation point parameter vectors must be unique"
                )
            vectors.add(vector)
            if vector == center_key:
                raise WorkflowError(
                    "variation evaluation points must not repeat the center "
                    "parameter vector"
                )
        for name in self.parameters:
            if name not in covered:
                raise WorkflowError(
                    f"variation evaluation parameter {name!r} is not varied by "
                    "any point"
                )
        for group in groups:
            if not any(
                _differing_names(self.center.parameters, point.parameters) == group
                for point in self.points
            ):
                raise WorkflowError(
                    "every declared interaction must be exercised by a joint point"
                )

    def as_dict(self) -> dict[str, object]:
        """Return the portable variation-evaluation representation."""
        return {
            "strategy": dict(self.strategy),
            "batchId": self.batch_id,
            "center": self.center.as_dict(),
            "points": [item.as_dict() for item in self.points],
            "parameters": list(self.parameters),
            "stability": self.stability,
            "limitations": self.limitations,
            "method": self.method,
            "interactions": [list(group) for group in self.interactions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> VariationEvaluation:
        """Rebuild one variation evaluation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("variation evaluation must be an object")
        misses = validate_arguments(VARIATION_PAYLOAD_SCHEMA, payload)
        if misses:
            raise WorkflowError(render_violations(misses))
        strategy = payload.get("strategy")
        if not isinstance(strategy, Mapping):
            raise WorkflowError("variation evaluation requires a strategy receipt")
        center = payload.get("center")
        if not isinstance(center, Mapping):
            raise WorkflowError("variation evaluation requires a center object")
        points = payload.get("points")
        if isinstance(points, str) or not isinstance(points, Sequence):
            raise WorkflowError("variation evaluation requires a points list")
        parameters = payload.get("parameters")
        if isinstance(parameters, str) or not isinstance(parameters, Sequence):
            raise WorkflowError("variation evaluation requires a parameters list")
        return cls(
            strategy=strategy,
            batch_id=_wire_field(payload, "batchId"),
            center=VariationCenter.from_dict(center),
            points=tuple(
                VariationPoint.from_dict(cast(Mapping[str, object], item))
                for item in points
            ),
            parameters=tuple(cast(str, item) for item in parameters),
            stability=_wire_field(payload, "stability"),
            limitations=_wire_field(payload, "limitations"),
            method=cast(str, payload.get("method", "one_at_a_time")),
            interactions=tuple(
                tuple(group)
                for group in cast(
                    Sequence[Sequence[str]], payload.get("interactions", [])
                )
            ),
        )


__all__ = [
    "VARIATION_PAYLOAD_SCHEMA",
    "VARIATION_STATES",
    "VariationCenter",
    "VariationEvaluation",
    "VariationPoint",
    "canonical_parameter_identity",
    "variation_payload_errors",
]
