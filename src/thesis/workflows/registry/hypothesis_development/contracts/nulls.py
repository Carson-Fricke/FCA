"""Explicit signal-destruction methods and conditional null evidence.

The complete fixed seed plan has no universal ceiling. Supported method claims
are checked against ordinary or block permutation. All unsuccessful replicates
count conservatively in p-values; selection-conditional evidence cannot be
misrepresented as a search-adjusted test. The typed decoder owns all semantic
cross-checks for completion and durable replay.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from thesis.agent.runtime.schema import (
    render_violations,
    validate_arguments,
    violations,
)
from thesis.research.evidence import metric_errors, numeric_metric
from thesis.workflows.research_models import PROGRAM_REFERENCE_SCHEMA

from ..errors import WorkflowError
from ..primitives import _text

#: The states one submitted null replicate may honestly report.
NULL_REPLICATE_STATES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "running",
    "cancelled",
    "unknown",
)

#: The directions an extremeness comparison may take.
NULL_DIRECTIONS: tuple[str, ...] = ("upper", "lower")

#: Explicitly supported frozen null transform modes.
NULL_TRANSFORM_MODES = ("permutation", "block_permutation")

#: Replicate and block-size bounds mirrored from the workspace service.
MAX_NULL_BLOCK_SIZE = 10_000

#: p-value equality tolerance required by the frozen contract.
_PVALUE_TOLERANCE = 1e-9

_MAX_JOB_ID = 256
_MAX_BATCH_ID = 256
_MAX_STATISTIC = 256
_MAX_ERROR = 8_192
_MAX_LIMITATIONS = 4_096


def _field(payload: Mapping[str, object], key: str) -> Any:
    """Read one canonical wire field for typed contract construction."""
    return payload.get(key)


def _finite_number(value: object) -> float | None:
    """Return the value as a finite float, or ``None`` when it is not one."""
    return numeric_metric(value)


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


_OBSERVED_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "metrics": {"type": "object"},
        "value": {"type": "number"},
    },
    "required": ["jobId", "metrics", "value"],
}

_REPLICATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "seed": {"type": "integer", "minimum": 0},
        "jobId": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_JOB_ID,
            "pattern": r"\S",
        },
        "state": {"type": "string", "enum": list(NULL_REPLICATE_STATES)},
        "metrics": {"type": ["object", "null"]},
        "error": {"type": ["string", "null"], "maxLength": _MAX_ERROR},
    },
    "required": ["seed", "jobId", "state"],
}

#: The advertised wire shape of one null-test evaluation payload.
NULL_TEST_EVALUATION_PAYLOAD_SCHEMA: dict[str, object] = {
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
        "mode": {"type": "string", "enum": list(NULL_TRANSFORM_MODES)},
        "evidenceScope": {"type": "string", "enum": ["selection_conditional"]},
        "failedReplicatePolicy": {
            "type": "string",
            "enum": ["conservative_exceedance"],
        },
        "targetResolution": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "signalAdequacy": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "signal": {"type": "string", "minLength": 1},
                "destroyedDependency": {
                    "type": "string",
                    "enum": ["serial_order", "inter_block_order"],
                },
                "preservedProperties": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": ["marginal_returns", "within_block_dependence"],
                    },
                },
                "limitations": {"type": "string", "minLength": 1},
            },
            "required": [
                "signal",
                "destroyedDependency",
                "preservedProperties",
                "limitations",
            ],
        },
        "blockSize": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_NULL_BLOCK_SIZE,
        },
        "statistic": {
            "type": "string",
            "minLength": 1,
            "maxLength": _MAX_STATISTIC,
            "pattern": r"\S",
        },
        "direction": {"type": "string", "enum": list(NULL_DIRECTIONS)},
        "observed": _OBSERVED_SCHEMA,
        "replicates": {
            "type": "array",
            "minItems": 1,
            "items": _REPLICATE_SCHEMA,
        },
        "exceedanceCount": {"type": "integer", "minimum": 0},
        "pValue": {"type": "number", "minimum": 0, "maximum": 1},
        "limitations": {
            "type": ["string", "null"],
            "maxLength": _MAX_LIMITATIONS,
        },
    },
    "required": [
        "strategy",
        "batchId",
        "mode",
        "blockSize",
        "statistic",
        "direction",
        "observed",
        "replicates",
        "exceedanceCount",
        "pValue",
        "evidenceScope",
        "failedReplicatePolicy",
        "signalAdequacy",
        "targetResolution",
    ],
}


def null_test_evaluation_payload_errors(
    payload: Mapping[str, object],
) -> list[str]:
    """Collect structural misses and the null-test cross-checks."""
    if not isinstance(payload, Mapping):
        return ["nullTesting must be an object"]
    errors = [
        miss.render()
        for miss in violations(
            NULL_TEST_EVALUATION_PAYLOAD_SCHEMA, payload, "nullTesting"
        )
    ]
    if errors:
        return errors
    try:
        NullTestEvaluation.from_dict(payload)
    except WorkflowError as error:
        errors.append(str(error))
    return errors


@dataclass(frozen=True, slots=True)
class NullTestObservation:
    """The observed candidate run and the statistic under test."""

    job_id: str
    metrics: Mapping[str, object]
    value: float

    def __post_init__(self) -> None:
        _text(self.job_id, "null test observation job_id", max_length=_MAX_JOB_ID)
        if not isinstance(self.metrics, Mapping):
            raise WorkflowError("null test observation metrics must be an object")
        errors = metric_errors(self.metrics, "null test observation metrics")
        if errors:
            raise WorkflowError("; ".join(errors))
        object.__setattr__(self, "metrics", dict(self.metrics))
        number = _finite_number(self.value)
        if number is None:
            raise WorkflowError("null test observation value must be a finite number")
        object.__setattr__(self, "value", number)

    def as_dict(self) -> dict[str, object]:
        """Return the portable observation representation."""
        return {
            "jobId": self.job_id,
            "metrics": dict(self.metrics),
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NullTestObservation:
        """Rebuild one observation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("null test observation must be an object")
        metrics = payload.get("metrics")
        if not isinstance(metrics, Mapping):
            raise WorkflowError("null test observation requires a metrics object")
        return cls(
            job_id=_field(payload, "jobId"),
            metrics=metrics,
            value=_field(payload, "value"),
        )


