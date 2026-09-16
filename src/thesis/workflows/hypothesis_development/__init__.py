"""Falsification-first hypothesis-development workflow package."""

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
from .state import (
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentState,
)
from .workflow import (
    HypothesisDevelopmentWorkflow,
)

__all__ = [
    "DEFAULT_MAX_COMPOSE_REPAIRS",
    "DEFAULT_MAX_HYPOTHESIS_CYCLES",
    "DEFAULT_MAX_RCA_CYCLES",
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
    "HypothesisDevelopmentState",
    "HypothesisDevelopmentWorkflow",
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
    "check_evidence_bound",
    "check_falsifiability",
    "check_falsification_plan",
    "check_research_model",
    "check_strategy_design",
    "check_verdict_advances",
    "transition_phase",
]
