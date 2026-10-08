"""The falsification evidence envelope.

The trials phase emits one portable envelope carrying only what the runs and
the accepted plan actually produced: the accepted study reference, the model
and fit identities, the finite fitted parameters, the runtime configuration,
the planned tests and controls, one assumption record per recorded trial, and
the canonical report digest.  :meth:`ResearchEvidence.from_dict` is both the
rebuild fold's decoder and the completion validator's drift guard, so the
envelope that advances the live workflow is exactly the envelope replay
restores.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from thesis.research.hypothesis import FALSIFIER_CONDITIONS, FalsifierRole
from thesis.research.models import (
    FittedModelRef,
    ResearchModelError,
    ResearchModelRef,
)
from thesis.workflows.research_models import StudyProgramReference

from ..errors import WorkflowError
from ..primitives import (
    _DIGEST,
    _identifier,
    _text,
)
from .plan import FalsificationPlan, FalsifierTest, PlaceboControl

_MAX_EVIDENCE_TEXT = 8_192
_MAX_USAGE_TEXT = 4_096
_MAX_ARTIFACT_TEXT = 1_024

_FALSIFIER_ROLES = frozenset(role.value for role in FalsifierRole)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{label} must be an object")
    return value


def _records(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise WorkflowError(f"{label} must be a list")
    return tuple(_mapping(item, label) for item in value)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise WorkflowError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise WorkflowError(f"{label} must be a finite number")
    return number


def _optional_text(value: object, label: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    return _text(value, label, max_length=max_length)


@dataclass(frozen=True, slots=True)
class AssumptionEvidence:
    """One recorded trial bound to the falsifier it measured."""

    falsifier_id: str
    statement: str
    role: str
    observable: str
    condition: str
    threshold: float
    test: str
    observed: float | None
    falsified: bool
    run_state: str
    detail: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.falsifier_id, "assumption falsifier_id")
        _text(self.statement, "assumption statement", max_length=_MAX_EVIDENCE_TEXT)
        if self.role not in _FALSIFIER_ROLES:
            raise WorkflowError(
                f"assumption role must be one of {sorted(_FALSIFIER_ROLES)}"
            )
        _identifier(self.observable, "assumption observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"assumption condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        object.__setattr__(
            self, "threshold", _finite(self.threshold, "assumption threshold")
        )
        _identifier(self.test, "assumption test")
        if self.observed is not None:
            object.__setattr__(
                self, "observed", _finite(self.observed, "assumption observed")
            )
        if not isinstance(self.falsified, bool):
            raise WorkflowError("assumption falsified must be a boolean")
        _text(self.run_state, "assumption run_state", max_length=64)
        if self.detail is not None:
            _text(self.detail, "assumption detail", max_length=_MAX_USAGE_TEXT)

    def as_dict(self) -> dict[str, object]:
        """Return the portable assumption evidence representation."""
        return {
            "condition": self.condition,
            "detail": self.detail,
            "falsified": self.falsified,
            "falsifierId": self.falsifier_id,
            "observable": self.observable,
            "observed": self.observed,
            "role": self.role,
            "runState": self.run_state,
            "statement": self.statement,
            "test": self.test,
            "threshold": self.threshold,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AssumptionEvidence:
        """Rebuild one assumption record from its portable representation."""
        payload = _mapping(payload, "assumption evidence")
        return cls(
            falsifier_id=cast(str, payload.get("falsifierId")),
            statement=cast(str, payload.get("statement")),
            role=cast(str, payload.get("role")),
            observable=cast(str, payload.get("observable")),
            condition=cast(str, payload.get("condition")),
            threshold=cast(float, payload.get("threshold")),
            test=cast(str, payload.get("test")),
            observed=cast("float | None", payload.get("observed")),
            falsified=cast(bool, payload.get("falsified")),
            run_state=cast(str, payload.get("runState")),
            detail=cast("str | None", payload.get("detail")),
        )


@dataclass(frozen=True, slots=True)
class ControlEvidence:
    """One planned negative control the verdict must be able to read."""

    control_id: str
    description: str
    expectation: str

    def __post_init__(self) -> None:
        _identifier(self.control_id, "control_id")
        _text(self.description, "control description", max_length=2_048)
        _text(self.expectation, "control expectation", max_length=2_048)

    def as_dict(self) -> dict[str, object]:
        """Return the portable control evidence representation."""
        return {
            "controlId": self.control_id,
            "description": self.description,
            "expectation": self.expectation,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ControlEvidence:
        """Rebuild one control record from its portable representation."""
        payload = _mapping(payload, "control evidence")
        return cls(
            control_id=cast(str, payload.get("controlId")),
            description=cast(str, payload.get("description")),
            expectation=cast(str, payload.get("expectation")),
        )


def _study_dict(study: StudyProgramReference) -> dict[str, object]:
    return {
        "workspaceId": study.workspace_id,
        "kind": "study",
        "programId": study.program_id,
        "gitCommit": study.git_commit,
        "version": study.version,
        "description": study.description,
        "outputs": list(study.outputs),
        "tests": dict(study.tests),
    }


def _model_dict(model: ResearchModelRef) -> dict[str, object]:
    return {
        "workspaceId": model.workspace_id,
        "modelId": model.model_id,
        "contentSha256": model.content_sha256,
    }


def _fit_dict(fit: FittedModelRef) -> dict[str, object]:
    return {
        "workspaceId": fit.workspace_id,
        "modelId": fit.model_id,
        "modelContentSha256": fit.model_content_sha256,
        "fitSha256": fit.fit_sha256,
    }


def _plan_dict(plan: FalsificationPlan) -> dict[str, object]:
    return {
        "tests": [
            {
                "testId": test.test_id,
                "falsifierId": test.falsifier_id,
                "observable": test.observable,
                "condition": test.condition,
                "threshold": test.threshold,
                "description": test.description,
            }
            for test in plan.tests
        ],
        "controls": [
            {
                "controlId": control.control_id,
                "description": control.description,
                "expectation": control.expectation,
            }
            for control in plan.controls
        ],
    }


def _study_from_dict(value: object) -> StudyProgramReference:
    document = _mapping(value, "research study reference")
    try:
        return StudyProgramReference.from_document(document)
    except (ResearchModelError, TypeError, KeyError) as error:
        raise WorkflowError(f"invalid research study reference: {error}") from error


def _model_from_dict(value: object) -> ResearchModelRef | None:
    if value is None:
        return None
    document = _mapping(value, "research model reference")
    try:
        return ResearchModelRef(
            workspace_id=cast(str, document.get("workspaceId")),
            model_id=cast(str, document.get("modelId")),
            content_sha256=cast(str, document.get("contentSha256")),
        )
    except ResearchModelError as error:
        raise WorkflowError(f"invalid research model reference: {error}") from error


def _fit_from_dict(value: object) -> FittedModelRef | None:
    if value is None:
        return None
    document = _mapping(value, "research fit reference")
    try:
        return FittedModelRef(
            workspace_id=cast(str, document.get("workspaceId")),
            model_id=cast(str, document.get("modelId")),
            model_content_sha256=cast(str, document.get("modelContentSha256")),
            fit_sha256=cast(str, document.get("fitSha256")),
        )
    except ResearchModelError as error:
        raise WorkflowError(f"invalid research fit reference: {error}") from error


def _plan_from_dict(value: object) -> FalsificationPlan:
    document = _mapping(value, "research plan")
    tests = tuple(
        FalsifierTest(
            test_id=cast(str, item.get("testId")),
            falsifier_id=cast(str, item.get("falsifierId")),
            observable=cast(str, item.get("observable")),
            condition=cast(str, item.get("condition")),
            threshold=cast(float, item.get("threshold")),
            description=cast(str, item.get("description")),
        )
        for item in _records(document.get("tests"), "research plan tests")
    )
    controls = tuple(
        PlaceboControl(
            control_id=cast(str, item.get("controlId")),
            description=cast(str, item.get("description")),
            expectation=cast(str, item.get("expectation")),
        )
        for item in _records(document.get("controls"), "research plan controls")
    )
    return FalsificationPlan(tests=tests, controls=controls)


def _fitted_parameters(
    value: object,
) -> Mapping[str, float] | None:
    if value is None:
        return None
    document = _mapping(value, "research fitted parameters")
    return {
        str(name): _finite(item, f"fitted parameter {name!r}")
        for name, item in document.items()
    }


def _runtime_configuration(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    document = _mapping(value, "research runtime configuration")
    return {str(name): item for name, item in document.items()}


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    """The trials phase's portable evidence envelope, decoded by replay."""

    hypothesis_id: str | None
    study: StudyProgramReference
    model: ResearchModelRef | None
    workspace_artifact: str | None
    fit: FittedModelRef | None
    fitted_parameters: Mapping[str, float] | None
    runtime_configuration: Mapping[str, object] | None
    plan: FalsificationPlan
    assumptions: tuple[AssumptionEvidence, ...]
    controls: tuple[ControlEvidence, ...]
    report_digest: str
    limitations: str | None = None

    def __post_init__(self) -> None:
        if self.hypothesis_id is not None:
            _identifier(self.hypothesis_id, "research hypothesis_id")
        if not isinstance(self.study, StudyProgramReference):
            raise WorkflowError("research evidence requires a study reference")
        if self.model is not None and not isinstance(self.model, ResearchModelRef):
            raise WorkflowError("research evidence model must be a ResearchModelRef")
        if self.workspace_artifact is not None:
            _text(
                self.workspace_artifact,
                "research workspace_artifact",
                max_length=_MAX_ARTIFACT_TEXT,
            )
        if self.fit is not None and not isinstance(self.fit, FittedModelRef):
            raise WorkflowError("research evidence fit must be a FittedModelRef")
        if self.fitted_parameters is not None:
            object.__setattr__(
                self,
                "fitted_parameters",
                MappingProxyType(
                    {
                        str(name): _finite(item, f"fitted parameter {name!r}")
                        for name, item in _mapping(
                            self.fitted_parameters, "research fitted parameters"
                        ).items()
                    }
                ),
            )
        if self.runtime_configuration is not None:
            object.__setattr__(
                self,
                "runtime_configuration",
                MappingProxyType(
                    {
                        str(name): item
                        for name, item in _mapping(
                            self.runtime_configuration,
                            "research runtime configuration",
                        ).items()
                    }
                ),
            )
        if not isinstance(self.plan, FalsificationPlan):
            raise WorkflowError("research evidence requires a falsification plan")
        if any(not isinstance(item, AssumptionEvidence) for item in self.assumptions):
            raise WorkflowError(
                "research evidence assumptions must be AssumptionEvidence values"
            )
        if any(not isinstance(item, ControlEvidence) for item in self.controls):
            raise WorkflowError(
                "research evidence controls must be ControlEvidence values"
            )
        if not isinstance(self.report_digest, str) or (
            _DIGEST.fullmatch(self.report_digest) is None
        ):
            raise WorkflowError(
                "research report digest must be a lowercase SHA-256 digest"
            )
        if self.limitations is not None:
            _text(
                self.limitations,
                "research limitations",
                max_length=_MAX_USAGE_TEXT,
            )

    def as_dict(self) -> dict[str, object]:
        """Return the portable research evidence representation."""
        return {
            "assumptions": [item.as_dict() for item in self.assumptions],
            "controls": [item.as_dict() for item in self.controls],
            "fit": None if self.fit is None else _fit_dict(self.fit),
            "fittedParameters": (
                None
                if self.fitted_parameters is None
                else {
                    name: float(value) for name, value in self.fitted_parameters.items()
                }
            ),
            "hypothesisId": self.hypothesis_id,
            "limitations": self.limitations,
            "model": None if self.model is None else _model_dict(self.model),
            "plan": _plan_dict(self.plan),
            "reportDigest": self.report_digest,
            "runtimeConfiguration": (
                None
                if self.runtime_configuration is None
                else dict(self.runtime_configuration)
            ),
            "study": _study_dict(self.study),
            "workspaceArtifact": self.workspace_artifact,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ResearchEvidence:
        """Rebuild the evidence envelope from its portable representation."""
        payload = _mapping(payload, "research evidence")
        hypothesis_id = payload.get("hypothesisId")
        if hypothesis_id is not None and not isinstance(hypothesis_id, str):
            raise WorkflowError("research hypothesisId must be text")
        report_digest = payload.get("reportDigest")
        if not isinstance(report_digest, str) or (
            _DIGEST.fullmatch(report_digest) is None
        ):
            raise WorkflowError(
                "research report digest must be a lowercase SHA-256 digest"
            )
        assumptions = tuple(
            AssumptionEvidence.from_dict(item)
            for item in _records(payload.get("assumptions"), "research assumptions")
        )
        controls = tuple(
            ControlEvidence.from_dict(item)
            for item in _records(payload.get("controls"), "research controls")
        )
        return cls(
            hypothesis_id=hypothesis_id,
            study=_study_from_dict(payload.get("study")),
            model=_model_from_dict(payload.get("model")),
            workspace_artifact=_optional_text(
                payload.get("workspaceArtifact"),
                "research workspace_artifact",
                max_length=_MAX_ARTIFACT_TEXT,
            ),
            fit=_fit_from_dict(payload.get("fit")),
            fitted_parameters=_fitted_parameters(payload.get("fittedParameters")),
            runtime_configuration=_runtime_configuration(
                payload.get("runtimeConfiguration")
            ),
            plan=_plan_from_dict(payload.get("plan")),
            assumptions=assumptions,
            controls=controls,
            report_digest=report_digest,
            limitations=_optional_text(
                payload.get("limitations"),
                "research limitations",
                max_length=_MAX_USAGE_TEXT,
            ),
        )


__all__ = [
    "AssumptionEvidence",
    "ControlEvidence",
    "ResearchEvidence",
]