@dataclass(frozen=True, slots=True)
class NullTestReplicate:
    """One null replicate with its honest run state and recorded metrics."""

    seed: int
    job_id: str
    state: str
    metrics: Mapping[str, object] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if type(self.seed) is not int or self.seed < 0:
            raise WorkflowError(
                "null test replicate seed must be an integer of at least 0"
            )
        _text(self.job_id, "null test replicate job_id", max_length=_MAX_JOB_ID)
        if self.state not in NULL_REPLICATE_STATES:
            raise WorkflowError(
                "null test replicate state must be one of "
                f"{', '.join(NULL_REPLICATE_STATES)}"
            )
        if self.metrics is not None:
            if not isinstance(self.metrics, Mapping):
                raise WorkflowError("null test replicate metrics must be an object")
            errors = metric_errors(self.metrics, "null test replicate metrics")
            if errors:
                raise WorkflowError("; ".join(errors))
            object.__setattr__(self, "metrics", dict(self.metrics))
        if self.error is not None:
            _text(self.error, "null test replicate error", max_length=_MAX_ERROR)

    def as_dict(self) -> dict[str, object]:
        """Return the portable replicate representation."""
        return {
            "seed": self.seed,
            "jobId": self.job_id,
            "state": self.state,
            "metrics": None if self.metrics is None else dict(self.metrics),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NullTestReplicate:
        """Rebuild one replicate from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("null test replicate must be an object")
        metrics = payload.get("metrics")
        if metrics is not None and not isinstance(metrics, Mapping):
            raise WorkflowError("null test replicate metrics must be an object")
        error = _field(payload, "error")
        if error is not None and not isinstance(error, str):
            raise WorkflowError("null test replicate error must be text")
        return cls(
            seed=_field(payload, "seed"),
            job_id=_field(payload, "jobId"),
            state=_field(payload, "state"),
            metrics=metrics,
            error=error,
        )


@dataclass(frozen=True, slots=True)
class NullTestEvaluation:
    """The accepted null test: observed run, replicates, and its statistics."""

    strategy: Mapping[str, object]
    batch_id: str
    mode: str
    block_size: int
    statistic: str
    direction: str
    observed: NullTestObservation
    replicates: tuple[NullTestReplicate, ...]
    exceedance_count: int
    p_value: float
    limitations: str | None = None
    signal_adequacy: Mapping[str, object] | None = None
    evidence_scope: str = "selection_conditional"
    failed_replicate_policy: str = "conservative_exceedance"
    target_resolution: float = 0.05

    def __post_init__(self) -> None:
        if not isinstance(self.signal_adequacy, Mapping):
            raise WorkflowError(
                "signalAdequacy must explicitly declare the tested signal dependency"
            )
        adequacy_schema = cast(
            Mapping[str, object], NULL_TEST_EVALUATION_PAYLOAD_SCHEMA["properties"]
        )["signalAdequacy"]
        misses = validate_arguments(
            cast(Mapping[str, object], adequacy_schema), self.signal_adequacy
        )
        if misses:
            raise WorkflowError("invalid signalAdequacy: " + render_violations(misses))
        if self.evidence_scope != "selection_conditional":
            raise WorkflowError(
                "only selection_conditional evidence is supported; the search is not replayed under the null"
            )
        if self.failed_replicate_policy != "conservative_exceedance":
            raise WorkflowError("failedReplicatePolicy must be conservative_exceedance")
        if not isinstance(self.strategy, Mapping):
            raise WorkflowError("null test evaluation strategy must be an object")
        misses = validate_arguments(PROGRAM_REFERENCE_SCHEMA, self.strategy)
        if misses:
            raise WorkflowError(
                "invalid null test evaluation strategy: " + render_violations(misses)
            )
        if self.strategy.get("kind") != "strategy":
            raise WorkflowError(
                "null test evaluation must reference a strategy program"
            )
        object.__setattr__(self, "strategy", dict(self.strategy))
        _text(self.batch_id, "null test evaluation batch_id", max_length=_MAX_BATCH_ID)
        if self.mode not in NULL_TRANSFORM_MODES:
            raise WorkflowError(
                "null test evaluation mode must be permutation or block_permutation"
            )
        if self.mode == "permutation" and self.block_size != 1:
            raise WorkflowError("permutation requires block_size=1")
        if type(self.block_size) is not int or not (
            1 <= self.block_size <= MAX_NULL_BLOCK_SIZE
        ):
            raise WorkflowError(
                "null test evaluation block_size must be an integer from 1 to "
                f"{MAX_NULL_BLOCK_SIZE}"
            )
        adequacy = self.signal_adequacy
        dependency = "inter_block_order" if self.block_size > 1 else "serial_order"
        if adequacy.get("destroyedDependency") != dependency:
            raise WorkflowError(
                f"this transform destroys {dependency}; the declared signal dependency is unsupported"
            )
        if (
            "within_block_dependence"
            in cast(Sequence[str], adequacy.get("preservedProperties", []))
            and self.block_size == 1
        ):
            raise WorkflowError(
                "within_block_dependence requires block_permutation with blockSize greater than one"
            )
        _text(
            self.statistic, "null test evaluation statistic", max_length=_MAX_STATISTIC
        )
        if self.direction not in NULL_DIRECTIONS:
            raise WorkflowError(
                "null test evaluation direction must be one of "
                f"{', '.join(NULL_DIRECTIONS)}"
            )
        if not isinstance(self.observed, NullTestObservation):
            raise WorkflowError(
                "null test evaluation observed must be a NullTestObservation"
            )
        if isinstance(self.replicates, str) or not isinstance(self.replicates, tuple):
            raise WorkflowError("null test evaluation replicates must be a tuple")
        if not len(self.replicates):
            raise WorkflowError("null test evaluation requires at least one replicate")
        resolution = _finite_number(self.target_resolution)
        if resolution is None or not 0 < resolution <= 1:
            raise WorkflowError("targetResolution must be a finite number in (0, 1]")
        if 1 / (len(self.replicates) + 1) > resolution + _PVALUE_TOLERANCE:
            raise WorkflowError(
                "declared replicates cannot achieve targetResolution; extend the frozen seed plan"
            )
        if any(not isinstance(item, NullTestReplicate) for item in self.replicates):
            raise WorkflowError(
                "null test evaluation replicates must be NullTestReplicate values"
            )
        seeds = [item.seed for item in self.replicates]
        if len(set(seeds)) != len(seeds):
            raise WorkflowError("null test evaluation replicate seeds must be unique")
        observed_metric = _finite_number(self.observed.metrics.get(self.statistic))
        if observed_metric is None:
            raise WorkflowError(
                "null test evaluation observed metrics must contain a finite number "
                f"at {self.statistic!r}"
            )
        if observed_metric != self.observed.value:
            raise WorkflowError(
                "null test evaluation observed value must equal "
                "observed.metrics[statistic]"
            )
        succeeded: list[float] = []
        for index, replicate in enumerate(self.replicates):
            if replicate.state != "succeeded":
                continue
            metrics = replicate.metrics
            if not isinstance(metrics, Mapping):
                raise WorkflowError(
                    f"null test evaluation replicate[{index}] metrics must be an "
                    "object for a succeeded replicate"
                )
            metric = _finite_number(metrics.get(self.statistic))
            if metric is None:
                raise WorkflowError(
                    f"null test evaluation replicate[{index}] metrics must contain "
                    f"a finite number at {self.statistic!r}"
                )
            succeeded.append(metric)
        if not succeeded:
            raise WorkflowError(
                "null test evaluation requires at least one succeeded replicate"
            )
        if type(self.exceedance_count) is not int or self.exceedance_count < 0:
            raise WorkflowError(
                "null test evaluation exceedance_count must be an integer of at least 0"
            )
        computed = sum(
            1
            for metric in succeeded
            if (
                metric >= self.observed.value
                if self.direction == "upper"
                else metric <= self.observed.value
            )
        )
        if self.exceedance_count != computed:
            raise WorkflowError(
                "null test evaluation exceedance_count must equal the extremeness "
                "count over the succeeded replicates"
            )
        p_value = _finite_number(self.p_value)
        if p_value is None:
            raise WorkflowError("null test evaluation p_value must be a finite number")
        unsuccessful = len(self.replicates) - len(succeeded)
        if (
            abs(p_value - (1 + computed + unsuccessful) / (1 + len(self.replicates)))
            > _PVALUE_TOLERANCE
        ):
            raise WorkflowError(
                "null test evaluation p_value must equal (1 + exceedance_count + unsuccessful) / "
                "(1 + declared replicates) under conservative_exceedance"
            )
        object.__setattr__(self, "p_value", p_value)
        if self.limitations is not None:
            _text(
                self.limitations,
                "null test evaluation limitations",
                max_length=_MAX_LIMITATIONS,
            )

    def as_dict(self) -> dict[str, object]:
        """Return the portable null-test-evaluation representation."""
        return {
            "strategy": dict(self.strategy),
            "batchId": self.batch_id,
            "mode": self.mode,
            "blockSize": self.block_size,
            "statistic": self.statistic,
            "direction": self.direction,
            "observed": self.observed.as_dict(),
            "replicates": [item.as_dict() for item in self.replicates],
            "exceedanceCount": self.exceedance_count,
            "pValue": self.p_value,
            "limitations": self.limitations,
            "signalAdequacy": dict(self.signal_adequacy or {}),
            "evidenceScope": self.evidence_scope,
            "failedReplicatePolicy": self.failed_replicate_policy,
            "targetResolution": self.target_resolution,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NullTestEvaluation:
        """Rebuild one null test evaluation from its portable representation."""
        if not isinstance(payload, Mapping):
            raise WorkflowError("null test evaluation must be an object")
        misses = validate_arguments(NULL_TEST_EVALUATION_PAYLOAD_SCHEMA, payload)
        if misses:
            raise WorkflowError(render_violations(misses))
        strategy = payload.get("strategy")
        if not isinstance(strategy, Mapping):
            raise WorkflowError("null test evaluation requires a strategy receipt")
        observed = payload.get("observed")
        if not isinstance(observed, Mapping):
            raise WorkflowError("null test evaluation requires an observed object")
        replicates = payload.get("replicates")
        if isinstance(replicates, str) or not isinstance(replicates, Sequence):
            raise WorkflowError("null test evaluation requires a replicates list")
        return cls(
            strategy=strategy,
            batch_id=_field(payload, "batchId"),
            mode=_field(payload, "mode"),
            block_size=_field(payload, "blockSize"),
            statistic=_field(payload, "statistic"),
            direction=_field(payload, "direction"),
            observed=NullTestObservation.from_dict(observed),
            replicates=tuple(
                NullTestReplicate.from_dict(cast(Mapping[str, object], item))
                for item in replicates
            ),
            exceedance_count=_field(payload, "exceedanceCount"),
            p_value=_field(payload, "pValue"),
            limitations=_field(payload, "limitations"),
            signal_adequacy=cast(Mapping[str, object], payload.get("signalAdequacy")),
            evidence_scope=cast(str, payload.get("evidenceScope")),
            failed_replicate_policy=cast(str, payload.get("failedReplicatePolicy")),
            target_resolution=cast(float, payload.get("targetResolution")),
        )


__all__ = [
    "MAX_NULL_BLOCK_SIZE",
    "NULL_DIRECTIONS",
    "NULL_REPLICATE_STATES",
    "NULL_TEST_EVALUATION_PAYLOAD_SCHEMA",
    "NullTestEvaluation",
    "NullTestObservation",
    "NullTestReplicate",
    "null_test_evaluation_payload_errors",
]
