"""Falsification-first hypothesis-development workflow package.

This package is the concrete workflow registered as ``hypothesis-development``.
It owns the workflow's contracts, prompts, phase vocabulary, per-phase
executors, typed piping, and the assembled
:class:`~thesis_runtime.workflows.spec.WorkflowSpec`.  The general engine lives
under ``thesis_runtime.workflows``; nothing here is imported by another workflow.
"""

from __future__ import annotations

from .activities import DevelopmentActivities
from .contracts import (
    DiscoveryAttempt,
    DiscoveryDraft,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    FalsifierTest,
    HypothesisDevelopmentRequest,
    HypothesisDraft,
    PlaceboControl,
    RcaDiagnosis,
    StrategyBacktest,
)
from .errors import (
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    WorkflowError,
)
from .phases import (
    HypothesisDevelopmentPhase,
    PhaseTransition,
    phase_from_value,
    workflow_definition,
)
from .piping import (
    identity_pipe,
    reset_backtests,
    reset_discovery,
    reset_premise,
)
from .primitives import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    _metric,
)
from .rebuild import fold_transition, rebuild_state
from .services import (
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

HYPOTHESIS_DEVELOPMENT_WORKFLOW = workflow_definition()
HYPOTHESIS_DEVELOPMENT_PHASES = HYPOTHESIS_DEVELOPMENT_WORKFLOW.phases

__all__ = [
    "DevelopmentActivities",
    "DiscoveryAttempt",
    "DiscoveryDraft",
    "StrategyBacktest",
    "FalsificationComposeError",
    "FalsificationPlan",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HYPOTHESIS_DEVELOPMENT_PHASES",
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "HypothesisDevelopmentIntakeError",
    "HypothesisDevelopmentOutcome",
    "HypothesisDevelopmentPhase",
    "HypothesisDevelopmentRequest",
    "HypothesisDevelopmentServices",
    "HypothesisDevelopmentState",
    "HypothesisDraft",
    "PhaseTransition",
    "PlaceboControl",
    "RcaDiagnosis",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUTED",
    "VERDICT_SUPPORTED",
    "WorkflowError",
    "_metric",
    "build_hypothesis_development_spec",
    "build_hypothesis_development_workflow",
    "fold_transition",
    "identity_pipe",
    "phase_from_value",
    "rebuild_state",
    "reset_backtests",
    "reset_discovery",
    "reset_premise",
    "services_of",
]
