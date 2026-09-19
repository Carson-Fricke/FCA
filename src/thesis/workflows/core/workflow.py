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

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from thesis.agent.context import PromptContext, assemble_context
from thesis.agent.router import InvocationResult, Role
from thesis.agent.runtime.control import PhaseCompletionRejected
from thesis.agent.runtime.datum import Datum, DatumType
from thesis.agent.runtime.delivery import (
    Anchor,
    DeliveryReceipt,
    DeliveryService,
    NodeInputService,
    TurnScheduler,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.spawn import SubAgentSpawner
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import (
    WORKFLOW_NODE_ROLE,
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
from thesis.workflows.core.delivery import NodeTurnDelivery, WorkflowNodeManager
from thesis.workflows.core.events import emit_phase, finish_execution
from thesis.workflows.core.gate import evaluate_gate
from thesis.workflows.core.machine import TransitionMachine, TransitionTable
from thesis.workflows.core.phase import PhaseExecutor, PhaseKind
from thesis.workflows.core.spec import (
    HandoffContext,
    TransitionRequest,
    WorkflowContext,
    WorkflowSpec,
)
from thesis.workflows.core.state import WorkflowState

__all__ = [
    "ActiveSubAgent",
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


@dataclass(frozen=True, slots=True)
class WorkflowPorts:
    """The composed engine ports that make a workflow runnable."""

    chat: ChatService
    clock: Callable[[], datetime]
    id_factory: Callable[[str], str]
    subagent_lifecycle: SubAgentLifecycle | None = None
    turn_scheduler: TurnScheduler | None = None


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
    operator_role: object
    json_instruction: str
    max_compose_repairs: int
    mcp_tools: Mapping[str, tuple[str, ...]]
    request_of: Callable[[WorkflowExecution, str], RequestT]
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
    validate_launch: Callable[[str, str | None, str | None], None]
    transition_error: Callable[[object, object], Exception]
    record_of: Callable[[int, object, object, datetime, int, str], object]
    rebuild: Callable[[StateT, object, tuple[object, ...]], StateT]
    author_question: Callable[[object, Callable[[str], PromptContext], str], str]
    invoke: Callable[
        [object, Role | str, PromptContext, Mapping[str, object]], InvocationResult
    ]


class Workflow[RequestT: WorkflowRequest, StateT: WorkflowState, OutcomeT]:
    """The general, spec-driven workflow instance.

    ``run`` drives one durable turn through the turn skeleton; ``advance_turn``
    dispatches the durable phase to its spec executor; ``execute`` launches a
    new execution.  The object is re-entrant: the derived in-process state is
    retained per execution until a terminal phase, and it is rebuilt through
    the doctrine's reconstruction hook after a restart.
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
        self._subagent_lifecycle = None if ports is None else ports.subagent_lifecycle
        self._turn_scheduler = None if ports is None else ports.turn_scheduler
        self._active_orchestrators: dict[str, ActiveSubAgent] = {}
        self._delivery: DeliveryService | None = None
        self._node_inputs: NodeInputService | None = None
        self._nodes: WorkflowNodeManager | None = None
        self._node_turns: NodeTurnDelivery | None = None
        self._transitions: TransitionMachine[object, object, StateT] | None = None
        if ports is not None:
            if doctrine is None:
                raise ValueError("a runnable workflow requires its doctrine")
            self._delivery = DeliveryService(
                ports.chat, schedule=ports.turn_scheduler, clock=ports.clock
            )
            self._node_inputs = NodeInputService(chat=ports.chat, clock=ports.clock)
            self._nodes = WorkflowNodeManager(
                ports.chat,
                ports.subagent_lifecycle,
                role=WORKFLOW_NODE_ROLE,
                actor_id=doctrine.actor,
                initial_phase=doctrine.initial_phase,
                clock=ports.clock,
            )
            self._node_turns = NodeTurnDelivery(
                chat=ports.chat,
                delivery=self._delivery,
                node_inputs=self._node_inputs,
                actor_id=doctrine.actor,
            )
            self._transitions = TransitionMachine(
                TransitionTable(spec.transitions, error=doctrine.transition_error),
                record_of=doctrine.record_of,
                rebuild=doctrine.rebuild,
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
    def executors(self) -> Mapping[str, PhaseExecutor[Any, RequestT, StateT]]:
        """Return the executor for every declared phase, by name."""
        return self._spec.executors

    @property
    def phase_kinds(self) -> Mapping[str, object]:
        """Return the declared kind for every phase, by name."""
        return self._spec.phase_kinds

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
    def subagent_lifecycle(self) -> SubAgentLifecycle | None:
        """Return the composed sub-agent lifecycle, when bound."""
        return self._subagent_lifecycle

    @property
    def turn_scheduler(self) -> TurnScheduler | None:
        """Return the configured turn scheduler, when bound."""
        return self._turn_scheduler

    @property
    def active_orchestrators(self) -> dict[str, ActiveSubAgent]:
        """Return the per-execution open orchestrators."""
        return self._active_orchestrators

    # -- launch and observation -------------------------------------------

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
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
        doctrine.validate_launch(ideas, trigger_message_id, anchor_sub_agent_id)
        execution = chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=doctrine.definition,
            actor_id=actor_id,
            message_id=trigger_message_id,
            investigation_id=investigation_id,
        )
        request = doctrine.request_of(execution, ideas)
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
    ) -> Mapping[str, object]:
        """Launch one execution at ``context``'s anchor and return its handle."""
        request = self.launch(
            context.workspace_id,
            context.chat_id,
            actor_id=context.actor_id,
            ideas=ideas,
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
        return self._enter_phase(request, state.phase, state, prompt)

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
        seed = self._spec.seeds.get(phase_value)
        task = override or (
            seed(self, request, state, context)
            if seed is not None
            else self._doctrine.tasks.get(phase_value)
        )
        if not task:
            return None
        instruction = self._completion_instruction(phase_value)
        return f"{task}\n\n{instruction}" if instruction else task

    def _spawner(self) -> SubAgentSpawner | None:
        """Return the one spawn-with-prompt primitive, when ports are bound."""
        if self._subagent_lifecycle is None or self._delivery is None:
            return None
        return SubAgentSpawner(
            lifecycle=self._subagent_lifecycle,
            delivery=self._delivery,
            clock=self._clock,
        )

    def _deliver_phase_prompt(
        self,
        request: RequestT,
        phase_value: str,
        sub_agent_id: str,
        prompt: str,
    ) -> None:
        """Deliver a phase prompt to an already-live agent, idempotently."""
        spawner = self._spawner()
        if spawner is None:
            return
        record = (
            None
            if self._subagent_lifecycle is None
            else self._subagent_lifecycle.get(request.workspace_id, sub_agent_id)
        )
        if record is None:
            return
        spawner.deliver_prompt(
            request.workspace_id,
            request.chat_id,
            record,
            prompt,
            key=f"phase-seed-{request.execution_id}-{phase_value}",
            data={"phase": phase_value},
        )

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
            lines.append(
                "Name the target in the completion: " + ", ".join(targets) + "."
            )
        return " ".join(lines)

    def apply_phase_completion(
        self, request: RequestT, node_id: str, completion: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Validate one phase agent's completion and apply its transition.

        This is the workflow's whole reaction to the agent's tool call: the
        contract is validated, a rejection is returned to the agent through the
        tool result for repair, and an accepted completion applies the declared
        transition and spawns, seeds, and schedules the next phase agent — or
        closes the execution when the phase is terminal.
        """
        if self._doctrine is None or self._chat is None:
            raise WorkflowEngineError("workflow has no doctrine or chat service bound")
        state = self._reconstruct_state(request)
        phase = self._phase_value(state.phase)
        active = self.ensure_orchestrator(request, state.phase)
        if active.sub_agent_id != node_id:
            raise PhaseCompletionRejected(
                f"completion does not belong to the current phase {phase!r}"
            )
        handed = self._apply_contract(request, state, phase, completion)
        target_phase = self._phase_value(handed.phase)
        next_prompt = self._phase_prompt(
            request,
            handed.phase,
            handed,
            HandoffContext(
                source=phase,
                target=target_phase,
                completion=completion,
                trigger_message_id=request.trigger_message_id,
            ),
        )
        self._enter_phase(request, handed.phase, handed, next_prompt)
        return {
            "executionId": request.execution_id,
            "phase": target_phase,
            "applied": True,
        }

    def _apply_contract(
        self,
        request: RequestT,
        state: StateT,
        phase: str,
        completion: Mapping[str, object],
    ) -> StateT:
        """Validate one contract and apply its declared transition and pipe.

        A rejection is raised as :class:`PhaseCompletionRejected`; an accepted
        contract advances the state through the declared transition, applies
        the transition's pipe, and returns the handed-off state.  A terminal
        phase closes the execution instead.
        """
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
            request, state, target, f"phase completion accepted for {phase!r}"
        )
        pipe = self._spec.piping.get((phase, target))
        return advanced if pipe is None else pipe(advanced)

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
    ) -> str | None:
        """Enter a phase: run deterministic code or spawn its agent.

        A deterministic phase is a function that returns a contract: the engine
        validates it exactly as an agent's completion and advances, cascading
        through consecutive deterministic phases.  A non-deterministic phase
        gets its agent spawned with ``prompt``.  Returns the seeded agent's id,
        or ``None`` for a deterministic entry.
        """
        current = state
        entry = phase
        for _ in range(len(self._spec.phases) + 1):
            phase_value = self._phase_value(entry)
            spec = self._spec.phase_specs.get(phase_value)
            if spec is None or spec.kind is not PhaseKind.DETERMINISTIC:
                active = self.ensure_orchestrator(request, entry, prompt=prompt)
                return active.sub_agent_id
            if spec.executor is None:
                raise WorkflowEngineError(
                    f"deterministic phase {phase_value!r} declares no executor"
                )
            contract = spec.executor(self, request, current)
            if contract is None:
                return None
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
            prompt = self._phase_prompt(
                request,
                current.phase,
                current,
                HandoffContext(
                    source=phase_value,
                    target=self._phase_value(current.phase),
                    completion=contract,
                    trigger_message_id=request.trigger_message_id,
                ),
            )
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
        if self._delivery is None:
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
        text = f"Hypothesis development {request.execution_id} failed: {reason}."
        self._delivery.deliver(
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

    def _transition_checked(
        self,
        request: RequestT,
        state: StateT,
        target: object,
        reason: str,
        check: Callable[[], str | None],
    ) -> StateT:
        """Validate phase exit criteria, requesting operator input if blocked."""
        outcome = evaluate_gate(check)
        if outcome.reason is not None:
            return self._request_operator_input(
                request,
                state,
                purpose=f"validate the step before {self._phase_value(target)}",
                detail=outcome.reason,
            )
        return self._transition(request, state, target, reason)

    def _transition(
        self,
        request: RequestT,
        state: StateT,
        target: object,
        reason: str,
    ) -> StateT:
        advanced = self._advance(state, target, reason)
        self._emit_phase(request, target, {"reason": reason})
        _, chat = self._require_doctrine_chat()
        try:
            chat.set_execution_phase(
                request.workspace_id, request.execution_id, self._phase_value(target)
            )
        except Exception:  # noqa: BLE001 - phase is best-effort projection state
            pass
        self._on_phase_entered(request, advanced)
        return advanced

    def _advance(self, state: StateT, target: object, reason: str) -> StateT:
        assert self._transitions is not None
        request = TransitionRequest(
            target_phase=target, intent_epoch=state.intent_epoch
        )
        return self._transitions.apply(state, request, reason=reason, at=self._clock())

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

    # -- operator interaction ----------------------------------------------

    def _request_operator_input(
        self,
        request: RequestT,
        state: StateT,
        *,
        purpose: str,
        detail: str,
        active: ActiveSubAgent | None = None,
    ) -> StateT:
        """Deliver an LLM-authored operator request as an ordinary prompt turn.

        Operator-facing text is never a canned workflow string. The model is
        asked to author the request from the concrete reason the workflow needs
        direction; the only fallback is the raw reason itself, used solely when
        the model is unavailable so the workflow still surfaces the concrete
        failure rather than inventing conversational text.

        The request is one node turn on the asking phase node and the execution
        is never paused or fenced.  The node enters ``needs_input`` through the
        unified node-input path so the operator's answer is accepted for that
        node and starts exactly one node turn.
        """
        resolved = active or self._active_orchestrators.get(request.execution_id)
        question = self._operator_question(
            request,
            state,
            active=resolved,
            purpose=purpose,
            detail=detail,
        )
        self._deliver_operator_request(
            request,
            resolved,
            question,
            question=question,
        )
        return self._replace_state(state, detail=question)

    def _operator_question(
        self,
        request: RequestT,
        state: StateT,
        *,
        active: ActiveSubAgent | None,
        purpose: str,
        detail: str,
    ) -> str:
        """Ask the model to author one operator-facing request.

        Returns the raw reason when the model is unavailable, so the operator
        still sees the concrete failure instead of a fabricated prompt.
        """
        del state
        doctrine, _ = self._require_doctrine_chat()
        if active is None:
            return detail
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-operator-question",
        )
        task = "\n".join(
            [
                "You are the workflow's operator-facing voice. The workflow "
                "needs the operator's direction to continue.",
                "Author the operator request, not a summary.",
                f"Purpose: {purpose}",
                f"Concrete reason the workflow needs direction: {detail}",
                "",
                "Write exactly one concise, natural request addressed to the "
                "operator. Include the concrete reason, say what you need from "
                "them, and do not mention internal step names, schemas, or that "
                "you are a model. If you reference any mathematics, render it as "
                "LaTeX for KaTeX: inline math as \\(...\\) and display equations "
                "as \\[...\\] on their own line, never with a bare $. Reply with "
                "the request text only: no quotes, no JSON, no code fences.",
            ]
        )
        try:
            text = doctrine.author_question(self._services, prompt_for, task)
        except Exception:  # noqa: BLE001 - a failed author falls back to the reason
            return detail
        return text.strip() or detail

    def _deliver_node_turn(
        self,
        request: RequestT,
        active: ActiveSubAgent,
        body: str,
        *,
        question: str | None = None,
        **fields: object,
    ) -> DeliveryReceipt:
        """Deliver one node-authored operator question at the phase node's anchor.

        The operator-facing text is persisted as the phase node's assistant turn
        at the node's anchor, but no turn is scheduled: the question has no
        answer yet, so a delivery-time schedule could only produce the
        workflow's unanswered-request no-op.  The answer accepted through
        :meth:`~thesis.agent.runtime.delivery.NodeInputService.accept` is the
        single scheduling point, and its scheduled turn re-enters this workflow
        exactly once.  A re-delivered question resolves to the durable message
        instead of duplicating the turn.
        """
        assert self._node_turns is not None
        return self._node_turns.deliver_question(
            self._context(request),
            sub_agent_id=active.sub_agent_id,
            phase=self._phase_value(active.phase),
            body=body,
            question=question,
            fields=fields,
        )

    def _deliver_operator_request(
        self,
        request: RequestT,
        active: ActiveSubAgent | None,
        body: str,
        *,
        question: str | None = None,
        **fields: object,
    ) -> None:
        """Deliver one operator request as a node turn and open the node.

        The phase node's durable record enters ``needs_input`` through the
        unified node-input path so the answer accepted for that node resumes the
        same record under the node's operator policy; the answer's accept
        schedules the one turn that re-enters the workflow, and the question
        delivery itself schedules nothing.
        """
        assert self._node_turns is not None
        if active is None:
            return
        self._node_turns.deliver_request(
            self._context(request),
            sub_agent_id=active.sub_agent_id,
            phase=self._phase_value(active.phase),
            body=body,
            question=question,
            fields=fields,
            open_node_input=self._subagent_lifecycle is not None,
        )

    def _prompt_builder(
        self, *, workspace_scope: str, role: str, context_id: str
    ) -> Callable[[str], PromptContext]:
        """Return a function that renders one task into a scoped prompt."""

        def build(task: str) -> PromptContext:
            return assemble_context(
                workspace_scope=workspace_scope,
                task=task,
                role=role,
                context_id=context_id,
            )

        return build

    def _compose_with_repair(
        self,
        request: RequestT,
        role: Role | str,
        *,
        base_task: str,
        prompt_for: Callable[[str], PromptContext],
        compose: Callable[[InvocationResult], Any],
        response_schema: str,
        emit_activity: bool = True,
        emit_output: bool = True,
    ) -> tuple[Any, InvocationResult]:
        """Invoke the model and compose, repairing malformed output in place.

        A compose failure never ends the run by itself: the model is re-invoked
        with the validation error and the required schema, up to
        ``max_compose_repairs`` times.  When repairs are exhausted the final
        error is re-raised.
        """
        doctrine, _ = self._require_doctrine_chat()
        if emit_activity:
            self._emit_orchestrator_progress(request, base_task)
        last_error: Exception | None = None
        invocation: InvocationResult | None = None
        for _ in range(doctrine.max_compose_repairs + 1):
            task = f"{base_task}\n\n{doctrine.json_instruction}"
            if last_error is not None:
                task = (
                    f"{base_task}\n\nYour previous reply could not be used: "
                    f"{last_error}. Reply again with a single JSON object that "
                    f"matches this schema: {response_schema}\n\n"
                    f"{doctrine.json_instruction}"
                )
            invocation = doctrine.invoke(
                self._services,
                role,
                prompt_for(task),
                {"type": "json_object"},
            )
            try:
                composed = compose(invocation)
            except Exception as error:  # noqa: BLE001 - compose output is repairable
                last_error = error
                continue
            if emit_activity and emit_output:
                self._emit_orchestrator_output(request, invocation)
            return composed, invocation
        assert invocation is not None and last_error is not None
        raise last_error

    def _post_phase_handoff(
        self,
        request: RequestT,
        body: str,
        *,
        kind: str,
    ) -> None:
        """Persist a system handoff into the currently active phase thread."""
        _, chat = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        if active is None:
            raise WorkflowEngineError("phase handoff requires an active orchestrator")
        chat.create_message(
            request.workspace_id,
            request.chat_id,
            actor_id=self._actor,
            body=body,
            role=MessageRole.SYSTEM,
            execution_id=request.execution_id,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "executionId": request.execution_id,
                "handoffKind": kind,
                "phase": self._phase_value(active.phase),
                "subAgentId": active.sub_agent_id,
            },
        )
        assert self._delivery is not None
        scheduler = self._delivery.scheduler
        if scheduler is not None:
            scheduler(request.workspace_id, request.chat_id, active.sub_agent_id)

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
                self._deliver_phase_prompt(
                    request,
                    self._phase_value(phase),
                    active.sub_agent_id,
                    prompt,
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
            tools = doctrine.mcp_tools.get(family, ())
            role = str(payload.get("role") or default_role)
            sub_agent_id = str(payload["subAgentId"])
            self._reopen_phase_node(
                request,
                sub_agent_id,
                phase=phase,
                role=role,
                tools=tools,
                prompt=prompt,
            )
            active = ActiveSubAgent(
                sub_agent_id=sub_agent_id,
                phase=phase,
                role=role,
                context=SubAgentContextSpec(),
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
        family, label = doctrine.orchestrators[phase_value]
        tools = doctrine.mcp_tools.get(family, ())
        context = SubAgentContextSpec(
            knowledge_cutoff=None, mcp_enabled=bool(tools), mcp_tools=tools
        )
        conversational = phase_value in doctrine.conversational_phases
        node_id = self._workflow_node_id(request)
        spec = SubAgentSpec(
            role=label,
            kind=(
                SubAgentKind.CONVERSATION if conversational else SubAgentKind.ONE_SHOT
            ),
            phase=phase_value,
            allowed_tools=tuple(tools),
        )
        spawner = self._spawner()
        sub_agent_id = self._id_factory("subagent")
        if spawner is not None and prompt:
            # Spawning and prompting are one act: the created agent's first
            # prompt is delivered at its own anchor, which wakes its turn.
            spawned = spawner.spawn(
                request.workspace_id,
                request.chat_id,
                spec=spec,
                actor_id=self._actor,
                prompt=prompt,
                parent_sub_agent_id=node_id,
                workflow_execution_id=request.execution_id,
                prompt_key=f"phase-seed-{request.execution_id}-{phase_value}",
                prompt_data={"phase": phase_value},
            )
            sub_agent_id = spawned.record.sub_agent_id
        elif self._subagent_lifecycle is not None:
            record = self._subagent_lifecycle.spawn(
                request.workspace_id,
                request.chat_id,
                spec=spec,
                actor_id=self._actor,
                parent_sub_agent_id=node_id,
                workflow_execution_id=request.execution_id,
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
            tools = doctrine.mcp_tools.get(family, ())
            role = str(payload.get("role") or default_role)
            sub_agent_id = str(payload["subAgentId"])
            self._reopen_phase_node(
                request,
                sub_agent_id,
                phase=phase,
                role=role,
                tools=tools,
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
        tools: tuple[str, ...],
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
        doctrine, _ = self._require_doctrine_chat()
        phase_value = self._phase_value(phase)
        record = self._subagent_lifecycle.get(request.workspace_id, sub_agent_id)
        if record is None or record.status not in {
            SubAgentStatus.COMPLETED,
            SubAgentStatus.FAILED,
            SubAgentStatus.CANCELLED,
        }:
            return
        spec = SubAgentSpec(
            role=role,
            kind=(
                SubAgentKind.CONVERSATION
                if phase_value in doctrine.conversational_phases
                else SubAgentKind.ONE_SHOT
            ),
            phase=phase_value,
            allowed_tools=tuple(tools),
        )
        spawner = self._spawner()
        if spawner is not None and prompt:
            spawner.spawn(
                request.workspace_id,
                request.chat_id,
                spec=spec,
                actor_id=self._actor,
                prompt=prompt,
                parent_sub_agent_id=self._workflow_node_id(request),
                workflow_execution_id=request.execution_id,
                sub_agent_id=sub_agent_id,
                prompt_key=f"phase-seed-{request.execution_id}-{phase_value}",
                prompt_data={"phase": phase_value},
            )
            return
        self._subagent_lifecycle.spawn(
            request.workspace_id,
            request.chat_id,
            spec=spec,
            actor_id=self._actor,
            parent_sub_agent_id=self._workflow_node_id(request),
            workflow_execution_id=request.execution_id,
            sub_agent_id=sub_agent_id,
        )

    def finish_orchestrator(
        self,
        request: RequestT,
        *,
        status: str,
        detail: str | None = None,
        summary: str | None = None,
        artifact: Mapping[str, object] | None = None,
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
                self._subagent_lifecycle.complete(
                    request.workspace_id,
                    active.sub_agent_id,
                    result=(None if summary is None else {"summary": summary}),
                )
            else:
                self._subagent_lifecycle.fail(
                    request.workspace_id,
                    active.sub_agent_id,
                    result=None if detail is None else {"failure": detail},
                )

    def _on_phase_entered(self, request: RequestT, state: StateT) -> None:
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
            )
        else:
            summary = (
                None if active is None else doctrine.summarize(state, active.phase)
            )
            self.finish_orchestrator(request, status=STATUS_COMPLETED, summary=summary)
        phase_value = self._phase_value(state.phase)
        if phase_value in doctrine.active_phases:
            self._activate_orchestrator(request, state.phase)
            task = doctrine.tasks.get(phase_value)
            if task is not None:
                self._emit_orchestrator_progress(request, task)

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

    def emit_orchestrator_text(self, request: RequestT, text: str) -> None:
        """Record the active orchestrator's authored text as an artifact."""
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
            return
        self.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=(
                self._phase_value(active.phase) in doctrine.conversational_phases
            ),
            extra={
                "artifact": {"content": text.strip(), "type": "text"},
                "kind": "artifact",
            },
        )

    def _emit_orchestrator_progress(self, request: RequestT, text: str) -> None:
        """Record a safe activity label, never the raw model instruction."""
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
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

    def _emit_orchestrator_output(
        self, request: RequestT, invocation: InvocationResult
    ) -> None:
        """Record the active phase orchestrator's reply as a visible artifact."""
        doctrine, _ = self._require_doctrine_chat()
        active = self._active_orchestrators.get(request.execution_id)
        output = invocation.output
        if active is None or not output:
            return
        self.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=(
                self._phase_value(active.phase) in doctrine.conversational_phases
            ),
            extra={"artifact": {"content": output, "type": "text"}, "kind": "artifact"},
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
