"""Workflow state and outcome records.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged except for the strategy chain, whose
legacy placeholders are replaced by typed accepted handoffs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from thesis.research.hypothesis import HypothesisDocument
from thesis.research.models import FittedModelRef, ResearchModelRef

from .contracts import (
    CandidateEvaluation,
    DiscoveryDraft,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    ForwardEvaluation,
    HypothesisDraft,
    NullTestEvaluation,
    RcaDiagnosis,
    ResearchEvidence,
    StrategyBacktest,
    VariationEvaluation,
)
from .phases import HypothesisDevelopmentPhase, PhaseTransition


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentState:
    """The immutable state snapshot after each workflow transition.

    ``strategy`` is the frozen program reference accepted at strategy design;
    ``strategy_backtest`` is the accepted backtest handoff.  Both are folded from
    stored completions, so replay reproduces them exactly.
    """

    workspace_id: str
    execution_id: str
    phase: HypothesisDevelopmentPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    discovery: DiscoveryDraft | None = None
    hypothesis: HypothesisDocument | None = None
    draft: HypothesisDraft | None = None
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    research_evidence: ResearchEvidence | None = None
    hypothesis_cycles: int = 0
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy: Mapping[str, object] | None = None
    strategy_backtest: StrategyBacktest | None = None
    candidate_evaluation: CandidateEvaluation | None = None
    null_testing: NullTestEvaluation | None = None
    variation: VariationEvaluation | None = None
    forward_validation: ForwardEvaluation | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: HypothesisDevelopmentPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    discovery: DiscoveryDraft | None = None
    hypothesis: HypothesisDocument | None = None
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    research_evidence: ResearchEvidence | None = None
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy: Mapping[str, object] | None = None
    hypothesis_cycles: int = 0
    strategy_backtest: StrategyBacktest | None = None
    candidate_evaluation: CandidateEvaluation | None = None
    null_testing: NullTestEvaluation | None = None
    variation: VariationEvaluation | None = None
    forward_validation: ForwardEvaluation | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "executionId": self.execution_id,
            "fenced": self.fenced,
            "discovery": (None if self.discovery is None else self.discovery.as_dict()),
            "hypothesis": (
                None
                if self.hypothesis is None
                else {
                    "frontMatter": self.hypothesis.front_matter.model_dump(mode="json"),
                    "body": self.hypothesis.body,
                    "contentSha256": self.hypothesis.content_sha256,
                    "path": self.hypothesis.relative_path,
                }
            ),
            "hypothesisCycles": self.hypothesis_cycles,
            "researchModel": (
                None
                if self.research_model_reference is None
                else {
                    "contentSha256": self.research_model_reference.content_sha256,
                    "evidenceArtifact": self.research_model_artifact,
                    "fittedReference": (
                        None
                        if self.fitted_model_reference is None
                        else self.fitted_model_reference.key
                    ),
                    "fittedParameters": (
                        None
                        if self.fitted_parameters is None
                        else dict(self.fitted_parameters)
                    ),
                    "runtimeConfiguration": (
                        None
                        if self.research_model_runtime_configuration is None
                        else dict(self.research_model_runtime_configuration)
                    ),
                    "modelId": self.research_model_reference.model_id,
                    "reference": self.research_model_reference.key,
                }
            ),
            "candidateEvaluation": (
                None
                if self.candidate_evaluation is None
                else self.candidate_evaluation.as_dict()
            ),
            "nullTesting": (
                None if self.null_testing is None else self.null_testing.as_dict()
            ),
            "variation": (None if self.variation is None else self.variation.as_dict()),
            "forwardValidation": (
                None
                if self.forward_validation is None
                else self.forward_validation.as_dict()
            ),
            "strategy": None if self.strategy is None else dict(self.strategy),
            "strategyBacktest": (
                None
                if self.strategy_backtest is None
                else self.strategy_backtest.as_dict()
            ),
            "phase": self.phase.value,
            "plan": (
                None
                if self.plan is None
                else {
                    "tests": [
                        {
                            "condition": test.condition,
                            "description": test.description,
                            "falsifierId": test.falsifier_id,
                            "observable": test.observable,
                            "testId": test.test_id,
                            "threshold": test.threshold,
                        }
                        for test in self.plan.tests
                    ],
                    "controls": [
                        {
                            "controlId": control.control_id,
                            "description": control.description,
                            "expectation": control.expectation,
                        }
                        for control in self.plan.controls
                    ],
                }
            ),
            "rca": [item.as_dict() for item in self.rca],
            "researchEvidence": (
                None
                if self.research_evidence is None
                else self.research_evidence.as_dict()
            ),
            "transitions": [item.as_dict() for item in self.transitions],
            "trials": [item.as_dict() for item in self.trials],
            "verdict": None if self.verdict is None else self.verdict.as_dict(),
            "workspaceId": self.workspace_id,
            "detail": self.detail,
        }
