"""Conversation-facing workflow host over the agent-independent graph engine.

Owns activity bindings, trusted conversation lifecycle, completion admission,
projection and durable dispatch. Generic routing, port readiness and activation
replay belong exclusively to workflows.graph; domain policy belongs to extensions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, cast

from thesis_runtime.contracts.datum import Datum, DatumType
from thesis_runtime.contracts.messages import from_record
from thesis_runtime.contracts.records import (
    WORKFLOW_NODE_ROLE,
    CompletionMode,
    InvalidLifecycleTransition,
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import (
    Anchor,
    DeliveryService,
)
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.events import EventReplay, ExecutionEventType
from thesis_runtime.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_sub_agent_event,
)
from thesis_runtime.execution.worker import Work, WorkStore
from thesis_runtime.execution.workflow_events import (
    append_workflow_phase_result,
    append_workflow_progress,
)
from thesis_runtime.subagents.control import PhaseCompletionRejected
from thesis_runtime.subagents.control_tools import CONTROL_PHASE_TOOL_NAMES
from thesis_runtime.subagents.lifecycle import SubAgentLifecycle
from thesis_runtime.subagents.specs import InitialContext, SubAgentSpec
from thesis_runtime.workflows.delivery import WorkflowNodeManager
from thesis_runtime.workflows.events import emit_phase, finish_execution, graph_payload
from thesis_runtime.workflows.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStore,
    ExecutionStoreConflict,
    StoreTransition,
)
from thesis_runtime.workflows.extensions import (
    PhaseSeed,
    RunRequest,
    StateSnapshot,
    WorkflowExtension,
    invoke_extension,
)
from thesis_runtime.workflows.graph import Graph, Position, replay_graph
from thesis_runtime.workflows.graph.contracts import ActivityCompletion
from thesis_runtime.workflows.phase import (
    ConversationPhase,
    DeterministicPhase,
    TerminalPhase,
)
from thesis_runtime.workflows.spec import (
    HandoffContext,
    WorkflowContext,
    WorkflowSpec,
)
from thesis_runtime.workflows.state import TERMINAL_EXECUTION_STATES, WorkflowState

__all__ = [
    "ActiveSubAgent",
    "Detach",
    "Workflow",
    "WorkflowDoctrine",
    "WorkflowEngineError",
    "WorkflowPorts",
    "WorkflowRequest",
]


class WorkflowEngineError(RuntimeError):
    """Raised when the general workflow object is used without its bindings."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class WorkflowRequest(Protocol):
    """The request slice the general turn skeleton reads."""

    @property
    def workspace_id(self) -> str:
        """Return the workspace id."""

    @property
    def chat_id(self) -> str:
        """Return the chat id."""

    @property
    def execution_id(self) -> str:
        """Return the durable execution id."""

    @property
    def trigger_message_id(self) -> str:
        """Return the launching message id."""

    @property
    def requested_by(self) -> str:
        """Return the requesting actor id."""

    @property
    def intent_epoch(self) -> int:
        """Return the execution's intent epoch."""


@dataclass(frozen=True, slots=True)
class ActiveSubAgent:
    """The currently open top-level orchestrator for one execution."""

    sub_agent_id: str
    phase: object
    role: str
    context: SubAgentContextSpec
    attempt_id: str


#: Runs one zero-argument callable out of band.  The composition owns the
#: threading seam; the engine only names it.
Detach = Callable[[Callable[[], None]], None]


@dataclass(frozen=True, slots=True)
class WorkflowPorts:
    """The composed engine ports that make a workflow runnable.

    ``conversations`` is the one service the engine sees: it spawns the
    phase children, delivers their results, and owns every wake.  The durable
    record lifecycle is read from that service when not supplied directly.
    ``execution_store`` is the authoritative durable document per execution
    (REQ-B5); machine history is committed there first and projected after.
    ``detach`` is the scheduling seam for successor-phase entry: when bound,
    a phase completion returns as soon as its transition is committed and the
    next phase runs out of band; when absent the engine enters it inline.
    """

    chat: ChatService
    clock: Callable[[], datetime]
    id_factory: Callable[[str], str]
    conversations: Conversations | None = None
    subagent_lifecycle: SubAgentLifecycle | None = None
    execution_store: ExecutionStore | None = None
    detach: Detach | None = None
    work_store: WorkStore | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDoctrine[RequestT: WorkflowRequest, StateT: WorkflowState, OutcomeT]:
    """The workflow-specific data the general engine reads.

    Everything a future workflow would otherwise encode in a subclass lives
    here: phase metadata, the trusted-context and projection hooks, and the
    model/persistence adapters.
    """

    definition: WorkflowDefinition
    initial_phase: str
    actor: str
    active_phases: tuple[str, ...]
    conversational_phases: frozenset[str]
    orchestrators: Mapping[str, tuple[str, str]]
    tasks: Mapping[str, str]
    activity: Mapping[str, str]
    default_activity: str
    mcp_tools: Mapping[str, tuple[str, ...]]
    extension: WorkflowExtension
    #: Per-phase MCP tool configuration. A phase absent here falls back to its
    #: family's ``mcp_tools``; an explicit empty tuple configures no MCP tools.
    phase_mcp_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Explicit local capabilities granted to each phase executor, not its family.
    phase_tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _phase_mcp_tools(
    doctrine: WorkflowDoctrine[Any, Any, Any], phase_value: str, family: str
) -> tuple[str, ...]:
    """Return the MCP tools configured for one phase, else its family's."""
    declared = doctrine.phase_mcp_tools.get(phase_value)
    if declared is not None:
        return tuple(declared)
    return tuple(doctrine.mcp_tools.get(family, ()))


def _phase_allowed_tools(
    doctrine: WorkflowDoctrine[Any, Any, Any], phase_value: str, family: str
) -> tuple[str, ...]:
    """Return a phase agent's exact agent-facing tool policy.

    The catalog is built from exactly this list, so the configuration here is
    both the dispatch policy and the advertised catalog: the phase's
    configured MCP tools, explicit local phase grants, and structural controls.
    """
    configured = _phase_mcp_tools(doctrine, phase_value, family)
    return tuple(
        dict.fromkeys(
            (
                *configured,
                *doctrine.phase_tools.get(phase_value, ()),
                *CONTROL_PHASE_TOOL_NAMES,
            )
        )
    )


