"""The general Thesis workflow implementation.

``thesis_runtime.workflows`` owns the workflow engine: the specification
contracts, the builder that validates them, the registry that resolves a
workflow id to its registration, the transition machine, node lifecycle, and
workflow event projections.  It contains no workflow-specific behavior or
contract; a concrete workflow is a
:class:`~thesis_runtime.workflows.spec.WorkflowSpec` registered under its id.
"""

from thesis_runtime.workflows.builder import WorkflowBuilder
from thesis_runtime.workflows.delivery import WorkflowNodeManager
from thesis_runtime.workflows.events import emit_phase, finish_execution
from thesis_runtime.workflows.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    PhaseSpec,
    SplitPhase,
    TerminalPhase,
)
from thesis_runtime.workflows.registry import (
    UnknownWorkflowError,
    WorkflowExecute,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis_runtime.workflows.spec import (
    WorkflowContext,
    WorkflowSpec,
    WorkflowSpecError,
)
from thesis_runtime.workflows.state import TERMINAL_EXECUTION_STATES, WorkflowState
from thesis_runtime.workflows.workflow import (
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
    "OneShotPhase",
    "PhaseSpec",
    "SplitPhase",
    "TERMINAL_EXECUTION_STATES",
    "TerminalPhase",
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
    "finish_execution",
]
