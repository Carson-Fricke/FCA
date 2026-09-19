"""Composed domain services for the hypothesis-development workflow.

The general engine treats this bundle as opaque: phase executors read the
domain dependencies (router, stores, composers, roles, budgets) from here, and
the doctrine carries only the structural data the engine itself reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from thesis.agent.router import ModelRouter, Role
from thesis.experiments.ledger import AttemptLedger
from thesis.persistence.journal import CommandJournal
from thesis.research.hypothesis import HypothesisStore
from thesis.strategies.validation import StrategySandboxValidator
from thesis.workflows.backtests import BacktestRunner
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import ResearchModelRunner

from .contracts.creation import HypothesisDevelopmentRequest
from .primitives import (
    DEFAULT_MAX_COMPOSE_REPAIRS,
    DEFAULT_MAX_HYPOTHESIS_CYCLES,
    DEFAULT_MAX_RCA_CYCLES,
)
from .prompts.creation import HypothesisComposer
from .prompts.falsification import FalsificationPlanner
from .prompts.rca import RcaComposer
from .prompts.strategy import StrategyDesigner
from .state import HypothesisDevelopmentState

#: The general workflow object as the hypothesis-development executors see it.
type DevelopmentEngine = Workflow[
    HypothesisDevelopmentRequest, HypothesisDevelopmentState, object
]


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentServices:
    """The composed domain services the phase executors read."""

    store: HypothesisStore
    composer: HypothesisComposer
    planner: FalsificationPlanner
    rca_composer: RcaComposer
    designer: StrategyDesigner
    router: ModelRouter | None = None
    runner: BacktestRunner | None = None
    research_runner: ResearchModelRunner | None = None
    validator: StrategySandboxValidator | None = None
    ledger: AttemptLedger | None = None
    journal: CommandJournal | None = None
    research_role: Role = "research"
    critique_role: Role = "critique"
    coding_role: Role = "coding"
    experiment_id: str | None = None
    max_hypothesis_cycles: int = DEFAULT_MAX_HYPOTHESIS_CYCLES
    max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES
    max_compose_repairs: int = DEFAULT_MAX_COMPOSE_REPAIRS
    mcp_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def services_of(workflow: Workflow[Any, Any, Any]) -> HypothesisDevelopmentServices:
    """Return the workflow's domain services bundle."""
    return cast(HypothesisDevelopmentServices, workflow.services)


__all__ = [
    "DevelopmentEngine",
    "HypothesisDevelopmentServices",
    "services_of",
]
