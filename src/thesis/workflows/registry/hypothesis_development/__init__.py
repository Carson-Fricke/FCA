"""Falsification-first hypothesis-development workflow package.

This package is the concrete workflow registered as ``hypothesis-development``.
It owns the workflow's contracts, prompts, gates, phase vocabulary, per-phase
executors, typed piping, and the assembled
:class:`~thesis.workflows.core.spec.WorkflowSpec`.  The general engine lives
under ``thesis.workflows.core``; nothing here is imported by another workflow.
"""

from __future__ import annotations

from .composers import (
    JsonDevelopmentHypothesisComposer,
    JsonFalsificationPlanner,
    JsonRcaComposer,
    JsonStrategyDesigner,
    _json_document,
)
from .contracts import (
    ExitCriterion,
    FalsificationPlan,
    FalsificationPlanDraft,
    FalsificationTrial,
    FalsificationVerdict,
    FalsifierTest,
    HypothesisDevelopmentRequest,
    HypothesisDraft,
    PlaceboControl,
    PlannedTest,
    RcaDiagnosis,
    StrategyContent,
    StrategyDesignDraft,
)
from .errors import (
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    InvalidPhaseTransition,
    WorkflowError,
)
from .gates import (
    check_evidence_bound,
    check_falsifiability,
    check_falsification_plan,
    check_research_model,
    check_strategy_design,
    check_verdict_advances,
)
from .phases import (
    HYPOTHESIS_DEVELOPMENT_PHASES,
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentPhase,
    PhaseTransition,
    transition_phase,
)
from .piping import identity_pipe, reset_backtests, reset_premise
from .primitives import (
    DEFAULT_MAX_COMPOSE_REPAIRS,
    DEFAULT_MAX_HYPOTHESIS_CYCLES,
    DEFAULT_MAX_RCA_CYCLES,
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    _metric,
)
from .prompts import (
    FalsificationPlanner,
    HypothesisComposer,
    RcaComposer,
    StrategyDesigner,
)
from .services import (
    DevelopmentEngine,
    HypothesisDevelopmentServices,
    services_of,
)
from .spec import (
    build_hypothesis_development_spec,
    build_hypothesis_development_workflow,
)
from .state import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentState,
)

__all__ = [
    "DEFAULT_MAX_COMPOSE_REPAIRS",
    "DEFAULT_MAX_HYPOTHESIS_CYCLES",
    "DEFAULT_MAX_RCA_CYCLES",
    "DevelopmentEngine",
    "ExitCriterion",
    "FalsificationComposeError",
    "FalsificationPlan",
    "FalsificationPlanDraft",
    "FalsificationPlanner",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HYPOTHESIS_DEVELOPMENT_PHASES",
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "HypothesisComposer",
    "HypothesisDevelopmentIntakeError",
    "HypothesisDevelopmentOutcome",
    "HypothesisDevelopmentPhase",
    "HypothesisDevelopmentRequest",
    "HypothesisDevelopmentServices",
    "HypothesisDevelopmentState",
    "HypothesisDraft",
    "InvalidPhaseTransition",
    "JsonDevelopmentHypothesisComposer",
    "JsonFalsificationPlanner",
    "JsonRcaComposer",
    "JsonStrategyDesigner",
    "PhaseTransition",
    "PlaceboControl",
    "PlannedTest",
    "RcaComposer",
    "RcaDiagnosis",
    "StrategyContent",
    "StrategyDesignDraft",
    "StrategyDesigner",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUTED",
    "VERDICT_SUPPORTED",
    "WorkflowError",
    "_json_document",
    "_metric",
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "check_evidence_bound",
    "check_falsifiability",
    "check_falsification_plan",
    "check_research_model",
    "check_strategy_design",
    "check_verdict_advances",
    "identity_pipe",
    "reset_backtests",
    "reset_premise",
    "services_of",
    "transition_phase",
]
