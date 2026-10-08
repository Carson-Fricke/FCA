"""The forward out-of-sample validation contract for the forward phase.

The forward-validation phase re-runs the frozen design — the committed
strategy, the same null-test design, and the same deterministic variation
points — on the protected forward window the phase policy selects, and hands
forward the whole evidence: the strategy receipt, the forward window, the
statistic and direction under test, the observed run, the nested null
replicates with their honest exceedance count and p-value, the nested
variation neighborhood with its stability prose, and the honest
development-versus-forward comparison.  :meth:`ForwardEvaluation.from_dict` is
both the rebuild fold's decoder and the completion validator's drift guard, so
a stored or submitted payload either composes exactly here or is rejected with
a :class:`~.errors.WorkflowError`.

The frozen cross-checks mirror the null-testing and variation contracts
exactly for the nested evidence.  Rather than forking those semantics, this
module composes the accepted :class:`NullTestEvaluation` and
:class:`VariationEvaluation` over the nested evidence (and delegates payload
collection to :func:`null_test_evaluation_payload_errors` and
:func:`variation_payload_errors`), so the two contracts share one truth.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from thesis.agent.runtime.schema import (
    render_violations,
    validate_arguments,
    violations,
)
from thesis.experiments.mandate import MandateError, rfc3339_ns
from thesis.workflows.research_models import PROGRAM_REFERENCE_SCHEMA

from ..errors import WorkflowError
from ..primitives import _text
from .nulls import (
    _OBSERVED_SCHEMA,
    _REPLICATE_SCHEMA,
    MAX_NULL_BLOCK_SIZE,
    MAX_NULL_REPLICATES,
    NULL_DIRECTIONS,
    NULL_TRANSFORM_MODE,
    NullTestEvaluation,
    NullTestObservation,
    NullTestReplicate,
    _strategy_reference_schema,
    null_test_evaluation_payload_errors,
)
from .variation import (
    _CENTER_SCHEMA,
    _POINT_SCHEMA,
    MAX_VARIATION_POINTS,
    VariationCenter,
    VariationEvaluation,
    VariationPoint,
    variation_payload_errors,
)

_MAX_BATCH_ID = 256
_MAX_STATISTIC = 256
_MAX_COMPARISON = 8_192
_MAX_LIMITATIONS = 4_096

#: RFC3339 timestamps with a UTC offset, as committed configurations use them.
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"

_WINDOW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "start": {"type": "string", "pattern": _DATE_PATTERN},
        "end": {"type": "string", "pattern": _DATE_PATTERN},
    },
    "required": ["start", "end"],
}

_NULL_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "batchId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_BATCH_ID,
            "pattern": r"\S",
        },
        "blockSize": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_NULL_BLOCK_SIZE,
        },
        "replicates": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_NULL_REPLICATES,
            "items": _REPLICATE_SCHEMA,
        },
        "exceedanceCount": {"type": "integer", "minimum": 0},
        "pValue": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "batchId",
        "blockSize",
        "replicates",
        "exceedanceCount",
        "pValue",
    ],
}

_VARIATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
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
                "maxLength": 128,
                "pattern": r"\S",
            },
        },
        "stability": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4_096,
            "pattern": r"\S",
        },
    },
    "required": ["batchId", "center", "points", "parameters", "stability"],
}

#: The advertised wire shape of one forward-validation payload.
FORWARD_PAYLOAD_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "strategy": _strategy_reference_schema(),
        "window": _WINDOW_SCHEMA,
        "statistic": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_STATISTIC,
            "pattern": r"\S",
        },
        "direction": {"type": "string", "enum": list(NULL_DIRECTIONS)},
        "observed": _OBSERVED_SCHEMA,
        "null": _NULL_SCHEMA,
        "variation": _VARIATION_SCHEMA,
        "comparison": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_COMPARISON,
            "pattern": r"\S",
        },
        "limitations": {"type": ["string", "null"], "maxLength": _MAX_LIMITATIONS},
    },
    "required": [
        "strategy",
        "window",
        "statistic",
        "direction",
        "observed",
        "null",
        "variation",
        "comparison",
    ],
}


def _field(payload: Mapping[str, object], *keys: str) -> Any:
    """Return the first present spelling of one wire field."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _window_errors(window: Mapping[str, object]) -> list[str]:
    """Collect malformed or inverted forward-window bounds."""
    errors: list[str] = []
    try:
        start_ns = rfc3339_ns(cast(str, window.get("start")))
        end_ns = rfc3339_ns(cast(str, window.get("end")))
    except (MandateError, TypeError):
        return ["forwardValidation.window must name RFC3339 UTC bounds"]
    if start_ns >= end_ns:
        errors.append(
            "forwardValidation.window.start must precede forwardValidation.window.end"
        )
    return errors


