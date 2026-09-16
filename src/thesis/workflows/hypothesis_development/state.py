"""Workflow state and outcome records.

Extracted from the original single-module hypothesis-development
implementation; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from thesis.research.hypothesis import HypothesisDocument
from thesis.research.models import FittedModelRef, ResearchModelRef
from thesis.strategies.bundles import StrategyBundle
from thesis.workflows.backtests import BacktestRunResult

from .contracts import (
    ExitCriterion,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    HypothesisDraft,
    RcaDiagnosis,
)
from .phases import HypothesisDevelopmentPhase, PhaseTransition


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: HypothesisDevelopmentPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    hypothesis: HypothesisDocument | None = None
    draft: HypothesisDraft | None = None
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    falsification_prose: str | None = None
    hypothesis_cycles: int = 0
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    strategy_cycles: int = 0
    strategy_backtest: BacktestRunResult | None = None
    optimization_backtest: BacktestRunResult | None = None
    model_provenance: Mapping[str, object] | None = None
    journal_id: str | None = None
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
    hypothesis: HypothesisDocument | None = None
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    hypothesis_cycles: int = 0
    strategy_cycles: int = 0
    strategy_backtest: BacktestRunResult | None = None
    optimization_backtest: BacktestRunResult | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "executionId": self.execution_id,
            "exitCriteria": [item.as_dict() for item in self.exit_criteria],
            "fenced": self.fenced,
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
            "journalId": self.journal_id,
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
            "optimizationBacktest": (
                None
                if self.optimization_backtest is None
                else self.optimization_backtest.as_dict()
            ),
            "strategyBacktest": (
                None
                if self.strategy_backtest is None
                else self.strategy_backtest.as_dict()
            ),
            "strategyCycles": self.strategy_cycles,
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
            "strategyManifest": (
                None
                if self.strategy_bundle is None
                else self.strategy_bundle.manifest()
            ),
            "transitions": [item.as_dict() for item in self.transitions],
            "trials": [item.as_dict() for item in self.trials],
            "verdict": None if self.verdict is None else self.verdict.as_dict(),
            "workspaceId": self.workspace_id,
            "detail": self.detail,
        }
