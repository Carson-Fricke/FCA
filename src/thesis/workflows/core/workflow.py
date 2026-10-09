"""The single general workflow object.

A concrete workflow is not a class: it is a :class:`Workflow` instance built by
:class:`~thesis.workflows.core.builder.WorkflowBuilder` from a
:class:`~thesis.workflows.core.spec.WorkflowSpec` plus workflow-specific data
(the doctrine of phase metadata and projection hooks) and an opaque services
bundle the phase executors read.  This object owns the engine: the ordered
phase dispatch, the durable per-turn skeleton, node lifecycle, delivery,
trusted context construction, epoch stamping, and the single scheduling point.
Nothing here knows a workflow's phases, prompts, or state shape beyond the
hooks the spec supplies.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, cast

from thesis.agent.runtime.control import PhaseCompletionRejected
from thesis.agent.runtime.control_tools import CONTROL_PHASE_TOOL_NAMES
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.datum import Datum, DatumType
from thesis.agent.runtime.delivery import (
    Anchor,
    DeliveryService,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
    CompletionMode,
    InvalidLifecycleTransition,
    MessageRole,
    RecordNotFound,
    SubAgentKind,
    SubAgentRecord,
    SubAgentStatus,
    TranscriptItemKind,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
)
from thesis.chat.service import ChatService
from thesis.execution.events import EventReplay, ExecutionEventType
from thesis.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_sub_agent_event,
)
from thesis.execution.workflow_events import (
    append_workflow_phase_result,
    append_workflow_progress,
)
from thesis.workflows.core.delivery import WorkflowNodeManager
from thesis.workflows.core.events import emit_phase, finish_execution
from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStore,
    ExecutionStoreConflict,
    StoreTransition,
)
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
)
from thesis.workflows.core.spec import (
    HandoffContext,
    WorkflowContext,
    WorkflowSpec,
)
from thesis.workflows.core.state import TERMINAL_EXECUTION_STATES, WorkflowState

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
    request_of: Callable[[WorkflowExecution, str, Mapping[str, object]], RequestT]
    context_of: Callable[
        [Workflow[RequestT, StateT, OutcomeT], RequestT], WorkflowContext
    ]
    project: Callable[[StateT], OutcomeT]
    reconstruct: Callable[[Workflow[RequestT, StateT, OutcomeT], RequestT], StateT]
    summarize: Callable[[StateT, object], str]
    report: Callable[
        [Workflow[RequestT, StateT, OutcomeT], StateT, object],
        Mapping[str, object] | None,
    ]
    validate_launch: Callable[[str, str | None, str | None, Mapping[str, object]], None]
    reduce_transition: Callable[
        [StateT, StoreTransition, Mapping[tuple[str, str], Callable[[StateT], StateT]]],
        StateT,
    ]
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

    ``prepare_phase`` spawns the current phase's agent with its prompt;
    ``apply_phase_completion`` validates one phase agent's completion and
    applies its transition; ``execute`` launches a new execution.  The object
    is re-entrant: the derived in-process state is retained per execution
    until a terminal phase, and it is rebuilt through the doctrine's
    reconstruction hook after a restart.
    """

    def __init__(
        self,
        spec: WorkflowSpec[Any, RequestT, StateT],
        *,
        doctrine: WorkflowDoctrine[RequestT, StateT, OutcomeT] | None = None,
        services: object | None = None,
        ports: WorkflowPorts | None = None,
    ) -> None:
        self._spec = spec
        self._doctrine = doctrine
        self._services = services
        self._ports = ports
        self._chat = None if ports is None else ports.chat
        self._clock = _utc_now if ports is None else ports.clock
        self._id_factory = _new_id if ports is None else ports.id_factory
        self._conversations = None if ports is None else ports.conversations
        self._execution_store = None if ports is None else ports.execution_store
        self._detach = None if ports is None else ports.detach
        lifecycle = None if ports is None else ports.subagent_lifecycle
        if lifecycle is None and self._conversations is not None:
            lifecycle = self._conversations.lifecycle
        self._subagent_lifecycle = lifecycle
        self._active_orchestrators: dict[str, ActiveSubAgent] = {}
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
    def transitions(self) -> Mapping[str, frozenset[str]]:
        """Return the allowed transition table."""
        return self._spec.transitions

    @property
    def piping(self) -> Mapping[tuple[str, str], Callable[[StateT], StateT]]:
        """Return the typed-state pipes, by transition."""
        return self._spec.piping

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
    def services(self) -> object | None:
        """Return the opaque workflow-specific services bundle."""
        return self._services

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
    def active_orchestrators(self) -> dict[str, ActiveSubAgent]:
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
        try:
            store.create(
                identity,
                launch_request=dict(payload()) if callable(payload) else {},
            )
        except ExecutionStoreConflict:
            return

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        launch_options: Mapping[str, object] | None = None,
        trigger_message_id: str | None = None,
        investigation_id: str | None = None,
        anchor_sub_agent_id: str | None = None,
    ) -> RequestT:
        """Bind the execution to the requesting message and open its node.

        Every launch binds to the operator's existing message; there is no
        synthetic-trigger fallback.  The execution's durable workflow node is
        parented to the caller's anchor, so the chain is launching agent →
        workflow node → phase agents.
        """
        doctrine, chat = self._require_doctrine_chat()
        options = dict(launch_options or {})
        doctrine.validate_launch(
            ideas, trigger_message_id, anchor_sub_agent_id, options
        )
        execution = chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=doctrine.definition,
            actor_id=actor_id,
            message_id=trigger_message_id,
            investigation_id=investigation_id,
        )
        request = doctrine.request_of(execution, ideas, options)
        self._record_launch(request)
        self._create_workflow_node(request, anchor_sub_agent_id)
        try:
            chat.start_workflow(
                workspace_id,
                request.execution_id,
                actor_id=actor_id,
            )
        except Exception:  # noqa: BLE001 - execution state is bookkeeping
            pass
        return request

    def execute(
        self,
        context: WorkflowContext,
        /,
        *,
        ideas: str,
        investigation_id: str | None = None,
        launch_options: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Launch one execution at ``context``'s anchor and return its handle."""
        request = self.launch(
            context.workspace_id,
            context.chat_id,
            actor_id=context.actor_id,
            ideas=ideas,
            launch_options=launch_options,
            trigger_message_id=context.trigger_message_id,
            investigation_id=investigation_id,
            anchor_sub_agent_id=context.anchor_sub_agent_id,
        )
        doctrine, _ = self._require_doctrine_chat()
        return {
            "executionId": cast(WorkflowRequest, request).execution_id,
            "phase": doctrine.initial_phase,
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
        """Spawn the durable current phase's agent with its first prompt.

        Spawning and prompting are one act: the agent is created and its seed
        is delivered at its own anchor, so that delivery wakes the agent's own
        turn.  The workflow never turns an agent.  Returns the agent's id, or
        ``None`` when the object has no bound doctrine or chat.
        """
        if self._doctrine is None or self._chat is None:
            return None
        state = self._reconstruct_state(request)
        phase_value = self._phase_value(state.phase)
        context = HandoffContext(
            source=None,
            target=phase_value,
            trigger_message_id=request.trigger_message_id,
        )
        override = (
            None
            if self._spec.start_handoff is None
            else self._spec.start_handoff(self, request, context)
        )
        prompt = self._phase_prompt(
            request, state.phase, state, context, override=override
        )
        try:
            return self._enter_phase(request, state.phase, state, prompt, context)
        except Exception as error:  # noqa: BLE001 - phase entry faults are terminal
            self.fail_execution(request, reason=str(error) or type(error).__name__)
            raise

    def _phase_prompt(
        self,
        request: RequestT,
        phase: object,
        state: StateT,
        context: HandoffContext,
        *,
        override: str | None = None,
    ) -> str | None:
        """Compose the phase agent's first prompt from the accepted handoff.

        The start/transition adapter's text wins; otherwise the phase's
        declared seed builds the prompt, and a phase with neither falls back to
        its doctrine task text.  The engine appends the completion obligation.
        A seed never reads another agent's transcript.
        """
        if self._doctrine is None:
            return None
        phase_value = self._phase_value(phase)
        spec = self._spec.phase_specs[phase_value]
        seed = spec.seed if isinstance(spec, ConversationPhase | OneShotPhase) else None
        task = override or (
            seed(self, request, state, context)
            if seed is not None
            else self._doctrine.tasks.get(phase_value)
        )
        if not task:
            return None
        instruction = self._completion_instruction(phase_value)
        return f"{task}\n\n{instruction}" if instruction else task

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

    def _spawn_phase_child(
        self,
        request: RequestT,
        phase_value: str,
        prompt: str | None,
        *,
        sub_agent_id: str | None = None,
    ) -> str | None:
        """Spawn (or re-prompt) one phase child through the one service.

        Spawning and prompting are one act: the child is created and its seed
        is delivered at its own anchor, and that delivery wakes the child's
        own turn.  Returns the child's id, or ``None`` when no service is
        bound or the prompt is empty.
        """
        if self._conversations is None or not prompt:
            return None
        attempt = self._phase_entry_ordinal(request, phase_value)
        spec, _context, _label = self._phase_spawn_spec(request, phase_value)
        child = self._conversations.spawn(
            Anchor(
                request.workspace_id,
                request.chat_id,
                sub_agent_id=self._workflow_node_id(request),
            ),
            spec,
            prompt,
            # A phase child never completes on text: its completion is the
            # tool this engine binds into its registry.
            SubAgentKind.CONVERSATION,
            actor_id=self._actor,
            completion_mode=CompletionMode.EXPLICIT_COMPLETION,
            phase_executor=True,
            sub_agent_id=sub_agent_id,
            # The seed identifies one entry into the phase: a loop-back
            # re-entry reuses the durable node, so only an entry-scoped key
            # delivers its fresh prompt instead of colliding with the previous
            # entry's delivery.
            prompt_key=f"phase-seed-{request.execution_id}-{phase_value}-{attempt}",
            prompt_data={"phase": phase_value, "attempt": attempt},
            attribution={
                "workflow_execution_id": request.execution_id,
                "phase_attempt_id": self._phase_attempt_id(request, phase_value),
            },
        )
        return child.conversation_id

    def _phase_entry_ordinal(self, request: RequestT, phase_value: str) -> int:
        """Return the durable ordinal of the current entry into ``phase_value``.

        A phase can be entered more than once: a verdict loop-back revisits
        hypothesis creation with the same durable node.  Counting the
        ``workflow.phase`` entry events the engine already emitted makes the
        ordinal stable across retries of one entry and distinct for every
        later entry.
        """
        _, chat = self._require_doctrine_chat()
        return sum(
            1
            for event in chat.replay_events(
                request.workspace_id, request.execution_id
            ).events
            if event.event_type is ExecutionEventType.WORKFLOW_PHASE
            and event.payload.get("phase") == phase_value
        )

    def _phase_attempt_id(self, request: RequestT, phase_value: str) -> str:
        return f"{request.execution_id}:{phase_value}:{self._phase_entry_ordinal(request, phase_value)}"

    def _completion_instruction(self, phase_value: str) -> str:
        """Describe the phase's completion obligation from its spec data.

        The instruction names the tool, the required contract fields, and the
        deterministic target, so every phase prompt (handoff override, declared
        seed, or doctrine task) tells its agent how to advance the workflow.
        Terminal phases have nothing to complete.
        """
        if phase_value in self.terminal_phases:
            return ""
        targets = sorted(self._spec.transitions.get(phase_value, frozenset()))
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
        self, request: RequestT, node_id: str, completion: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Validate one phase agent's completion and apply its transition.

        This is the workflow's whole reaction to the agent's tool call: the
        contract is validated, a rejection is returned to the agent through the
        tool result for repair, and an accepted completion applies the declared
        transition.  Acceptance is the end of the completed phase and the end of
        this tool call: the successor phase is entered through the detach seam
        (:meth:`_hand_off_phase`), so a deterministic successor — or the next
        agent's spawn — runs as its own unit instead of blocking the caller's
        turn.  A terminal phase closes the execution here.
        """
        if self._doctrine is None or self._chat is None:
            raise WorkflowEngineError("workflow has no doctrine or chat service bound")
        self._check_acceptance(request, completion=completion)
        state = self._reconstruct_state(request)
        phase = self._phase_value(state.phase)
        declared = self._spec.phase_specs[phase]
        if isinstance(declared, DeterministicPhase):
            # A deterministic phase has no node and no orchestrator (REQ-C1):
            # its contract is applied inside the engine's own cascade, so a
            # completion addressed to it never belongs to an agent turn.
            raise PhaseCompletionRejected(
                f"phase {phase!r} is deterministic and accepts no completion; "
                "the workflow advances on its own"
            )
        active = self._active_orchestrators.get(request.execution_id)
        self._check_phase_owner(request, node_id, phase)
        if active is None:
            active = self.ensure_orchestrator(request, state.phase)
        if active.sub_agent_id != node_id:
            raise PhaseCompletionRejected(
                f"completion does not belong to the current phase {phase!r}"
            )
        handed = self._apply_contract(
            request, state, phase, completion, node_id=node_id
        )
        target_phase = self._phase_value(handed.phase)
        next_context = HandoffContext(
            source=phase,
            target=target_phase,
            completion=completion,
            trigger_message_id=request.trigger_message_id,
        )
        next_prompt = self._phase_prompt(request, handed.phase, handed, next_context)
        self._hand_off_phase(request, handed, next_prompt, next_context)
        return {
            "executionId": request.execution_id,
            "phase": target_phase,
            "applied": True,
        }

    def _hand_off_phase(
        self,
        request: RequestT,
        state: StateT,
        prompt: str | None,
        context: HandoffContext,
    ) -> None:
        """Enter the successor phase, detached when the composition binds it.

        The transition is already committed; entering the successor is the next
        phase's own work.  With a detach seam the caller's turn is released
        immediately and the successor runs out of band — a deterministic phase
        then produces its phase result and its own transition, and a fault
        fails the execution durably instead of surfacing inside the completed
        phase's tool call.  Without a seam the entry runs inline, which keeps
        tests and embedders deterministic.
        """
        if self._detach is None:
            try:
                self._enter_phase(request, state.phase, state, prompt, context)
            except Exception as error:  # noqa: BLE001 - phase faults are terminal
                self.fail_execution(request, reason=str(error) or type(error).__name__)
                raise
            return

        def run() -> None:
            try:
                self._enter_phase(request, state.phase, state, prompt, context)
            except Exception as error:  # noqa: BLE001 - a detached fault is durable
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
        if phase in self.terminal_phases:
            self._finish_execution(
                request,
                WorkflowExecutionState.COMPLETED,
                ExecutionEventType.WORKFLOW_COMPLETED,
                payload={"state": "completed", "completion": dict(completion)},
            )
            return state
        target = self._resolve_target(phase, completion)
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
            final_value = self._phase_value(handed.phase)
            if final_value == "failed":
                self._finish_execution(
                    request,
                    WorkflowExecutionState.FAILED,
                    ExecutionEventType.WORKFLOW_FAILED,
                    payload={
                        "state": "failed",
                        "finalPhase": phase,
                        "completion": dict(completion),
                    },
                )
            else:
                self._finish_execution(
                    request,
                    WorkflowExecutionState.COMPLETED,
                    ExecutionEventType.WORKFLOW_COMPLETED,
                    payload={
                        "state": "completed",
                        "completion": dict(completion),
                        "finalPhase": phase,
                    },
                )
        return handed

    def _validate_contract(
        self, phase: str, completion: Mapping[str, object]
    ) -> str | None:
        """Return the repair reason a contract is rejected, or ``None``."""
        if not completion:
            return "completion contract must not be empty"
        validator = self._spec.validators.get(phase)
        if validator is not None:
            failure = validator(completion)
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
        allowed = sorted(self._spec.transitions.get(phase, frozenset()))
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
        prompt: str | None,
        context: HandoffContext,
    ) -> str | None:
        """Enter a phase: run deterministic code or spawn its agent.

        A deterministic phase is a function that returns a contract: the engine
        validates it exactly as an agent's completion and advances, cascading
        through consecutive deterministic phases.  A non-deterministic phase
        gets its agent spawned with ``prompt``.  A terminal phase has nothing
        to enter — the execution is finished, so no agent is spawned and no
        cascade continues.  Returns the seeded agent's id, or ``None`` for a
        deterministic or terminal entry.
        """
        current = state
        entry = phase
        for _ in range(len(self._spec.phases) + 1):
            if self._execution_cancelled_or_deleted(request):
                return None
            phase_value = self._phase_value(entry)
            if phase_value in self.terminal_phases:
                return None
            spec = self._spec.phase_specs[phase_value]
            if not isinstance(spec, DeterministicPhase):
                active = self.ensure_orchestrator(request, entry, prompt=prompt)
                return active.sub_agent_id
            contract = spec.contract(self, request, current, context)
            if self._execution_cancelled_or_deleted(request):
                return None
            if contract is None:
                raise WorkflowEngineError(
                    f"deterministic phase {phase_value!r} returned no contract"
                )
            if not isinstance(contract, Mapping):
                raise WorkflowEngineError(
                    f"deterministic phase {phase_value!r} must return a contract"
                )
            try:
                current = self._apply_contract(request, current, phase_value, contract)
            except PhaseCompletionRejected as error:
                raise WorkflowEngineError(
                    f"deterministic phase {phase_value!r} produced an invalid "
                    f"contract: {error}"
                ) from error
            context = HandoffContext(
                source=phase_value,
                target=self._phase_value(current.phase),
                completion=contract,
                trigger_message_id=request.trigger_message_id,
            )
            prompt = self._phase_prompt(request, current.phase, current, context)
            entry = current.phase
        raise WorkflowEngineError("deterministic phase cascade did not terminate")

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

    def _context(self, request: RequestT) -> WorkflowContext:
        """Return the trusted engine context for one execution turn."""
        doctrine, _ = self._require_doctrine_chat()
        return doctrine.context_of(self, request)

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
        return execution.state in {
            WorkflowExecutionState.STOPPED,
            WorkflowExecutionState.CANCELLED,
        }

    # -- transition mechanics -----------------------------------------------

    def _reconstruct_state(self, request: RequestT) -> StateT:
        """Rebuild the run state from the durable execution and artifacts."""
        doctrine, _ = self._require_doctrine_chat()
        return doctrine.reconstruct(self, request)

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
        at = self._clock()
        self._check_acceptance(request, completion=completion)
        if node_id is not None:
            self._check_phase_owner(request, node_id, self._phase_value(state.phase))
        assert self._execution_store is not None
        observed = self._execution_store.get(request.execution_id)
        if len(observed.transitions) != len(state.transitions):
            raise PhaseCompletionRejected("phase state changed before acceptance")
        transition = StoreTransition(
            sequence=len(state.transitions) + 1,
            from_phase=self._phase_value(state.phase),
            to_phase=self._phase_value(target),
            occurred_at=at,
            intent_epoch=request.intent_epoch,
            reason=reason,
            completion=dict(completion or {}),
        )
        doctrine, _ = self._require_doctrine_chat()
        advanced = doctrine.reduce_transition(state, transition, self._spec.piping)
        # Store-first ordering (REQ-B2): the transition — with its completion
        # contract — is committed to the execution store before any
        # projection, so a crash in between leaves the state correct.
        self._check_acceptance(request, completion=completion)
        self._commit_transition(
            request, transition, expected_revision=observed.revision, node_id=node_id
        )
        self._emit_phase(request, target, {"reason": reason})
        self._on_phase_entered(request, advanced, completion=completion)
        return advanced

    def _commit_transition(
        self,
        request: RequestT,
        transition: StoreTransition,
        *,
        expected_revision: int,
        node_id: str | None = None,
    ) -> None:
        """Commit, project the phase, and close its executor under one fence."""
        store = self._execution_store
        assert store is not None
        _, chat = self._require_doctrine_chat()

        def commit() -> ExecutionDocument:
            return store.append_transition(
                request.execution_id, transition, expected_revision=expected_revision
            )

        with chat.execution_guard(request.workspace_id, request.execution_id):
            try:
                chat.accept_transition(
                    request.workspace_id,
                    request.execution_id,
                    expected_intent_epoch=request.intent_epoch,
                    expected_phase=transition.from_phase,
                    expected_node_id=node_id,
                    expected_attempt_id=(
                        None
                        if node_id is None
                        else self._phase_attempt_id(request, transition.from_phase)
                    ),
                    target_phase=transition.to_phase,
                    commit=commit,
                )
            except InvalidLifecycleTransition as error:
                raise PhaseCompletionRejected(str(error)) from error
            except ExecutionStoreConflict as error:
                raise PhaseCompletionRejected(
                    "phase state changed before commit"
                ) from error
            # Cancellation uses this same guard. Close the accepted executor
            # before releasing it; events and successor entry stay outside.
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

    def _post_phase_handoff(
        self,
        request: RequestT,
        body: str,
        *,
        kind: str,
    ) -> None:
        """Deliver a system handoff into the currently active phase thread.

        The handoff is one delivery through the one service (REQ-A2): the
        message is persisted at the phase agent's anchor and the anchor's wake
        is routed by the service's own rule.  The delivery key is derived from
        the handoff's content, so a re-delivered handoff resolves to the
        durable message instead of duplicating the turn.
        """
        self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        if active is None:
            raise WorkflowEngineError("phase handoff requires an active orchestrator")
        phase = self._phase_value(active.phase)
        digest = hashlib.sha256(
            f"{request.execution_id}|{kind}|{phase}|{body}".encode()
        ).hexdigest()
        assert self._conversations is not None
        self._conversations.deliver(
            Anchor(
                request.workspace_id, request.chat_id, sub_agent_id=active.sub_agent_id
            ),
            None,
            body=body,
            role=MessageRole.SYSTEM,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "executionId": request.execution_id,
                "handoffKind": kind,
                "phase": phase,
                "subAgentId": active.sub_agent_id,
            },
            execution_id=request.execution_id,
            actor_id=self._actor,
            idempotency_key=f"handoff-{digest[:32]}",
        )

    # -- orchestrator lifecycle --------------------------------------------

    def ensure_orchestrator(
        self, request: RequestT, phase: object, *, prompt: str | None = None
    ) -> ActiveSubAgent:
        """Ensure one phase's agent exists and has been given its prompt.

        When the agent is already live, the prompt is delivered to it
        idempotently; when it is found in the durable record, it is reopened
        with the prompt; otherwise it is spawned with the prompt.  In all three
        cases the prompt delivery is what wakes the agent's own turn.
        """
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            if prompt is not None:
                self._spawn_phase_child(
                    request,
                    self._phase_value(phase),
                    prompt,
                    sub_agent_id=active.sub_agent_id,
                )
            return active
        doctrine, chat = self._require_doctrine_chat()
        phase_value = self._phase_value(phase)
        node_id = self._workflow_node_id(request)
        for event in reversed(
            chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != phase_value
                or payload.get("parentSubAgentId") != node_id
            ):
                continue
            family, default_role = doctrine.orchestrators[phase_value]
            tools = _phase_mcp_tools(doctrine, phase_value, family)
            role = str(payload.get("role") or default_role)
            sub_agent_id = str(payload["subAgentId"])
            self._reopen_phase_node(
                request,
                sub_agent_id,
                phase=phase,
                role=role,
                prompt=prompt,
            )
            active = ActiveSubAgent(
                sub_agent_id=sub_agent_id,
                phase=phase,
                role=role,
                context=SubAgentContextSpec(
                    knowledge_cutoff=None,
                    mcp_enabled=bool(tools),
                    mcp_tools=tools,
                ),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, phase, prompt=prompt)
        return self._active_orchestrators[request.execution_id]

    def _spawn_orchestrator(
        self, request: RequestT, phase: object, *, prompt: str | None = None
    ) -> None:
        doctrine, _ = self._require_doctrine_chat()
        phase_value = self._phase_value(phase)
        spec, context, label = self._phase_spawn_spec(request, phase_value)
        conversational = phase_value in doctrine.conversational_phases
        sub_agent_id = self._spawn_phase_child(request, phase_value, prompt)
        if sub_agent_id is None:
            sub_agent_id = self._id_factory("subagent")
            if self._subagent_lifecycle is not None:
                record = self._subagent_lifecycle.spawn(
                    request.workspace_id,
                    request.chat_id,
                    spec=spec,
                    actor_id=self._actor,
                    completion_mode=CompletionMode.EXPLICIT_COMPLETION,
                    phase_executor=True,
                    parent_sub_agent_id=self._workflow_node_id(request),
                    workflow_execution_id=request.execution_id,
                    phase_attempt_id=self._phase_attempt_id(request, phase_value),
                )
                sub_agent_id = record.sub_agent_id
        self.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_SPAWNED,
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=None,
            phase=phase,
            role=label,
            context=context,
            accepts_input=conversational,
        )
        self._active_orchestrators[request.execution_id] = ActiveSubAgent(
            sub_agent_id=sub_agent_id,
            phase=phase,
            role=label,
            context=context,
        )

    def _activate_orchestrator(
        self, request: RequestT, phase: object
    ) -> ActiveSubAgent:
        """Reopen one phase's durable conversation node, creating it once."""
        doctrine, chat = self._require_doctrine_chat()
        phase_value = self._phase_value(phase)
        node_id = self._workflow_node_id(request)
        for event in reversed(
            chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != phase_value
                or payload.get("parentSubAgentId") != node_id
            ):
                continue
            family, default_role = doctrine.orchestrators[phase_value]
            tools = _phase_mcp_tools(doctrine, phase_value, family)
            role = str(payload.get("role") or default_role)
            sub_agent_id = str(payload["subAgentId"])
            self._reopen_phase_node(
                request,
                sub_agent_id,
                phase=phase,
                role=role,
            )
            active = ActiveSubAgent(
                sub_agent_id=sub_agent_id,
                phase=phase,
                role=role,
                context=SubAgentContextSpec(
                    knowledge_cutoff=None,
                    mcp_enabled=bool(tools),
                    mcp_tools=tools,
                ),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, phase)
        return self._active_orchestrators[request.execution_id]

    def _reopen_phase_node(
        self,
        request: RequestT,
        sub_agent_id: str,
        *,
        phase: object,
        role: str,
        prompt: str | None = None,
    ) -> None:
        """Reopen a reused phase node and give it its new prompt.

        Re-entering a phase reuses the node's durable conversation, but the
        node's lifecycle record was closed when the phase last exited.  The
        record is reset through the same spawn-with-prompt command — the depth,
        children, and budget guards re-run, while the identity, parent, and
        transcript are preserved — and the prompt delivery wakes the reused
        agent's own turn.
        """
        if self._subagent_lifecycle is None:
            return
        phase_value = self._phase_value(phase)
        record = self._subagent_lifecycle.get(request.workspace_id, sub_agent_id)
        if record is None or record.status not in {
            SubAgentStatus.COMPLETED,
            SubAgentStatus.FAILED,
            SubAgentStatus.CANCELLED,
        }:
            return
        if prompt and self._conversations is not None:
            self._spawn_phase_child(
                request, phase_value, prompt, sub_agent_id=sub_agent_id
            )
            return
        spec, _context, _label = self._phase_spawn_spec(request, phase_value)
        spec = replace(spec, role=role)
        self._subagent_lifecycle.spawn(
            request.workspace_id,
            request.chat_id,
            spec=spec,
            actor_id=self._actor,
            completion_mode=CompletionMode.EXPLICIT_COMPLETION,
            phase_executor=True,
            parent_sub_agent_id=self._workflow_node_id(request),
            workflow_execution_id=request.execution_id,
            sub_agent_id=sub_agent_id,
            phase_attempt_id=self._phase_attempt_id(request, phase_value),
        )

    def finish_orchestrator(
        self,
        request: RequestT,
        *,
        status: str,
        detail: str | None = None,
        summary: str | None = None,
        artifact: Mapping[str, object] | None = None,
        result: Mapping[str, object] | None = None,
    ) -> None:
        """Close the execution's open orchestrator with its final projection."""
        active = self._active_orchestrators.pop(request.execution_id, None)
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
                },
            )
        extra: dict[str, object] = {"frozen": True, "status": status}
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
        if self._subagent_lifecycle is not None:
            if status == STATUS_COMPLETED:
                record = self._subagent_lifecycle.get(
                    request.workspace_id, active.sub_agent_id
                )
                if record is not None and record.status is SubAgentStatus.COMPLETED:
                    return
                completed_result: Mapping[str, object] | None = (
                    result
                    if result is not None
                    else (None if summary is None else {"summary": summary})
                )
                self._subagent_lifecycle.complete(
                    request.workspace_id,
                    active.sub_agent_id,
                    result=completed_result,
                )
            else:
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
    ) -> None:
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        report = doctrine.report(self, state, active)
        if report is not None:
            assert active is not None
            summary = doctrine.summarize(state, active.phase)
            self.finish_orchestrator(
                request,
                status=STATUS_COMPLETED,
                summary=summary,
                artifact=report,
                result=completion,
            )
        else:
            summary = (
                None if active is None else doctrine.summarize(state, active.phase)
            )
            self.finish_orchestrator(
                request, status=STATUS_COMPLETED, summary=summary, result=completion
            )
        phase_value = self._phase_value(state.phase)
        if phase_value in doctrine.active_phases:
            self._activate_orchestrator(request, state.phase)
            if phase_value in doctrine.tasks:
                self._emit_orchestrator_progress(request)

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

    def _emit_orchestrator_progress(self, request: RequestT) -> None:
        """Record the active phase's safe activity label."""
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
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
            extra={"kind": "activity", "text": activity},
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

    @staticmethod
    def _replace_state(state: StateT, **changes: object) -> StateT:
        return cast(StateT, replace(cast(Any, state), **changes))
