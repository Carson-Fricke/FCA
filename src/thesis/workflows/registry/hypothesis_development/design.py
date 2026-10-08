"""The frozen research design: what the development evidence committed the run to.

The design is a pure function of accepted state plus the protected forward
window.  It names the frozen strategy reference, the center parameters, the
block-permutation null design (statistic, direction, block size, and seeds), the
variation neighborhood, and the forward window.  Forward submissions carry its
digest, so a forward result proves which design it ran under.  Nothing is stored
as a second copy: the design is recomputed from the folded development state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from thesis.persistence.journal import content_hash

from .contracts.variation import canonical_parameter_identity
from .errors import WorkflowError
from .state import HypothesisDevelopmentState


def canonical_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    """Return one parameter vector in its canonical, type-strict form.

    The form is the parsed :func:`canonical_parameter_identity`: keys are sorted
    and every value keeps its JSON type, so integer ``5`` stays an integer and
    float ``5.0`` stays a float.  Python equality on the result is not the
    identity test; compare :func:`canonical_parameter_identity` texts instead.
    """
    parsed: dict[str, object] = json.loads(canonical_parameter_identity(parameters))
    return parsed


@dataclass(frozen=True, slots=True)
class FrozenDesign:
    """The development commitments a forward validation must reproduce exactly."""

    strategy: Mapping[str, object]
    window_start: str
    window_end: str
    statistic: str
    direction: str
    block_size: int
    seeds: tuple[int, ...]
    center: Mapping[str, object]
    variation_parameters: tuple[str, ...]
    variation_points: tuple[Mapping[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        """Return the canonical document the digest is taken over."""
        return {
            "blockSize": self.block_size,
            "center": dict(self.center),
            "direction": self.direction,
            "seeds": list(self.seeds),
            "statistic": self.statistic,
            "strategy": dict(self.strategy),
            "variationParameters": list(self.variation_parameters),
            "variationPoints": [dict(point) for point in self.variation_points],
            "window": {"end": self.window_end, "start": self.window_start},
        }

    def sha256(self) -> str:
        """Return the content digest forward submissions carry."""
        return content_hash(self.as_dict())


def frozen_design(
    state: HypothesisDevelopmentState, window_start: str, window_end: str
) -> FrozenDesign:
    """Derive the frozen design from the accepted development evidence.

    Raises :class:`WorkflowError` until strategy design, null testing, and
    variation have all been accepted, because the design is not yet defined.
    """
    if state.strategy is None:
        raise WorkflowError("the frozen design requires an accepted strategy reference")
    if state.null_testing is None:
        raise WorkflowError("the frozen design requires accepted null testing")
    if state.variation is None:
        raise WorkflowError("the frozen design requires an accepted variation")
    null = state.null_testing
    variation = state.variation
    points = sorted(
        variation.points,
        key=lambda point: canonical_parameter_identity(point.parameters),
    )
    return FrozenDesign(
        strategy=dict(state.strategy),
        window_start=window_start,
        window_end=window_end,
        statistic=null.statistic,
        direction=null.direction,
        block_size=null.block_size,
        seeds=tuple(sorted(replicate.seed for replicate in null.replicates)),
        center=canonical_parameters(variation.center.parameters),
        variation_parameters=tuple(sorted(variation.parameters)),
        variation_points=tuple(
            canonical_parameters(point.parameters) for point in points
        ),
    )


__all__ = [
    "FrozenDesign",
    "canonical_parameters",
    "frozen_design",
]
