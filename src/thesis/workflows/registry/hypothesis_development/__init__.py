"""Falsification-first hypothesis-development workflow package.

This package is the concrete workflow registered as ``hypothesis-development``.
It owns the workflow's contracts, prompts, phase vocabulary, per-phase
executors, typed piping, and the assembled
:class:`~thesis.workflows.core.spec.WorkflowSpec`.  The general engine lives
under ``thesis.workflows.core``; nothing here is imported by another workflow.
"""

from __future__ import annotations

from .contracts import (
    DiscoveryAttempt,
    DiscoveryCandidate,
    DiscoveryDraft,
    ExitCriterion,
    FalsificationPlan,
    FalsificationTrial,
    FalsificationVerdict,
    FalsifierTest,
    HypothesisDevelopmentRequest,
    HypothesisDraft,
    PlaceboControl,
    RcaDiagnosis,
)
from .errors import (
    FalsificationComposeError,
    HypothesisDevelopmentIntakeError,
    InvalidPhaseTransition,
    WorkflowError,
)
from .phases import (
    HypothesisDevelopmentPhase,
    PhaseTransition,
    phase_from_value,
    transition_phase,
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

HYPOTHESIS_DEVELOPMENT_WORKFLOW = workflow_definition()
HYPOTHESIS_DEVELOPMENT_PHASES = HYPOTHESIS_DEVELOPMENT_WORKFLOW.phases

__all__ = [
    "DevelopmentEngine",
    "DiscoveryAttempt",
    "DiscoveryCandidate",
    "DiscoveryDraft",
    "ExitCriterion",
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
    "InvalidPhaseTransition",
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
    "transition_phase",
]