def forward_payload_errors(payload: Mapping[str, object]) -> list[str]:
    """Collect structural misses and the frozen forward cross-checks.

    The nested null and variation evidence is validated by the same
    collectors the null-testing and variation phases use, with their report
    labels rewritten to the forward paths, so every contract keeps one truth.
    """
    if not isinstance(payload, Mapping):
        return ["forwardValidation must be an object"]
    errors = [
        miss.render()
        for miss in violations(FORWARD_PAYLOAD_SCHEMA, payload, "forwardValidation")
    ]
    strategy = payload.get("strategy")
    if isinstance(strategy, Mapping) and strategy.get("kind") != "strategy":
        errors.append("forwardValidation.strategy.kind must be 'strategy'")
    window = payload.get("window")
    if isinstance(window, Mapping):
        errors.extend(_window_errors(window))
    null = payload.get("null")
    if isinstance(null, Mapping):
        nested = {
            "strategy": strategy,
            "batchId": null.get("batchId"),
            "mode": NULL_TRANSFORM_MODE,
            "blockSize": null.get("blockSize"),
            "statistic": payload.get("statistic"),
            "direction": payload.get("direction"),
            "observed": payload.get("observed"),
            "replicates": null.get("replicates"),
            "exceedanceCount": null.get("exceedanceCount"),
            "pValue": null.get("pValue"),
        }
        errors.extend(
            error.replace("nullTesting", "forwardValidation.null", 1)
            for error in null_test_evaluation_payload_errors(nested)
        )
    variation = payload.get("variation")
    if isinstance(variation, Mapping):
        nested_variation = {
            "strategy": strategy,
            "batchId": variation.get("batchId"),
            "center": variation.get("center"),
            "points": variation.get("points"),
            "parameters": variation.get("parameters"),
            "stability": variation.get("stability"),
        }
        errors.extend(
            error.replace("variation", "forwardValidation.variation", 1)
            for error in variation_payload_errors(nested_variation)
        )
    return errors


@dataclass(frozen=True, slots=True)
class ForwardWindow:
    """The forward partition window one validation actually ran on."""

    start: str
    end: str

    def __post_init__(self) -> None:
        _text(self.start, "forward window start", max_length=64)
        _text(self.end, "forward window end", max_length=64)
        try:
            start_ns = rfc3339_ns(self.start)
            end_ns = rfc3339_ns(self.end)
        except MandateError as error:
            raise WorkflowError(
                "forward window bounds must be RFC3339 UTC timestamps"
            ) from error
        if start_ns >= end_ns:
            raise WorkflowError("forward window start must precede its end")

    def as_dict(self) -> dict[str, object]:
        """Return the portable window representation."""
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ForwardWindow:
        """Rebuild one window from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("forward window must be an object")
        return cls(
            start=_field(payload, "start"),
            end=_field(payload, "end"),
        )


@dataclass(frozen=True, slots=True)
class ForwardNullEvidence:
    """The block-permutation null design re-run on the forward window."""

    batch_id: str
    block_size: int
    replicates: tuple[NullTestReplicate, ...]
    exceedance_count: int
    p_value: float

    def __post_init__(self) -> None:
        _text(
            self.batch_id,
            "forward null evidence batch_id",
            max_length=_MAX_BATCH_ID,
        )
        if type(self.block_size) is not int or not (
            1 <= self.block_size <= MAX_NULL_BLOCK_SIZE
        ):
            raise WorkflowError(
                "forward null evidence block_size must be an integer from 1 to "
                f"{MAX_NULL_BLOCK_SIZE}"
            )
        if isinstance(self.replicates, str) or not isinstance(self.replicates, tuple):
            raise WorkflowError("forward null evidence replicates must be a tuple")
        if not 1 <= len(self.replicates) <= MAX_NULL_REPLICATES:
            raise WorkflowError(
                f"forward null evidence requires 1 to {MAX_NULL_REPLICATES} replicates"
            )
        if any(not isinstance(item, NullTestReplicate) for item in self.replicates):
            raise WorkflowError(
                "forward null evidence replicates must be NullTestReplicate values"
            )
        seeds = [item.seed for item in self.replicates]
        if len(set(seeds)) != len(seeds):
            raise WorkflowError("forward null evidence replicate seeds must be unique")
        if type(self.exceedance_count) is not int or self.exceedance_count < 0:
            raise WorkflowError(
                "forward null evidence exceedance_count must be an integer of at "
                "least 0"
            )
        p_value = self.p_value
        if isinstance(p_value, bool) or not isinstance(p_value, (int, float)):
            raise WorkflowError("forward null evidence p_value must be a finite number")
        number = float(p_value)
        if not math.isfinite(number):
            raise WorkflowError("forward null evidence p_value must be a finite number")
        object.__setattr__(self, "p_value", number)

    def as_dict(self) -> dict[str, object]:
        """Return the portable null-evidence representation."""
        return {
            "batchId": self.batch_id,
            "blockSize": self.block_size,
            "replicates": [item.as_dict() for item in self.replicates],
            "exceedanceCount": self.exceedance_count,
            "pValue": self.p_value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ForwardNullEvidence:
        """Rebuild one null evidence object from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("forward null evidence must be an object")
        replicates = payload.get("replicates")
        if isinstance(replicates, str) or not isinstance(replicates, Sequence):
            raise WorkflowError("forward null evidence requires a replicates list")
        return cls(
            batch_id=_field(payload, "batchId", "batch_id"),
            block_size=_field(payload, "blockSize", "block_size"),
            replicates=tuple(
                NullTestReplicate.from_dict(cast(Mapping[str, object], item))
                for item in replicates
            ),
            exceedance_count=_field(payload, "exceedanceCount", "exceedance_count"),
            p_value=_field(payload, "pValue", "p_value"),
        )


