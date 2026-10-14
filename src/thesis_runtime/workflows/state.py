"""Common workflow state shapes and replay constants.

The machine only needs a small slice of any concrete workflow state: its
current phase, its intent epoch, and its ordered transition records.
``WorkflowState`` is that structural contract; a concrete state dataclass
satisfies it without inheriting from the engine.
"""

from __future__ import annotations

from typing import Protocol

from thesis_runtime.contracts.records import WorkflowExecutionState

#: Execution states after which the engine never resumes a run.
TERMINAL_EXECUTION_STATES: frozenset[WorkflowExecutionState] = frozenset(
    {
        WorkflowExecutionState.COMPLETED,
        WorkflowExecutionState.FAILED,
        WorkflowExecutionState.STOPPED,
        WorkflowExecutionState.CANCELLED,
    }
)


class WorkflowState(Protocol):
    """The state slice the generic transition machinery reads."""

    @property
    def phase(self) -> object:
        """Return the current phase value."""

    @property
    def intent_epoch(self) -> int:
        """Return the execution's intent epoch at the last transition."""

    @property
    def transitions(self) -> tuple[object, ...]:
        """Return the ordered transition records."""
