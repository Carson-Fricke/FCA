"""The deterministic parameter-variation contract for the variation phase.

The variation phase measures a one-at-a-time parameter neighborhood around the
best shortlisted candidate of the accepted candidate evaluation.  The agent
submits one parameter batch for the committed strategy, inspects every point
run, and hands forward the whole evaluation: the strategy receipt, the
deterministic batch identity, the succeeded center with its copied metrics, the
1..16 varied points, the declared parameter names that were actually varied,
and the honest stability and limitations prose.
:meth:`VariationEvaluation.from_dict` is both the rebuild fold's decoder and the
completion validator's drift guard, so a stored or submitted payload either
composes exactly here or is rejected with a
:class:`~.errors.WorkflowError`.

The frozen cross-checks bind the payload to the neighborhood it claims:

- every point differs from the center parameter vector in exactly one key and
  that key is in ``parameters``;
- every name in ``parameters`` is the differing key of at least one point;
- point parameter vectors are unique and none equals the center vector;
- numeric metric values are finite when present and parameter values are
  scalar (string, number, or boolean) and finite.
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

#: The states one submitted variation point may honestly report.
VARIATION_STATES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "running",
    "cancelled",
    "unknown",
)

#: The largest deterministic neighborhood one variation batch may submit.
MAX_VARIATION_POINTS = 16

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


def _finite_number(value: object) -> float | None:
    """Return the value as a finite float, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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


def _parameter_errors(parameters: Mapping[str, object], label: str) -> list[str]:
    """Collect every parameter name or value outside the scalar/finite rule."""
    errors: list[str] = []
    for name, value in parameters.items():
        if not isinstance(name, str) or not name:
            errors.append(f"{label}: parameter names must be non-empty strings")
            continue
        if isinstance(value, bool) or isinstance(value, str):
            continue
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)):
                errors.append(f"{label}.{name}: parameter values must be finite")
            continue
        errors.append(
            f"{label}.{name}: parameter values must be scalar "
            "(string, number, or boolean)"
        )
    return errors


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
    if not all(isinstance(name, str) for name in parameters):
        raise WorkflowError("parameter names must be strings")
    try:
        return json.dumps(
            dict(parameters),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
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
        "points": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_VARIATION_POINTS,
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
    strategy = payload.get("strategy")
    if isinstance(strategy, Mapping) and strategy.get("kind") != "strategy":
        errors.append("variation.strategy.kind must be 'strategy'")

    declared: list[str] = []
    raw_parameters = payload.get("parameters")
    if isinstance(raw_parameters, Sequence) and not isinstance(
        raw_parameters, (str, bytes)
    ):
        for name in raw_parameters:
            if isinstance(name, str) and name:
                declared.append(name)

    center_parameters: Mapping[str, object] = {}
    center_valid = False
    center = payload.get("center")
    if isinstance(center, Mapping):
        raw_center = center.get("parameters")
        if isinstance(raw_center, Mapping):
            center_parameters = raw_center
            center_errors = _parameter_errors(raw_center, "variation.center.parameters")
            errors.extend(center_errors)
            center_valid = not center_errors
        if center.get("state") != "succeeded":
            errors.append("variation.center.state must be 'succeeded'")
        metrics = center.get("metrics")
        if isinstance(metrics, Mapping):
            errors.extend(_metric_errors(metrics, "variation.center.metrics"))
        else:
            errors.append(
                "variation.center.metrics must be an object for the succeeded center"
            )

    points = payload.get("points")
    vectors: set[str] = set()
    center_key = (
        canonical_parameter_identity(center_parameters) if center_valid else None
    )
    covered: set[str] = set()
    if isinstance(points, Sequence) and not isinstance(points, (str, bytes)):
        for index, item in enumerate(points):
            if not isinstance(item, Mapping):
                continue
            label = f"variation.points[{index}]"
            raw_point = item.get("parameters")
            if isinstance(raw_point, Mapping):
                point_errors = _parameter_errors(raw_point, f"{label}.parameters")
                errors.extend(point_errors)
                if not point_errors:
                    if center_valid:
                        diff = _differing_names(center_parameters, raw_point)
                        if len(diff) != 1:
                            errors.append(
                                f"{label} must differ from the center in exactly one "
                                "parameter"
                            )
                        else:
                            key = next(iter(diff))
                            if key in declared:
                                covered.add(key)
                            else:
                                errors.append(
                                    f"{label} differs in parameter {key!r} which is "
                                    "not declared in variation.parameters"
                                )
                    vector = canonical_parameter_identity(raw_point)
                    if vector in vectors:
                        errors.append(
                            "variation.points parameter vectors must be unique"
                        )
                    vectors.add(vector)
                    if vector == center_key:
                        errors.append(
                            "variation.points must not repeat the center "
                            "parameter vector"
                        )
            metrics = item.get("metrics")
            if isinstance(metrics, Mapping):
                errors.extend(_metric_errors(metrics, f"{label}.metrics"))
            elif item.get("state") == "succeeded":
                errors.append(
                    f"{label}.metrics must be an object for a succeeded point"
                )
        if center_valid:
            for name in declared:
                if name not in covered:
                    errors.append(
                        f"variation.parameters name {name!r} is not varied by any point"
                    )
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
        errors = _parameter_errors(self.parameters, "variation point parameters")
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
            errors = _metric_errors(self.metrics, "variation point metrics")
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
        errors = _parameter_errors(self.parameters, "variation center parameters")
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
        errors = _metric_errors(self.metrics, "variation center metrics")
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
        if not 1 <= len(self.points) <= MAX_VARIATION_POINTS:
            raise WorkflowError(
                f"variation evaluation requires 1 to {MAX_VARIATION_POINTS} points"
            )
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
            if len(diff) != 1:
                raise WorkflowError(
                    f"variation evaluation point[{index}] must differ from the "
                    "center in exactly one parameter"
                )
            key = next(iter(diff))
            if key not in declared:
                raise WorkflowError(
                    f"variation evaluation point[{index}] differs in parameter "
                    f"{key!r} which is not declared in parameters"
                )
            covered.add(key)
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
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> VariationEvaluation:
        """Rebuild one variation evaluation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("variation evaluation must be an object")
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
        )


__all__ = [
    "MAX_VARIATION_POINTS",
    "VARIATION_PAYLOAD_SCHEMA",
    "VARIATION_STATES",
    "VariationCenter",
    "VariationEvaluation",
    "VariationPoint",
    "canonical_parameter_identity",
    "variation_payload_errors",
]
