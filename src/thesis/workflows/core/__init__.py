"""The general Thesis workflow implementation.

``thesis.workflows.core`` owns the workflow engine: the specification
contracts, the builder that validates them, the registry that resolves a
workflow id to its registration, the transition machine, node delivery, and
workflow event projections.  It contains no workflow-specific behavior or
contract; a concrete workflow is a
:class:`~thesis.workflows.core.spec.WorkflowSpec` registered under its id.
"""

from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.delivery import NodeTurnDelivery, WorkflowNodeManager
from thesis.workflows.core.events import emit_phase, finish_execution
from thesis.workflows.core.gate import GateCheck, GateOutcome, evaluate_gate
from thesis.workflows.core.machine import TransitionMachine, TransitionTable
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    PhaseSpec,
)
from thesis.workflows.core.registry import (
    UnknownWorkflowError,
    WorkflowExecute,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis.workflows.core.spec import (
    PipingFunction,
    TransitionRequest,
    WorkflowContext,
    WorkflowSpec,
    WorkflowSpecError,
)
from thesis.workflows.core.state import TERMINAL_EXECUTION_STATES, WorkflowState
from thesis.workflows.core.workflow import (
    ActiveSubAgent,
    Workflow,
    WorkflowDoctrine,
    WorkflowEngineError,
    WorkflowPorts,
    WorkflowRequest,
)

__all__ = [
    "ActiveSubAgent",
    "ConversationPhase",
    "DeterministicPhase",
    "GateCheck",
    "GateOutcome",
    "NodeTurnDelivery",
    "OneShotPhase",
    "PhaseSpec",
    "PipingFunction",
    "TERMINAL_EXECUTION_STATES",
    "TransitionMachine",
    "TransitionRequest",
    "TransitionTable",
    "UnknownWorkflowError",
    "Workflow",
    "WorkflowBuilder",
    "WorkflowContext",
    "WorkflowDoctrine",
    "WorkflowEngineError",
    "WorkflowExecute",
    "WorkflowNodeManager",
    "WorkflowPorts",
    "WorkflowRegistration",
    "WorkflowRegistry",
    "WorkflowRequest",
    "WorkflowSpec",
    "WorkflowSpecError",
    "WorkflowState",
    "emit_phase",
    "evaluate_gate",
    "finish_execution",
]
