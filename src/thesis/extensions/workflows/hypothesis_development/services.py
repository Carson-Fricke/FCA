"""Composed domain services for the hypothesis-development workflow.

The FCA endpoint owns this bundle. Phase implementations receive it through
scoped DevelopmentActivities; it never crosses the generic host JSON boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .activities import DevelopmentActivities

from thesis.research.hypothesis import HypothesisStore
from thesis.workflows.research_models import ProgramSnapshots, ResearchModelRunner


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


def services_of(workflow: DevelopmentActivities) -> HypothesisDevelopmentServices:
    """Return the workflow's domain services bundle."""
    return workflow.services


__all__ = [
    "HypothesisDevelopmentServices",
    "services_of",
]
