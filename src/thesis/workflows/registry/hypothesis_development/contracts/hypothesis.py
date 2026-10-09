"""The falsifiable hypothesis and its separate statistical model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from thesis.research.hypothesis import HypothesisFalsifier
from thesis.research.models import ResearchModel
from thesis.workflows.research_models import StudyProgramReference
from thesis.workspaces.sketch_contract import StrategySketchReference

from ..errors import (
    FalsificationComposeError,
)
from ..primitives import (
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class HypothesisDraft:
    """The falsifiable hypothesis and its separate statistical model."""

    claim: str
    disagreement: str
    risk_factor: str
    horizon: str
    universe: tuple[str, ...]
    falsifiers: tuple[HypothesisFalsifier, ...]
    model: ResearchModel | StudyProgramReference
    sizing: str | None = None
    body: str = ""
    hypothesis_id: str | None = None
    sketch_reference: StrategySketchReference | None = None
    assumption_tests: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.claim, "claim", max_length=8_192)
        _text(self.disagreement, "disagreement", max_length=8_192)
        _text(self.risk_factor, "risk_factor", max_length=4_096)
        _text(self.horizon, "horizon", max_length=4_096)
        if isinstance(self.universe, str) or not isinstance(self.universe, Sequence):
            raise FalsificationComposeError("universe must be a sequence")
        universe = tuple(_identifier(item, "universe") for item in self.universe)
        if not universe:
            raise FalsificationComposeError("universe must contain at least one symbol")
        object.__setattr__(self, "universe", universe)
        if any(not isinstance(item, HypothesisFalsifier) for item in self.falsifiers):
            raise FalsificationComposeError(
                "hypothesis draft requires HypothesisFalsifier values"
            )
        if not isinstance(self.model, ResearchModel | StudyProgramReference):
            raise FalsificationComposeError(
                "hypothesis draft requires a research model"
            )
        if self.sizing is not None:
            _text(self.sizing, "sizing", max_length=4_096)
        if self.hypothesis_id is not None:
            _identifier(self.hypothesis_id, "hypothesis_id")