@dataclass(frozen=True, slots=True)
class ForwardVariationEvidence:
    """The deterministic variation neighborhood re-run on the forward window."""

    batch_id: str
    center: VariationCenter
    points: tuple[VariationPoint, ...]
    parameters: tuple[str, ...]
    stability: str

    def __post_init__(self) -> None:
        _text(
            self.batch_id,
            "forward variation evidence batch_id",
            max_length=_MAX_BATCH_ID,
        )
        if not isinstance(self.center, VariationCenter):
            raise WorkflowError(
                "forward variation evidence center must be a VariationCenter"
            )
        if isinstance(self.points, (str, bytes)) or not isinstance(self.points, tuple):
            raise WorkflowError("forward variation evidence points must be a tuple")
        if not 1 <= len(self.points) <= MAX_VARIATION_POINTS:
            raise WorkflowError(
                "forward variation evidence requires 1 to "
                f"{MAX_VARIATION_POINTS} points"
            )
        if any(not isinstance(item, VariationPoint) for item in self.points):
            raise WorkflowError(
                "forward variation evidence points must be VariationPoint values"
            )
        if isinstance(self.parameters, (str, bytes)) or not isinstance(
            self.parameters, tuple
        ):
            raise WorkflowError("forward variation evidence parameters must be a tuple")
        declared: set[str] = set()
        for name in self.parameters:
            _text(name, "forward variation evidence parameter name", max_length=128)
            if name in declared:
                raise WorkflowError(
                    "forward variation evidence parameter names must be unique"
                )
            declared.add(name)
        if not declared:
            raise WorkflowError(
                "forward variation evidence parameters must declare at least one name"
            )
        _text(
            self.stability,
            "forward variation evidence stability",
            max_length=4_096,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable variation-evidence representation."""
        return {
            "batchId": self.batch_id,
            "center": self.center.as_dict(),
            "points": [item.as_dict() for item in self.points],
            "parameters": list(self.parameters),
            "stability": self.stability,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ForwardVariationEvidence:
        """Rebuild one variation evidence object from its portable form."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("forward variation evidence must be an object")
        center = payload.get("center")
        if not isinstance(center, Mapping):
            raise WorkflowError("forward variation evidence requires a center object")
        points = payload.get("points")
        if isinstance(points, str) or not isinstance(points, Sequence):
            raise WorkflowError("forward variation evidence requires a points list")
        parameters = payload.get("parameters")
        if isinstance(parameters, str) or not isinstance(parameters, Sequence):
            raise WorkflowError("forward variation evidence requires a parameters list")
        return cls(
            batch_id=_field(payload, "batchId", "batch_id"),
            center=VariationCenter.from_dict(center),
            points=tuple(
                VariationPoint.from_dict(cast(Mapping[str, object], item))
                for item in points
            ),
            parameters=tuple(cast(str, item) for item in parameters),
            stability=_field(payload, "stability"),
        )


@dataclass(frozen=True, slots=True)
class ForwardEvaluation:
    """The accepted forward validation: frozen design re-run on the holdout."""

    strategy: Mapping[str, object]
    window: ForwardWindow
    statistic: str
    direction: str
    observed: NullTestObservation
    null: ForwardNullEvidence
    variation: ForwardVariationEvidence
    comparison: str
    limitations: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, Mapping):
            raise WorkflowError("forward evaluation strategy must be an object")
        misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, self.strategy)
        if misses:
            raise WorkflowError(
                "invalid forward evaluation strategy: " + render_violations(misses)
            )
        if self.strategy.get("kind") != "strategy":
            raise WorkflowError("forward evaluation must reference a strategy program")
        object.__setattr__(self, "strategy", dict(self.strategy))
        if not isinstance(self.window, ForwardWindow):
            raise WorkflowError("forward evaluation window must be a ForwardWindow")
        _text(self.statistic, "forward evaluation statistic", max_length=_MAX_STATISTIC)
        if self.direction not in NULL_DIRECTIONS:
            raise WorkflowError(
                "forward evaluation direction must be one of "
                f"{', '.join(NULL_DIRECTIONS)}"
            )
        if not isinstance(self.observed, NullTestObservation):
            raise WorkflowError(
                "forward evaluation observed must be a NullTestObservation"
            )
        if not isinstance(self.null, ForwardNullEvidence):
            raise WorkflowError("forward evaluation null must be ForwardNullEvidence")
        if not isinstance(self.variation, ForwardVariationEvidence):
            raise WorkflowError(
                "forward evaluation variation must be ForwardVariationEvidence"
            )
        _text(
            self.comparison,
            "forward evaluation comparison",
            max_length=_MAX_COMPARISON,
        )
        if self.limitations is not None:
            _text(
                self.limitations,
                "forward evaluation limitations",
                max_length=_MAX_LIMITATIONS,
            )
        # Reuse the null-testing and variation semantics for the nested
        # evidence: composing the accepted evaluations here runs every frozen
        # cross-check (observed value, replicate statistic, exceedance count,
        # p-value, unique seeds, succeeded coverage, one-key-diff, declared
        # coverage, unique vectors) under one truth.
        NullTestEvaluation(
            strategy=self.strategy,
            batch_id=self.null.batch_id,
            mode=NULL_TRANSFORM_MODE,
            block_size=self.null.block_size,
            statistic=self.statistic,
            direction=self.direction,
            observed=self.observed,
            replicates=self.null.replicates,
            exceedance_count=self.null.exceedance_count,
            p_value=self.null.p_value,
        )
        VariationEvaluation(
            strategy=self.strategy,
            batch_id=self.variation.batch_id,
            center=self.variation.center,
            points=self.variation.points,
            parameters=self.variation.parameters,
            stability=self.variation.stability,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable forward-evaluation representation."""
        return {
            "strategy": dict(self.strategy),
            "window": self.window.as_dict(),
            "statistic": self.statistic,
            "direction": self.direction,
            "observed": self.observed.as_dict(),
            "null": self.null.as_dict(),
            "variation": self.variation.as_dict(),
            "comparison": self.comparison,
            "limitations": self.limitations,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ForwardEvaluation:
        """Rebuild one forward evaluation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("forward evaluation must be an object")
        strategy = payload.get("strategy")
        if not isinstance(strategy, Mapping):
            raise WorkflowError("forward evaluation requires a strategy receipt")
        window = payload.get("window")
        if not isinstance(window, Mapping):
            raise WorkflowError("forward evaluation requires a window object")
        observed = payload.get("observed")
        if not isinstance(observed, Mapping):
            raise WorkflowError("forward evaluation requires an observed object")
        null = payload.get("null")
        if not isinstance(null, Mapping):
            raise WorkflowError("forward evaluation requires a null object")
        variation = payload.get("variation")
        if not isinstance(variation, Mapping):
            raise WorkflowError("forward evaluation requires a variation object")
        return cls(
            strategy=strategy,
            window=ForwardWindow.from_dict(window),
            statistic=_field(payload, "statistic"),
            direction=_field(payload, "direction"),
            observed=NullTestObservation.from_dict(observed),
            null=ForwardNullEvidence.from_dict(null),
            variation=ForwardVariationEvidence.from_dict(variation),
            comparison=_field(payload, "comparison"),
            limitations=_field(payload, "limitations"),
        )


__all__ = [
    "FORWARD_PAYLOAD_SCHEMA",
    "ForwardEvaluation",
    "ForwardNullEvidence",
    "ForwardVariationEvidence",
    "ForwardWindow",
    "forward_payload_errors",
]
