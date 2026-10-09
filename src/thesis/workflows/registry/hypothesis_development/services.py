"""Composed domain services for the hypothesis-development workflow.

The general engine treats this bundle as opaque: phase executors read the
domain dependencies (stores and study execution) from here, and the doctrine
carries only the structural data the engine itself reads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from thesis.research.hypothesis import HypothesisStore
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import ProgramSnapshots, ResearchModelRunner

from .contracts.creation import HypothesisDevelopmentRequest
from .state import HypothesisDevelopmentState

#: The general workflow object as the hypothesis-development executors see it.
type DevelopmentEngine = Workflow[
    HypothesisDevelopmentRequest, HypothesisDevelopmentState, object
]


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentServices:
    """The composed domain services the phase executors read."""

    store: HypothesisStore
    research_runner: ResearchModelRunner | None = None
    programs: ProgramSnapshots | None = None
    experiment_id: str | None = None
    mcp_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    phase_mcp_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    phase_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def services_of(workflow: Workflow[Any, Any, Any]) -> HypothesisDevelopmentServices:
    """Return the workflow's domain services bundle."""
    return cast(HypothesisDevelopmentServices, workflow.services)


__all__ = [
    "DevelopmentEngine",
    "HypothesisDevelopmentServices",
    "services_of",
]
