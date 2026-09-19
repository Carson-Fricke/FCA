"""The planned falsification tests and negative controls."""

from __future__ import annotations

from dataclasses import dataclass

from thesis.research.hypothesis import FALSIFIER_CONDITIONS

from ..errors import (
    WorkflowError,
)
from ..primitives import (
    _identifier,
    _text,
)


@dataclass(frozen=True, slots=True)
class PlannedTest:
    """One planned falsifier test before it is bound to a declared falsifier."""

    test_id: str
    falsifier_id: str
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class PlaceboControl:
    """One negative/placebo control that should not show the claimed edge."""

    control_id: str
    description: str
    expectation: str

    def __post_init__(self) -> None:
        _identifier(self.control_id, "control_id")
        _text(self.description, "control description", max_length=2_048)
        _text(self.expectation, "control expectation", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlanDraft:
    """The model-authored plan before falsifier binding."""

    tests: tuple[PlannedTest, ...]
    controls: tuple[PlaceboControl, ...]


@dataclass(frozen=True, slots=True)
class FalsifierTest:
    """One planned falsifier test bound to its observable and threshold."""

    test_id: str
    falsifier_id: str
    observable: str
    condition: str
    threshold: float
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _identifier(self.observable, "observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"falsifier condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("falsifier threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlan:
    """The concrete falsification plan: tests plus negative controls."""

    tests: tuple[FalsifierTest, ...]
    controls: tuple[PlaceboControl, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tests, tuple) or not isinstance(self.controls, tuple):
            raise WorkflowError("falsification plan tests and controls must be tuples")