class Workflow[RequestT: WorkflowRequest, StateT: WorkflowState, OutcomeT]:
    """The general, spec-driven workflow instance.

    ``prepare_phase`` reconciles each active node with its durable dispatch;
    ``apply_phase_completion`` validates one phase agent's completion and
    applies its transition; ``execute`` launches a new execution.  The object
    projects control from accepted history. Extensions reconstruct scoped
    domain state and never choose the host's active frontier.
    """

    def __init__(
        self,
        spec: WorkflowSpec[Any, RequestT, StateT],
        *,
        doctrine: WorkflowDoctrine[RequestT, StateT, OutcomeT] | None = None,
        ports: WorkflowPorts | None = None,
    ) -> None:
        self._spec = spec
        self._doctrine = doctrine
        self._ports = ports
        self._chat = None if ports is None else ports.chat
        self._clock = _utc_now if ports is None else ports.clock
        self._id_factory = _new_id if ports is None else ports.id_factory
        self._conversations = None if ports is None else ports.conversations
        self._execution_store = None if ports is None else ports.execution_store
        self._detach = None if ports is None else ports.detach
        self._work_store = None if ports is None else ports.work_store
        lifecycle = None if ports is None else ports.subagent_lifecycle
        if lifecycle is None and self._conversations is not None:
            lifecycle = self._conversations.lifecycle
        self._subagent_lifecycle = lifecycle
        self._active_orchestrators: dict[tuple[str, str], ActiveSubAgent] = {}
        self._running_deterministic: set[str] = set()
        self._delivery: DeliveryService | None = (
            None if self._conversations is None else self._conversations.delivery
        )
        self._nodes: WorkflowNodeManager | None = None
        if ports is not None:
            if doctrine is None:
                raise ValueError("a runnable workflow requires its doctrine")
            if self._conversations is None:
                raise ValueError("a runnable workflow requires the service")
            if self._execution_store is None:
                raise ValueError("a runnable workflow requires the execution store")
            assert self._delivery is not None
            assert self._subagent_lifecycle is not None
            self._nodes = WorkflowNodeManager(
                ports.chat,
                self._subagent_lifecycle,
                role=WORKFLOW_NODE_ROLE,
                actor_id=doctrine.actor,
                initial_phase=doctrine.initial_phase,
                clock=ports.clock,
            )

    # -- spec surface ------------------------------------------------------

    @property
    def extension(self) -> WorkflowExtension:
        """Return the injected extension port for the owning composition."""
        if self._doctrine is None:
            raise WorkflowEngineError("workflow has no extension binding")
        return self._doctrine.extension

    def check_phase_executor(self, request: RequestT, node_id: str, phase: str) -> None:
        """Check one live graph attempt, independently of its sibling positions."""
        self._check_acceptance(request)
        if phase not in self.graph_state(request).positions:
            raise PhaseCompletionRejected("completion phase is no longer current")
        self._check_phase_owner(request, node_id, phase)

    @property
    def spec(self) -> WorkflowSpec[Any, RequestT, StateT]:
        """Return the assembled specification this object runs."""
        return self._spec

    @property
    def workflow_id(self) -> str:
        """Return the workflow id."""
        return self._spec.workflow_id

    @property
    def version(self) -> str:
        """Return the workflow version."""
        return self._spec.version

    @property
    def title(self) -> str:
        """Return the workflow title."""
        return self._spec.title

    @property
    def phases(self) -> tuple[Any, ...]:
        """Return the declared phases in order."""
        return self._spec.phases

    @property
    def phase_names(self) -> tuple[str, ...]:
        """Return the declared phase names in order."""
        return self._spec.phase_names

    @property
    def terminal_phases(self) -> tuple[str, ...]:
        """Return the declared terminal phase names."""
        return self._spec.terminal_phases

    @property
    def route_names(self) -> Mapping[str, frozenset[str]]:
        """Return the allowed transition table."""
        return self._spec.route_names

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        if self._doctrine is not None:
            return self._doctrine.definition
        return WorkflowDefinition(
            workflow_id=self._spec.workflow_id,
            version=self._spec.version,
            title=self._spec.title,
            phases=self._spec.phase_names,
        )

    @property
    def chat(self) -> ChatService | None:
        """Return the composed chat service, when bound."""
        return self._chat

    @property
    def clock(self) -> Callable[[], datetime]:
        """Return the configured clock."""
        return self._clock

    @property
    def id_factory(self) -> Callable[[str], str]:
        """Return the configured identifier factory."""
        return self._id_factory

    @property
    def delivery(self) -> DeliveryService | None:
        """Return the composed delivery service, when bound."""
        return self._delivery

    @property
    def conversations(self) -> Conversations | None:
        """Return the one service this engine sees, when bound."""
        return self._conversations

    @property
    def execution_store(self) -> ExecutionStore | None:
        """Return the authoritative execution store, when bound."""
        return self._execution_store

    @property
    def subagent_lifecycle(self) -> SubAgentLifecycle | None:
        """Return the composed sub-agent lifecycle, when bound."""
        return self._subagent_lifecycle

    @property
    def active_orchestrators(self) -> dict[tuple[str, str], ActiveSubAgent]:
        """Return the per-execution open orchestrators."""
        return self._active_orchestrators

    # -- launch and observation -------------------------------------------

    def _record_launch(self, request: RequestT) -> None:
        """Create the execution's durable document in the store (REQ-B1).

        The identity and the launch request are written at launch, so a
        rebuild never reaches into the chat for the trigger.  A document that
        already exists (a replayed launch) is kept as it is.
        """
        store = self._execution_store
        if store is None:
            return
        identity = ExecutionIdentity(
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            chat_id=request.chat_id,
            workflow_id=self.definition.workflow_id,
            workflow_version=self.definition.version,
            requested_by=request.requested_by,
        )
        payload = getattr(request, "as_dict", None)
        launch_payload = dict(payload()) if callable(payload) else {}
        try:
            store.create(identity, launch_request=launch_payload)
        except ExecutionStoreConflict:
            existing = store.get(request.execution_id)
            if (
                existing.identity != identity
                or existing.launch_request != launch_payload
            ):
                raise WorkflowEngineError(
                    "execution identity conflicts with its durable launch"
                ) from None
            return

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        inputs: Mapping[str, object],
        trigger_message_id: str | None = None,
        references: Mapping[str, tuple[str, ...]] | None = None,
        anchor_sub_agent_id: str | None = None,
    ) -> RequestT:
        """Bind the execution to the requesting message and open its node.

        Every launch binds to the operator's existing message; there is no
        synthetic-trigger fallback.  The execution's durable workflow node is
        parented to the caller's anchor, so the chain is launching agent →
        workflow node → phase agents.
        """
        doctrine, chat = self._require_doctrine_chat()
        self._extension(
            "validate_launch",
            {
                "inputs": dict(inputs),
                "triggerMessageId": trigger_message_id,
                "anchorSubAgentId": anchor_sub_agent_id,
            },
        )
        execution = chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=doctrine.definition,
            actor_id=actor_id,
            message_id=trigger_message_id,
            references=references,
        )
        request = cast(
            RequestT,
            RunRequest.bind(
                execution,
                self._extension(
                    "request",
                    {"execution": execution.as_dict(), "inputs": dict(inputs)},
                ),
            ),
        )
        self._record_launch(request)
        self._create_workflow_node(request, anchor_sub_agent_id)
        chat.start_workflow(workspace_id, request.execution_id, actor_id=actor_id)
        return request

    def execute(
        self,
        context: WorkflowContext,
        /,
        *,
        inputs: Mapping[str, object],
        references: Mapping[str, tuple[str, ...]] | None = None,
    ) -> Mapping[str, object]:
        """Launch one execution at ``context``'s anchor and return its handle."""
        request = self.launch(
            context.workspace_id,
            context.chat_id,
            actor_id=context.actor_id,
            inputs=inputs,
            trigger_message_id=context.trigger_message_id,
            references=references,
            anchor_sub_agent_id=context.anchor_sub_agent_id,
        )
        _, chat = self._require_doctrine_chat()
        return {
            "executionId": cast(WorkflowRequest, request).execution_id,
            "activePhases": list(
                chat.get_execution(
                    cast(WorkflowRequest, request).workspace_id,
                    cast(WorkflowRequest, request).execution_id,
                ).active_phases
            ),
        }

    def steer(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        body: str,
        message_id: str | None = None,
    ) -> WorkflowExecution:
        """Record immediate, non-destructive steering on the execution."""
        _, chat = self._require_doctrine_chat()
        if message_id is None:
            conversations = self._conversations
            if conversations is None:
                raise WorkflowEngineError("workflow has no conversation service bound")
            execution = chat.get_execution(workspace_id, execution_id)
            root = conversations.root(workspace_id, execution.chat_id)
            percolation = chat.post_steering_message(
                workspace_id,
                execution.chat_id,
                actor_id=actor_id,
                body=body,
                targets_execution_id=execution_id,
                message_factory=partial(root.send, schedule=False),
            )
            # The worker must observe the committed steering epoch and event.
            conversations.schedule_input(percolation.message)
            return next(
                item
                for item in percolation.executions
                if item.execution_id == execution_id
            )
        return chat.steer_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            body=body,
            message_id=message_id,
        )

    def observe(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        """Return an ordered replay page for reconnecting clients."""
        _, chat = self._require_doctrine_chat()
        return chat.replay_events(
            workspace_id,
            execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def prepare_phase(self, request: RequestT) -> str | None:
        """Reconcile and dispatch the current durable graph frontier."""
        if self._doctrine is None or self._chat is None:
            return None
        if self._execution_cancelled_or_deleted(request):
            return None
        graph = self.graph_state(request)
        assert self._execution_store is not None
        sequence = graph.sequence
        execution = self._require_execution(request)
        if (
            execution.active_phases != tuple(graph.positions)
            or execution.control_sequence != sequence
        ):
            self._chat.set_execution_phases(
                request.workspace_id,
                request.execution_id,
                tuple(graph.positions),
                sequence=sequence,
            )
        # A crash after store acceptance may have left the executor open.
        if (sequence or graph.scopes) and not any(
            event.event_type is ExecutionEventType.WORKFLOW_PHASE
            and event.payload.get("controlSequence") == sequence
            for event in self._chat.event_store.replay(request.execution_id).events
        ):
            history = self._execution_store.get(request.execution_id).transitions
            self._emit_phase(
                request,
                next(
                    iter(graph.positions),
                    history[-1].from_phase if history else self._spec.phases[0].phase,
                ),
                graph_payload(graph),
            )
        assert self._execution_store is not None
        for transition in self._execution_store.get(request.execution_id).transitions:
            if transition.executor_id is None or self._subagent_lifecycle is None:
                continue
            with self._chat.execution_guard(request.workspace_id, request.execution_id):
                record = self._subagent_lifecycle.get(
                    request.workspace_id, transition.executor_id
                )
                if (
                    record is not None
                    and record.phase_attempt_id == transition.attempt_id
                    and record.status
                    not in {
                        SubAgentStatus.COMPLETED,
                        SubAgentStatus.FAILED,
                        SubAgentStatus.CANCELLED,
                    }
                ):
                    self._subagent_lifecycle.complete(
                        request.workspace_id,
                        record.sub_agent_id,
                        result=transition.completion,
                    )
        ids = []
        for phase, position in tuple(graph.positions.items()):
            if phase in self.terminal_phases:
                if (
                    graph.scopes
                    or graph.pending
                    or any(name not in self.terminal_phases for name in graph.positions)
                ):
                    continue
                self._finish_terminal(
                    request, phase, self._handoff_output(position), position.source
                )
                continue
            current = self.graph_state(request).positions.get(phase)
            if current is None or current.attempt_id != position.attempt_id:
                continue
            try:
                state = self._reconstruct_state(request, phase)
            except PhaseCompletionRejected:
                continue
            context = HandoffContext(
                source=position.source,
                target=phase,
                completion=self._handoff_output(position),
                inputs=position.inputs,
                trigger_message_id=request.trigger_message_id,
            )
            prompt = self._phase_prompt(request, phase, state, context)
            if self._work_store is not None:
                self._enqueue_phase(request, phase, prompt, context)
            else:
                from thesis_runtime.storage.ownership import OwnershipLost

                try:
                    result = self._enter_phase(request, phase, state, prompt, context)
                except OwnershipLost:
                    raise
                except Exception as error:
                    current = self.graph_state(request).positions.get(phase)
                    if current is None or current.attempt_id != position.attempt_id:
                        self.prepare_phase(request)
                        continue
                    self.fail_execution(
                        request, reason=str(error) or type(error).__name__
                    )
                    raise
                if result is not None:
                    ids.append(result)
        return ids[0] if len(ids) == 1 else None

    def _phase_prompt(
        self,
        request: RequestT,
        phase: object,
        state: StateT,
        context: HandoffContext,
        *,
        override: str | None = None,
    ) -> PhaseSeed | None:
        """Compose the phase agent's first prompt from the accepted handoff.

        The start/transition adapter's text wins; otherwise the phase's
        declared seed builds the prompt, and a phase with neither falls back to
        its doctrine task text.  The engine appends the completion obligation.
        A seed never reads another agent's transcript.
        """
        if self._doctrine is None:
            return None
        phase_value = self._phase_value(phase)
        seed = self._extension("seed", self._extension_input(request, state, context))
        task = override or seed.get("text")
        if task is not None and not isinstance(task, str):
            raise WorkflowEngineError("extension seed text must be a string")
        task = task or self._doctrine.tasks.get(phase_value)
        if not task:
            return None
        instruction = self._completion_instruction(phase_value)
        identities = seed.get("contextMessageIds", [])
        if not isinstance(identities, list) or any(
            not isinstance(item, str) for item in identities
        ):
            raise WorkflowEngineError(
                "contextMessageIds must contain explicit source message IDs"
            )
        _, chat = self._require_doctrine_chat()
        initial_context = []
        for message_id in identities:
            original = chat.get_message(request.workspace_id, message_id)
            if original.chat_id != request.chat_id:
                raise WorkflowEngineError("initial context belongs to another chat")
            initial_context.append(
                InitialContext(
                    from_record(original).content.text,
                    original.message_id,
                    original.conversation_id,
                )
            )
        return PhaseSeed(
            f"{task}\n\n{instruction}" if instruction else task, tuple(initial_context)
        )

    def _phase_spawn_spec(
        self, request: RequestT, phase_value: str
    ) -> tuple[SubAgentSpec, SubAgentContextSpec, str]:
        """Build the phase child's durable spec, its context, and its label.

        The durable record's kind mirrors the declared phase type — a
        conversational phase's child accepts input from birth, a one-shot
        phase's child follows the one-shot input rule — while the child's
        turn-end policy is conversational for every phase child, because a
        phase completes only through the completion tool.
        """
        doctrine, _ = self._require_doctrine_chat()
        family, label = doctrine.orchestrators[phase_value]
        tools = _phase_mcp_tools(doctrine, phase_value, family)
        declared = self._spec.phase_specs[phase_value]
        kind = (
            SubAgentKind.CONVERSATION
            if isinstance(declared, ConversationPhase)
            else SubAgentKind.ONE_SHOT
        )
        spec = SubAgentSpec(
            role=label,
            kind=kind,
            phase=phase_value,
            allowed_tools=_phase_allowed_tools(doctrine, phase_value, family),
        )
        context = SubAgentContextSpec(
            knowledge_cutoff=None, mcp_enabled=bool(tools), mcp_tools=tools
        )
        return spec, context, label

    def graph_state(self, request: RequestT) -> Graph[StoreTransition]:
        """Authoritative control projection shared by execution and recovery."""
        assert self._execution_store is not None
        return replay_graph(
            request.execution_id,
            self._spec.phases[0].phase,
            self._spec.graph_nodes,
            self._execution_store.get(request.execution_id).transitions,
            self._request_payload(request),
        )

    @staticmethod
    def _handoff_output(position: Position[StoreTransition]) -> Mapping[str, object]:
        """Project the product's primary handoff; named inputs remain explicit."""
        if tuple(position.inputs) == ("input",):
            return position.inputs["input"]
        return dict(position.inputs)

    def causal_history(
        self, request: RequestT, phase: str
    ) -> tuple[StoreTransition, ...]:
        """Only accepted ancestors of this current attempt, excluding siblings."""
        return self.graph_state(request).positions[phase].history

    def visible_history(
        self, request: RequestT, phase: str
    ) -> tuple[StoreTransition, ...]:
        """Accepted ancestors and explicitly joined dependencies for scoped reads."""
        position = self.graph_state(request).positions[phase]
        allowed = {item.sequence for item in position.history} | set(
            position.dependencies
        )
        assert self._execution_store is not None
        return tuple(
            item
            for item in self._execution_store.get(request.execution_id).transitions
            if item.sequence in allowed
        )

    def _phase_attempt_id(self, request: RequestT, phase_value: str) -> str:
        position = self.graph_state(request).positions.get(phase_value)
        if position is None:
            raise PhaseCompletionRejected("phase attempt is no longer current")
        return position.attempt_id

    def _completion_instruction(self, phase_value: str) -> str:
        """Describe the phase's completion obligation from its spec data.

        The instruction names the tool, the required contract fields, and the
        deterministic target, so every phase prompt (handoff override, declared
        seed, or doctrine task) tells its agent how to advance the workflow.
        Terminal phases have nothing to complete.
        """
        if phase_value in self.terminal_phases:
            return ""
        targets = sorted(self._spec.route_names.get(phase_value, frozenset()))
        if not targets:
            return ""
        # The engine still accepts a terminal failure target, but the agent is
        # never invited to name it: a phase agent that cannot proceed asks the
        # operator instead of ending the run.
        named = [target for target in targets if target != "failed"] or targets
        lines = [
            "Complete this phase only by calling request_phase_completion with a "
            "completion object."
        ]
        required = self._spec.required.get(phase_value, ())
        if required:
            lines.append(
                "The completion object must carry: " + ", ".join(required) + "."
            )
        declared = self._spec.on_complete.get(phase_value)
        if declared is not None:
            lines.append(f"Use target {declared!r}.")
        else:
            lines.append("Name the target in the completion: " + ", ".join(named) + ".")
            if "failed" in targets and "failed" not in named:
                lines.append(
                    "Never name the failed target yourself; if the work cannot "
                    "proceed, ask the operator through request_workflow_input "
                    "instead."
                )
        return " ".join(lines)

    def apply_phase_completion(
        self,
        request: RequestT,
        node_id: str,
        completion: Mapping[str, object],
        *,
        attempt_id: str,
    ) -> Mapping[str, object]:
        """Accept one branch completion; repeat requests return their receipt."""
        if self._subagent_lifecycle is None or self._execution_store is None:
            raise WorkflowEngineError("phase completion requires durable services")
        self._require_execution(request)
        record = self._subagent_lifecycle.get(request.workspace_id, node_id)
        if (
            record is None
            or not record.phase_executor
            or record.workflow_execution_id != request.execution_id
        ):
            raise PhaseCompletionRejected(
                "completion does not belong to a phase executor"
            )
        accepted = next(
            (
                item
                for item in self._execution_store.get(request.execution_id).transitions
                if item.attempt_id == attempt_id and item.executor_id == node_id
            ),
            None,
        )
        if accepted is not None:
            if dict(accepted.completion) != dict(completion):
                raise PhaseCompletionRejected(
                    "completion conflicts with the accepted attempt"
                )
            return {
                "executionId": request.execution_id,
                "phase": accepted.route,
                "applied": True,
            }
        self._check_acceptance(request, completion=completion)
        if record.phase_attempt_id != attempt_id:
            raise PhaseCompletionRejected(
                "completion does not belong to the current phase attempt"
            )
        phase = record.phase
        assert phase is not None
        self._check_phase_owner(request, node_id, phase)
        state = self._reconstruct_state(request, phase)
        _, context, _ = self._phase_spawn_spec(request, phase)
        assert record.phase_attempt_id is not None
        self._active_orchestrators[(request.execution_id, phase)] = ActiveSubAgent(
            node_id, phase, record.role, context, record.phase_attempt_id
        )
        try:
            handed = self._apply_contract(
                request, state, phase, completion, node_id=node_id
            )
        except PhaseCompletionRejected:
            receipt = next(
                (
                    item
                    for item in self._execution_store.get(
                        request.execution_id
                    ).transitions
                    if item.attempt_id == record.phase_attempt_id
                    and item.executor_id == node_id
                ),
                None,
            )
            if receipt is None:
                raise
            if dict(receipt.completion) != dict(completion):
                raise PhaseCompletionRejected(
                    "completion conflicts with the accepted attempt"
                ) from None
            return {
                "executionId": request.execution_id,
                "phase": receipt.route,
                "applied": True,
            }
        target = self._phase_value(handed.phase)
        self._dispatch_frontier(request)
        return {"executionId": request.execution_id, "phase": target, "applied": True}

    def _enqueue_phase(
        self,
        request: RequestT,
        phase: str,
        prompt: PhaseSeed | None,
        context: HandoffContext,
    ) -> None:
        if self._work_store is None:
            raise WorkflowEngineError("durable phase dispatch is not composed")
        attempt = self._phase_attempt_id(request, phase)
        key = f"phase:{attempt}:{request.intent_epoch}"
        replay_safe = {
            item.phase
            for item in self._spec.phases
            if isinstance(item, DeterministicPhase) and item.replay_safe
        }
        generation = 0
        while True:
            try:
                work = self._work_store.get(request.workspace_id, key)
            except LookupError:
                break
            if work.state != "interrupted" or phase not in replay_safe:
                return
            generation += 1
            key = f"phase:{attempt}:{request.intent_epoch}:recovery:{generation}"
        self._work_store.enqueue(
            Work(
                request.workspace_id,
                key,
                f"phase:{attempt}",
                "workflow",
                {
                    "execution_id": request.execution_id,
                    "intent_epoch": request.intent_epoch,
                    "phase": phase,
                    "attempt_id": attempt,
                    "prompt": None if prompt is None else prompt.text,
                    "initial_context": []
                    if prompt is None
                    else [item.as_dict() for item in prompt.initial_context],
                    "context": {
                        "source": context.source,
                        "target": context.target,
                        "completion": dict(context.completion),
                        "inputs": {
                            name: dict(value) for name, value in context.inputs.items()
                        },
                        "trigger_message_id": context.trigger_message_id,
                    },
                },
            )
        )

    def enter_scheduled_phase(
        self, request: RequestT, payload: Mapping[str, object]
    ) -> None:
        """A queued dispatch is valid only for its exact current node attempt."""
        phase = str(payload["phase"])
        position = self.graph_state(request).positions.get(phase)
        if position is None or position.attempt_id != payload["attempt_id"]:
            return
        if payload["intent_epoch"] != request.intent_epoch:
            raise PhaseCompletionRejected("queued phase intent epoch is stale")
        raw = cast(Mapping[str, Any], payload["context"])
        context = HandoffContext(
            source=raw["source"],
            target=raw["target"],
            completion=raw["completion"],
            inputs=raw["inputs"],
            trigger_message_id=raw["trigger_message_id"],
        )
        from thesis_runtime.storage.ownership import OwnershipLost

        try:
            self._enter_phase(
                request,
                phase,
                self._reconstruct_state(request, phase),
                None
                if payload["prompt"] is None
                else PhaseSeed.from_dict(
                    {
                        "text": payload["prompt"],
                        "initialContext": payload.get("initial_context", []),
                    }
                ),
                context,
            )
        except OwnershipLost:
            raise
        except Exception as error:
            current = self.graph_state(request).positions.get(phase)
            if current is None or current.attempt_id != position.attempt_id:
                self.prepare_phase(request)
                return
            self.fail_execution(request, reason=str(error) or type(error).__name__)
            raise

    def _dispatch_frontier(self, request: RequestT) -> None:
        """Successor entry stays outside completion acceptance and its guard."""
        if self._execution_cancelled_or_deleted(request):
            return
        if self._detach is None or self._work_store is not None:
            self.prepare_phase(request)
            return

        def run() -> None:
            try:
                self.prepare_phase(request)
            except Exception as error:
                self.fail_execution(request, reason=str(error) or type(error).__name__)

        self._detach(run)

    def _apply_contract(
        self,
        request: RequestT,
        state: StateT,
        phase: str,
        completion: Mapping[str, object],
        *,
        node_id: str | None = None,
    ) -> StateT:
        """Validate one contract and apply its declared transition and pipe.

        A rejection is raised as :class:`PhaseCompletionRejected`; an accepted
        contract advances the state through the declared transition, applies
        the transition's pipe, and returns the handed-off state.  A terminal
        phase closes the execution instead.
        """
        self._check_acceptance(request, completion=completion)
        failure = self._validate_contract(phase, completion)
        if failure:
            raise PhaseCompletionRejected(failure)
        selected = ActivityCompletion(
            self._resolve_target(phase, completion), completion
        )
        target = selected.route
        advanced = self._transition(
            request,
            state,
            target,
            f"phase completion accepted for {phase!r}",
            completion=completion,
            node_id=node_id,
        )
        handed = advanced
        if self._phase_value(handed.phase) in self.terminal_phases:
            self._finish_terminal(
                request, self._phase_value(handed.phase), completion, phase
            )
        return handed

    def _finish_terminal(
        self,
        request: RequestT,
        phase: str,
        completion: Mapping[str, object],
        source: str | None,
    ) -> None:
        """Finalize accepted terminal arrivals, including interrupted publication."""
        terminal = self._spec.phase_specs[phase]
        assert isinstance(terminal, TerminalPhase)
        failed = terminal.outcome == "failed"
        state = (
            WorkflowExecutionState.FAILED
            if failed
            else WorkflowExecutionState.COMPLETED
        )
        self._finish_execution(
            request,
            state,
            ExecutionEventType.WORKFLOW_FAILED
            if failed
            else ExecutionEventType.WORKFLOW_COMPLETED,
            payload={
                "state": state.value,
                "finalPhase": source,
                "completion": dict(completion),
            },
        )

    def _validate_contract(
        self, phase: str, completion: Mapping[str, object]
    ) -> str | None:
        """Return the repair reason a contract is rejected, or ``None``."""
        if not completion:
            return "completion contract must not be empty"
        failure = self._extension(
            "validate", {"phase": phase, "completion": dict(completion)}
        ).get("failure")
        if failure is not None:
            if not isinstance(failure, str):
                raise WorkflowEngineError("extension validation failure must be text")
            if failure:
                return failure
        required = self._spec.required.get(phase, ())
        missing = [
            name for name in required if not str(completion.get(name, "")).strip()
        ]
        if missing:
            return "completion is missing required field(s): " + ", ".join(missing)
        return None

    def _resolve_target(self, phase: str, completion: Mapping[str, object]) -> str:
        """Return the declared transition target for an accepted contract."""
        allowed = sorted(self._spec.route_names.get(phase, frozenset()))
        if not allowed:
            raise PhaseCompletionRejected(f"phase {phase!r} has no declared transition")
        requested_target = completion.get("target")
        declared_target = self._spec.on_complete.get(phase)
        target = (
            str(requested_target)
            if isinstance(requested_target, str) and requested_target.strip()
            else declared_target
            if declared_target is not None
            else allowed[0]
            if len(allowed) == 1
            else None
        )
        if target is None:
            raise PhaseCompletionRejected(
                f"phase {phase!r} allows {', '.join(allowed)}; name the target "
                "in the completion contract"
            )
        if target not in allowed:
            raise PhaseCompletionRejected(
                f"phase {phase!r} cannot move to {target!r}; allowed: "
                + ", ".join(allowed)
            )
        return target

    def _enter_phase(
        self,
        request: RequestT,
        phase: object,
        state: StateT,
        prompt: PhaseSeed | None,
        context: HandoffContext,
    ) -> str | None:
        """Enter one node; durable workers serialize deterministic attempt work."""
        if self._execution_cancelled_or_deleted(request):
            return None
        name = self._phase_value(phase)
        if (
            name in self.terminal_phases
            or name not in self.graph_state(request).positions
        ):
            return None
        spec = self._spec.phase_specs[name]
        if not isinstance(spec, DeterministicPhase):
            if prompt is None:
                raise WorkflowEngineError("agent phase entry requires a prompt")
            return self._start_phase_agent(request, phase, prompt=prompt).sub_agent_id
        attempt = self._phase_attempt_id(request, name)
        # Embedders have no durable worker; this is only local coalescing.
        _, chat = self._require_doctrine_chat()
        with chat.execution_guard(request.workspace_id, request.execution_id):
            if attempt in self._running_deterministic:
                return None
            self._running_deterministic.add(attempt)
        try:
            contract = self._extension(
                "execute", self._extension_input(request, state, context)
            ).get("completion")
            if self._execution_cancelled_or_deleted(request):
                return None
            if not isinstance(contract, Mapping):
                raise WorkflowEngineError(
                    f"deterministic phase {name!r} returned no contract"
                )
            try:
                self._apply_contract(request, state, name, contract)
            except PhaseCompletionRejected as error:
                raise WorkflowEngineError(
                    f"deterministic phase {name!r} produced an invalid contract: {error}"
                ) from error
        finally:
            with chat.execution_guard(request.workspace_id, request.execution_id):
                self._running_deterministic.discard(attempt)
        # Preserve serial deterministic cascades within their admitted dispatch.
        # Production still enqueues each successor through the same work store.
        self.prepare_phase(request)
        return None

    def fail_execution(self, request: RequestT, *, reason: str) -> None:
        """Fail the execution on a phase-agent session fault.

        A fault fails the node that raised it and the execution: the open
        orchestrator closes as failed, the execution ends ``failed`` with the
        workflow-failed event, and the failure is delivered once at the
        execution's anchor — the only upward delivery this workflow makes.
        """
        if self._doctrine is None or self._chat is None:
            return
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return
        if execution.state in TERMINAL_EXECUTION_STATES:
            return
        node_id = self._workflow_node_id(request)
        parent_sub_agent_id: str | None = None
        if node_id is not None and self._subagent_lifecycle is not None:
            record = self._subagent_lifecycle.get(request.workspace_id, node_id)
            parent_sub_agent_id = None if record is None else record.parent_sub_agent_id
        self.finish_orchestrator(
            request,
            status=STATUS_FAILED,
            detail=reason,
            summary=f"Workflow failed: {reason}",
        )
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": reason, "state": "failed"},
        )
        if self._conversations is None:
            return
        execution_phase = "failed"
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            pass
        else:
            execution_phase = execution.phase or "failed"
        text = f"{self.title} {request.execution_id} failed: {reason}."
        self._conversations.deliver(
            Anchor(
                request.workspace_id,
                request.chat_id,
                sub_agent_id=parent_sub_agent_id,
            ),
            Datum(
                datum_type=DatumType.WORKFLOW_OUTCOME.value,
                ok=False,
                structured={
                    "executionId": request.execution_id,
                    "status": "failed",
                    "phase": execution_phase,
                },
                text=text,
                summary=text,
                failure={"kind": "workflow", "message": reason},
            ),
            execution_id=request.execution_id,
        )

    # -- trusted context and execution checks ------------------------------

    def _extension(
        self, operation: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        doctrine, _ = self._require_doctrine_chat()
        return invoke_extension(doctrine.extension, operation, payload)

    @staticmethod
    def _request_payload(request: RequestT) -> Mapping[str, object]:
        encoder = getattr(request, "as_dict", None)
        if not callable(encoder):
            raise WorkflowEngineError("workflow request requires a JSON encoder")
        payload = encoder()
        if not isinstance(payload, Mapping):
            raise WorkflowEngineError("workflow request must encode as an object")
        return payload

    def _extension_input(
        self, request: RequestT, state: StateT, handoff: HandoffContext | None = None
    ) -> Mapping[str, object]:
        return {
            "inputs": {}
            if handoff is None
            else {name: dict(value) for name, value in handoff.inputs.items()},
            "request": self._request_payload(request),
            "phase": self._phase_value(state.phase),
            "history": [
                cast(StoreTransition, item).as_dict() for item in state.transitions
            ],
            "handoff": None
            if handoff is None
            else {
                "source": handoff.source,
                "target": handoff.target,
                "completion": dict(handoff.completion),
                "inputs": {name: dict(value) for name, value in handoff.inputs.items()},
                "triggerMessageId": handoff.trigger_message_id,
                "subAgentId": handoff.sub_agent_id,
            },
        }

    def _context(self, request: RequestT) -> WorkflowContext:
        """Return the trusted engine context for one execution turn."""
        return WorkflowContext(
            workspace_id=request.workspace_id,
            chat_id=request.chat_id,
            actor_id=request.requested_by,
            execution_id=request.execution_id,
            trigger_message_id=request.trigger_message_id,
            intent_epoch=request.intent_epoch,
            delivery=self._delivery,
        )

    def _create_workflow_node(
        self,
        request: RequestT,
        anchor_sub_agent_id: str | None,
    ) -> SubAgentRecord | None:
        """Create the execution's durable node as a child of the launching anchor."""
        assert self._nodes is not None
        return self._nodes.create(self._context(request), anchor_sub_agent_id)

    def _workflow_node_id(self, request: RequestT) -> str | None:
        """Resolve the durable workflow node owning this execution's phases."""
        assert self._nodes is not None
        return self._nodes.resolve(self._context(request))

    def _require_execution(self, request: RequestT) -> WorkflowExecution:
        _, chat = self._require_doctrine_chat()
        try:
            execution = chat.get_execution(request.workspace_id, request.execution_id)
        except RecordNotFound as error:
            raise WorkflowEngineError(
                f"execution {request.execution_id!r} does not exist"
            ) from error
        if execution.chat_id != request.chat_id:
            raise WorkflowEngineError("execution does not belong to the request chat")
        if execution.trigger_message_id != request.trigger_message_id:
            raise WorkflowEngineError("trigger message does not match the execution")
        return execution

    def _execution_cancelled_or_deleted(self, request: RequestT) -> bool:
        """Return whether deletion or cancellation invalidated this worker."""
        _, chat = self._require_doctrine_chat()
        try:
            execution = chat.get_execution(request.workspace_id, request.execution_id)
        except RecordNotFound:
            return True
        return execution.state in TERMINAL_EXECUTION_STATES

    # -- transition mechanics -----------------------------------------------

    def _reconstruct_state(self, request: RequestT, phase: str | None = None) -> StateT:
        graph = self.graph_state(request)
        if phase is None:
            if len(graph.positions) != 1:
                raise WorkflowEngineError(
                    "a parallel frontier requires an explicit phase"
                )
            phase = next(iter(graph.positions))
        position = graph.positions.get(phase)
        if position is None:
            raise PhaseCompletionRejected("phase attempt is no longer current")
        payload = self._extension(
            "state",
            {
                "request": self._request_payload(request),
                "phase": phase,
                "history": [item.as_dict() for item in position.history],
            },
        )
        return cast(
            StateT, StateSnapshot.decode({**payload, "phase": phase}, position.history)
        )

    def _check_acceptance(
        self, request: RequestT, *, completion: Mapping[str, object] | None = None
    ) -> None:
        execution = self._require_execution(request)
        if execution.state in TERMINAL_EXECUTION_STATES:
            raise PhaseCompletionRejected(
                "execution is no longer accepting completions"
            )
        if execution.intent_epoch != request.intent_epoch:
            raise PhaseCompletionRejected("completion intent epoch is stale")
        if completion is not None and "intentEpoch" in completion:
            epoch = completion["intentEpoch"]
            if (
                isinstance(epoch, bool)
                or not isinstance(epoch, int)
                or epoch != request.intent_epoch
            ):
                raise PhaseCompletionRejected("completion intent epoch is stale")

    def _check_phase_owner(self, request: RequestT, node_id: str, phase: str) -> None:
        record = (
            None
            if self._subagent_lifecycle is None
            else self._subagent_lifecycle.get(request.workspace_id, node_id)
        )
        if (
            record is None
            or not record.phase_executor
            or record.workflow_execution_id != request.execution_id
            or record.chat_id != request.chat_id
            or record.parent_sub_agent_id != self._workflow_node_id(request)
            or record.phase != phase
            or record.phase_attempt_id != self._phase_attempt_id(request, phase)
            or record.status
            in {
                SubAgentStatus.COMPLETED,
                SubAgentStatus.FAILED,
                SubAgentStatus.CANCELLED,
            }
        ):
            raise PhaseCompletionRejected(
                "completion does not belong to the current phase executor"
            )

    def _transition(
        self,
        request: RequestT,
        state: StateT,
        target: object,
        reason: str,
        *,
        completion: Mapping[str, object] | None = None,
        node_id: str | None = None,
    ) -> StateT:
        self._check_acceptance(request, completion=completion)
        assert self._execution_store is not None
        source_phase = self._phase_value(state.phase)
        target_phase = self._phase_value(target)
        for _ in range(8):
            observed = self._execution_store.get(request.execution_id)
            graph = replay_graph(
                request.execution_id,
                self._spec.phases[0].phase,
                self._spec.graph_nodes,
                observed.transitions,
                self._request_payload(request),
            )
            position = graph.positions.get(source_phase)
            if position is None or position.history != tuple(state.transitions):
                raise PhaseCompletionRejected("phase state changed before acceptance")
            transition = StoreTransition(
                sequence=len(observed.transitions) + 1,
                from_phase=source_phase,
                route=target_phase,
                occurred_at=self._clock(),
                intent_epoch=request.intent_epoch,
                reason=reason,
                completion=dict(completion or {}),
                attempt_id=position.attempt_id,
                executor_id=node_id,
            )
            advanced_graph = replay_graph(
                request.execution_id,
                self._spec.phases[0].phase,
                self._spec.graph_nodes,
                (*observed.transitions, transition),
                self._request_payload(request),
            )
            scoped = (*position.history, transition)
            payload = self._extension(
                "state",
                {
                    "request": self._request_payload(request),
                    "phase": target_phase,
                    "history": [item.as_dict() for item in scoped],
                },
            )
            advanced = cast(
                StateT, StateSnapshot.decode({**payload, "phase": target_phase}, scoped)
            )
            try:
                self._commit_transition(
                    request,
                    transition,
                    expected_revision=observed.revision,
                    node_id=node_id,
                    active_phases=tuple(advanced_graph.positions),
                )
            except ExecutionStoreConflict:
                continue
            self._emit_phase(
                request,
                next(iter(advanced_graph.positions), source_phase),
                {
                    "route": target_phase,
                    "reason": reason,
                    **graph_payload(advanced_graph),
                    "attemptId": position.attempt_id,
                },
            )
            self._on_phase_entered(
                request, advanced, completion=completion, source_phase=source_phase
            )
            return advanced
        raise WorkflowEngineError("completion remained contended")

    def _commit_transition(
        self,
        request: RequestT,
        transition: StoreTransition,
        *,
        expected_revision: int,
        active_phases: tuple[str, ...],
        node_id: str | None = None,
    ) -> None:
        store = self._execution_store
        assert store is not None
        _, chat = self._require_doctrine_chat()

        def commit() -> ExecutionDocument:
            return store.append_transition(
                request.execution_id, transition, expected_revision=expected_revision
            )

        with chat.execution_guard(request.workspace_id, request.execution_id):
            self._check_acceptance(request, completion=transition.completion)
            if node_id is not None:
                self._check_phase_owner(request, node_id, transition.from_phase)
            try:
                chat.accept_transition(
                    request.workspace_id,
                    request.execution_id,
                    expected_intent_epoch=request.intent_epoch,
                    expected_phase=transition.from_phase,
                    expected_node_id=node_id,
                    expected_attempt_id=None
                    if node_id is None
                    else transition.attempt_id,
                    target_phases=active_phases,
                    target_sequence=transition.sequence,
                    commit=commit,
                )
            except InvalidLifecycleTransition as error:
                raise PhaseCompletionRejected(str(error)) from error
            if node_id is not None:
                assert self._subagent_lifecycle is not None
                self._subagent_lifecycle.complete(
                    request.workspace_id, node_id, result=transition.completion
                )

    def _emit_phase(
        self,
        request: RequestT,
        phase: object,
        payload: Mapping[str, object],
    ) -> None:
        _, chat = self._require_doctrine_chat()
        emit_phase(
            chat=chat,
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=self._phase_value(phase),
            payload=payload,
            actor_id=self._actor,
            intent_epoch=request.intent_epoch,
            clock=self._clock,
            id_factory=self._id_factory,
        )

    def _finish_execution(
        self,
        request: RequestT,
        final_state: WorkflowExecutionState,
        event_type: ExecutionEventType,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        _, chat = self._require_doctrine_chat()
        finish_execution(
            chat=chat,
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            final_state=final_state,
            event_type=event_type,
            payload=payload,
            actor_id=self._actor,
            clock=self._clock,
            id_factory=self._id_factory,
            on_finished=lambda: self._finish_workflow_node(request, final_state),
        )

    def _finish_workflow_node(
        self,
        request: RequestT,
        final_state: WorkflowExecutionState,
    ) -> None:
        """Close the execution's durable workflow node when the run ends."""
        assert self._nodes is not None
        self._nodes.finish(self._context(request), final_state)

    def _start_phase_agent(
        self, request: RequestT, phase: object, *, prompt: PhaseSeed
    ) -> ActiveSubAgent:
        """Start or revisit a phase through its one spawn-with-prompt path."""
        doctrine, chat = self._require_doctrine_chat()
        if self._conversations is None or not prompt.text.strip():
            raise WorkflowEngineError("agent phase entry requires a service and prompt")
        phase_value = self._phase_value(phase)
        node_id = self._workflow_node_id(request)
        existing = next(
            (
                record
                for record in self._conversations.store.list_sub_agents(
                    request.workspace_id
                )
                if record.workflow_execution_id == request.execution_id
                and record.phase_executor
                and record.phase == phase_value
                and record.parent_sub_agent_id == node_id
            ),
            None,
        )
        previous = None if existing is None else existing.sub_agent_id
        revisiting = (
            existing is not None
            and existing.phase_attempt_id
            == self._phase_attempt_id(request, phase_value)
        )
        spec, context, label = self._phase_spawn_spec(request, phase_value)
        ordinal = self._phase_attempt_id(request, phase_value)
        child = self._conversations.spawn(
            Anchor(request.workspace_id, request.chat_id, sub_agent_id=node_id),
            spec,
            prompt.text,
            SubAgentKind.CONVERSATION,
            sub_agent_id=previous,
            actor_id=self._actor,
            completion_mode=CompletionMode.EXPLICIT_COMPLETION,
            phase_executor=True,
            initial_context=prompt.initial_context,
            prompt_key=f"phase-seed-{request.execution_id}-{phase_value}-{ordinal}",
            prompt_data={"phase": phase_value, "attempt": ordinal},
            attribution={
                "workflow_execution_id": request.execution_id,
                "phase_attempt_id": self._phase_attempt_id(request, phase_value),
            },
        )
        active = ActiveSubAgent(
            sub_agent_id=child.conversation_id,
            phase=phase,
            role=label,
            context=context,
            attempt_id=ordinal,
        )
        self._active_orchestrators[(request.execution_id, phase_value)] = active
        if revisiting:
            return active
        self.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_SPAWNED,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=phase,
            role=label,
            context=context,
            accepts_input=phase_value in doctrine.conversational_phases,
            extra={"phaseExecutor": True, "phaseAttemptId": active.attempt_id},
        )
        self._emit_orchestrator_progress(request, phase_value)
        return active

    def finish_orchestrator(
        self,
        request: RequestT,
        *,
        status: str,
        detail: str | None = None,
        summary: str | None = None,
        artifact: Mapping[str, object] | None = None,
        phase: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Close the execution's open orchestrator with its final projection."""
        if phase is None:
            for execution_id, name in tuple(self._active_orchestrators):
                if execution_id == request.execution_id:
                    self.finish_orchestrator(
                        request,
                        status=status,
                        detail=detail,
                        summary=summary,
                        artifact=artifact,
                        phase=name,
                    )
            return
        active = self._active_orchestrators.get((request.execution_id, phase))
        if (
            active is not None
            and attempt_id is not None
            and active.attempt_id != attempt_id
        ):
            return
        self._active_orchestrators.pop((request.execution_id, phase), None)
        if active is None:
            return
        if summary:
            artifact_payload: dict[str, object] = (
                dict(artifact)
                if artifact is not None
                else {"content": summary, "type": "text"}
            )
            if "content" not in artifact_payload:
                artifact_payload["content"] = summary
            if "type" not in artifact_payload:
                artifact_payload["type"] = "text"
            self.append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_OUTPUT,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "artifact": artifact_payload,
                    "kind": "artifact",
                    "phaseAttemptId": active.attempt_id,
                    "phaseExecutor": True,
                },
            )
        extra: dict[str, object] = {
            "frozen": True,
            "status": status,
            "phaseAttemptId": active.attempt_id,
            "phaseExecutor": True,
        }
        if detail is not None:
            extra["detail"] = detail
        event_type = (
            ExecutionEventType.SUB_AGENT_COMPLETED
            if status == STATUS_COMPLETED
            else ExecutionEventType.SUB_AGENT_FAILED
        )
        self.append_sub_agent(
            request,
            event_type,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=False,
            extra=extra,
        )
        # Accepted executors close under the store acceptance guard. Publishing
        # their reports must never close a conversation reopened by a later loop.
        if self._subagent_lifecycle is not None and status != STATUS_COMPLETED:
            self._subagent_lifecycle.fail(
                request.workspace_id,
                active.sub_agent_id,
                result=None if detail is None else {"failure": detail},
            )

    def _on_phase_entered(
        self,
        request: RequestT,
        state: StateT,
        *,
        completion: Mapping[str, object] | None = None,
        source_phase: str,
    ) -> None:
        doctrine, _ = self._require_doctrine_chat()
        phase = source_phase
        active = self._active_orchestrators.get((request.execution_id, phase))
        response = self._extension(
            "report",
            {
                **self._extension_input(request, state),
                "activePhase": None
                if active is None
                else self._phase_value(active.phase),
            },
        )
        report = response.get("artifact")
        summary = response.get("summary")
        if report is not None and not isinstance(report, Mapping):
            raise WorkflowEngineError("extension report artifact must be an object")
        if summary is not None and not isinstance(summary, str):
            raise WorkflowEngineError("extension report summary must be text")
        self.finish_orchestrator(
            request,
            status=STATUS_COMPLETED,
            summary=summary,
            artifact=report,
            phase=source_phase,
            attempt_id=cast(StoreTransition, state.transitions[-1]).attempt_id,
        )

    # -- projections --------------------------------------------------------

    def append_sub_agent(
        self,
        request: RequestT,
        event_type: ExecutionEventType,
        *,
        sub_agent_id: str,
        parent_sub_agent_id: str | None,
        phase: object,
        role: str,
        context: SubAgentContextSpec,
        accepts_input: bool = False,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        _, chat = self._require_doctrine_chat()
        append_sub_agent_event(
            chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            event_type=event_type,
            occurred_at=self._clock(),
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=(
                self._workflow_node_id(request)
                if parent_sub_agent_id is None
                else parent_sub_agent_id
            ),
            phase=self._phase_value(phase),
            role=role,
            context=context,
            accepts_input=accepts_input,
            intent_epoch=request.intent_epoch,
            extra=extra,
        )

    def emit_phase_progress(
        self,
        request: RequestT,
        phase: object,
        *,
        kind: str | None = None,
        text: str | None = None,
    ) -> None:
        """Append one execution-scoped ``workflow.progress`` event (REQ-C2).

        Deterministic phases are not sub-agents: their executors record
        progress through this engine passthrough, carrying the phase name and
        never a ``subAgentId``.
        """
        _, chat = self._require_doctrine_chat()
        append_workflow_progress(
            chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            occurred_at=self._clock(),
            phase=self._phase_value(phase),
            kind=kind,
            text=text,
            actor_id=self._actor,
            intent_epoch=request.intent_epoch,
        )

    def emit_phase_result(
        self,
        request: RequestT,
        phase: object,
        *,
        summary: str,
        artifact: Mapping[str, object],
    ) -> None:
        """Append one ``workflow.phase_result`` event and store the artifact.

        The event is the projection; the typed artifact is also recorded in
        the execution store (REQ-B1), so the phase's outcome survives chat
        deletion.  A failed computation records the error variant.
        """
        _, chat = self._require_doctrine_chat()
        phase_value = self._phase_value(phase)
        store = self._execution_store
        if store is not None:
            store.put_phase_result(request.execution_id, phase_value, artifact)
        append_workflow_phase_result(
            chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            occurred_at=self._clock(),
            phase=phase_value,
            summary=summary,
            artifact=artifact,
            actor_id=self._actor,
            intent_epoch=request.intent_epoch,
        )

    def _emit_orchestrator_progress(self, request: RequestT, phase: str) -> None:
        """Record the active phase's safe activity label."""
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get((request.execution_id, phase))
        if active is None:
            return
        activity = doctrine.activity.get(
            self._phase_value(active.phase), doctrine.default_activity
        )
        self.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=(
                self._phase_value(active.phase) in doctrine.conversational_phases
            ),
            extra={
                "kind": "activity",
                "text": activity,
                "phaseAttemptId": active.attempt_id,
                "phaseExecutor": True,
            },
        )

    def _require_doctrine_chat(
        self,
    ) -> tuple[WorkflowDoctrine[RequestT, StateT, OutcomeT], ChatService]:
        if self._doctrine is None or self._chat is None:
            raise WorkflowEngineError("workflow has no doctrine or chat service bound")
        return self._doctrine, self._chat

    @property
    def _actor(self) -> str:
        if self._doctrine is None:
            raise WorkflowEngineError("workflow has no doctrine bound")
        return self._doctrine.actor

    @staticmethod
    def _phase_value(phase: object) -> str:
        value = getattr(phase, "value", None)
        if isinstance(value, str):
            return value
        return str(phase)
