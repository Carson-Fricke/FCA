"""Fixed graph behavior through real completion, conversations and durable history."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from thesis_runtime.contracts.records import WorkflowDefinition
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.subagents.control import PhaseCompletionRejected
from thesis_runtime.workflows.builder import WorkflowBuilder
from thesis_runtime.workflows.execution_store import (
    ExecutionStoreError,
    InMemoryExecutionStore,
)
from thesis_runtime.workflows.phase import (
    DeterministicPhase,
    OneShotPhase,
    TerminalPhase,
    WorkflowSpecError,
)
from thesis_runtime.workflows.workflow import WorkflowDoctrine, WorkflowPorts


class ReviewExtension:
    """Small concrete workflow with explicit handoffs and a pure report join."""

    def __init__(self):
        self.inputs = {}
        self.joins = []

    def invoke(self, operation, payload):
        if operation == "validate_launch":
            return {}
        if operation == "request":
            execution = payload["execution"]
            return {
                "workspaceId": execution["workspace_id"],
                "chatId": execution["chat_id"],
                "executionId": execution["execution_id"],
                "triggerMessageId": execution["trigger_message_id"],
                "requestedBy": execution["requested_by"],
                "intentEpoch": execution["intent_epoch"],
            }
        if operation == "state":
            return {"phase": payload["phase"], "intentEpoch": 0}
        if operation == "validate":
            return {
                "failure": None
                if payload["completion"].get("report")
                else "report required"
            }
        if operation == "seed":
            self.inputs[payload["phase"]] = payload["history"]
            return {"text": "Review the accepted subject independently."}
        if operation == "execute":
            reports = payload["inputs"]
            self.joins.append(reports)
            return {"completion": {"report": reports}}
        if operation == "report":
            return {"summary": "Accepted review", "artifact": {"type": "text"}}
        raise AssertionError(operation)


class InterruptedStore(InMemoryExecutionStore):
    """Storage boundary which can lose an acknowledgement after durable acceptance."""

    interrupt_phase = None

    def append_transition(self, execution_id, transition, *, expected_revision=None):
        accepted = super().append_transition(
            execution_id, transition, expected_revision=expected_revision
        )
        if self.interrupt_phase == transition.from_phase:
            self.interrupt_phase = None
            raise ExecutionStoreError("response lost after acceptance")
        return accepted


def build(ports, extension, *, nested=False):
    phases = ("start", "left", "repair", "right", "right_repair", "join", "done") + (
        ("source_check", "timing_check", "inner_join") if nested else ()
    )
    builder = WorkflowBuilder().workflow(
        "graph-review", version="2.0.0", title="Review"
    )
    for phase in ("start", "left", "repair", "right", "right_repair"):
        builder.phase(OneShotPhase(phase=phase, seed="seed"))
    builder.phase(
        DeterministicPhase(
            phase="join",
            inputs=("left", "right"),
            contract="execute",
            on_complete="done",
            replay_safe=True,
        )
    )
    builder.phase(TerminalPhase(phase="done"))
    builder.routes(
        {
            "start": {"split": ("left.input", "right.input")},
            "left": {"repair": ("repair.input",), "join": ("join.left",)},
            "repair": {"left": ("left.input",)},
            "right": {"right_repair": ("right_repair.input",), "join": ("join.right",)},
            "right_repair": {"right": ("right.input",)},
            "join": {"done": ("done.input",)},
            "done": {},
        }
    )
    builder.doctrine(
        WorkflowDoctrine(
            WorkflowDefinition(
                workflow_id="graph-review",
                version="2.0.0",
                title="Review",
                phases=phases,
            ),
            "start",
            "reviewer",
            ("start", "left", "repair", "right"),
            frozenset(),
            {phase: ("reviewer", phase) for phase in phases},
            {},
            {},
            "Reviewing",
            {},
            extension,
        )
    )
    if nested:
        for phase in ("source_check", "timing_check"):
            builder.phase(OneShotPhase(phase=phase, seed="seed"))
        builder.phase(
            DeterministicPhase(
                phase="inner_join",
                inputs=("source", "timing"),
                contract="execute",
                on_complete="repair",
                replay_safe=True,
            )
        )
        builder.route("left", "inner_split", "source_check.input", "timing_check.input")
        builder.route("source_check", "inner_join", "inner_join.source")
        builder.route("timing_check", "inner_join", "inner_join.timing")
        builder.route("inner_join", "repair", "repair.input")
        builder.route("repair", "join", "join.left")
    return builder.ports(ports).build()


@pytest.fixture
def kit():
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Review", owner_actor_id="operator")
    trigger = chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Review"
    )
    executions = InterruptedStore()
    conversations = Conversations(chat=chat, spawn_thread=lambda _node: None)
    extension = ReviewExtension()
    ports = WorkflowPorts(
        chat,
        lambda: datetime(2026, 10, 13, tzinfo=UTC),
        lambda prefix: prefix + "-" + uuid4().hex,
        conversations=conversations,
        execution_store=executions,
    )
    workflow = build(ports, extension)
    request = workflow.launch(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        inputs={},
        trigger_message_id=trigger.message_id,
    )
    workflow.prepare_phase(request)

    def node(phase):
        return next(
            item
            for item in conversations.store.list_sub_agents("workspace")
            if item.workflow_execution_id == request.execution_id
            and item.phase == phase
            and item.phase_executor
        )

    def complete(phase, target, report=None):
        return workflow.apply_phase_completion(
            request,
            node(phase).sub_agent_id,
            {"target": target, "report": report or phase},
            attempt_id=str(
                getattr(
                    workflow.subagent_lifecycle.get(
                        request.workspace_id, node(phase).sub_agent_id
                    ),
                    "phase_attempt_id",
                    "unbound",
                )
            ),
        )

    return SimpleNamespace(
        chat=chat,
        ports=ports,
        workflow=workflow,
        request=request,
        node=node,
        complete=complete,
        extension=extension,
        executions=executions,
    )


def test_branch_loop_waiting_join_and_frozen_inputs(kit):
    kit.complete("start", "split", "subject-v1")
    assert all(
        event.payload["phase"] in kit.workflow.phase_names
        for event in kit.chat.replay_events(
            "workspace", kit.request.execution_id
        ).events
        if event.event_type.value == "workflow.phase"
    )
    left_input = kit.extension.inputs["left"]
    assert left_input == kit.extension.inputs["right"]
    kit.complete("right", "right_repair")
    kit.complete("right_repair", "right")
    assert kit.chat.get_execution(
        "workspace", kit.request.execution_id
    ).active_phases == ("left", "right")
    kit.complete("right", "join", "right-report")
    assert kit.extension.joins == []
    kit.complete("left", "repair")
    kit.complete("repair", "left")
    assert kit.extension.inputs["left"][-1]["fromPhase"] == "repair"
    assert all(item["fromPhase"] != "right" for item in kit.extension.inputs["left"])
    kit.complete("left", "join", "left-report")
    assert kit.extension.joins == [
        {
            "left": {"target": "join", "report": "left-report"},
            "right": {"target": "join", "report": "right-report"},
        }
    ]
    assert (
        kit.chat.get_execution("workspace", kit.request.execution_id).state.value
        == "completed"
    )


def test_racing_arrivals_and_identical_retry_have_one_join(kit):
    kit.complete("start", "split")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda phase: kit.complete(phase, "join"), ("left", "right")))
    assert len(kit.extension.joins) == 1
    before = kit.executions.get(kit.request.execution_id)
    kit.complete("left", "join")
    assert kit.executions.get(kit.request.execution_id) == before
    with pytest.raises(PhaseCompletionRejected, match="conflict"):
        kit.complete("left", "join", "changed")


def test_rebuilt_host_preserves_arrival_and_rejects_cancelled_completion(kit):
    kit.complete("start", "split")
    kit.complete("right", "join")
    rebuilt = build(kit.ports, kit.extension)
    rebuilt.prepare_phase(kit.request)
    assert kit.extension.joins == []
    kit.chat.cancel_workflow("workspace", kit.request.execution_id, actor_id="operator")

    class RefusingProvider:
        def request_snapshot(self, *args, **kwargs):
            pytest.fail("a cancelled branch prepared a provider call")

        def stream(self, *args, **kwargs):
            pytest.fail("a cancelled branch dispatched a provider call")

    conversations = Conversations(
        chat=kit.chat, provider=RefusingProvider(), spawn_thread=lambda _node: None
    )
    assert (
        conversations.run_node_turn(
            "workspace", kit.request.chat_id, kit.node("left").sub_agent_id
        ).status
        == "cancelled"
    )
    with pytest.raises(PhaseCompletionRejected):
        rebuilt.apply_phase_completion(
            kit.request,
            kit.node("left").sub_agent_id,
            {"target": "join", "report": "late"},
            attempt_id=str(
                getattr(
                    rebuilt.subagent_lifecycle.get(
                        kit.request.workspace_id, kit.node("left").sub_agent_id
                    ),
                    "phase_attempt_id",
                    "unbound",
                )
            ),
        )
    assert kit.extension.joins == []


def test_compiler_rejects_undeclared_input_port(kit):
    from dataclasses import replace

    from thesis_runtime.workflows.graph.contracts import Destination

    spec = kit.workflow.spec
    with pytest.raises(WorkflowSpecError, match="undeclared destination"):
        replace(
            spec,
            routes={
                **spec.routes,
                "right": {"join": (Destination("join", "missing"),)},
            },
        )


def test_interrupted_pure_join_resumes_under_the_same_attempt(kit):
    from dataclasses import replace
    from typing import cast

    from azure.cosmos import ContainerProxy
    from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer

    from thesis_runtime.adapters.cosmos.work import CosmosWorkStore

    kit.complete("start", "split")
    queue = CosmosWorkStore(cast(ContainerProxy, LimitedContainer()))
    ports = replace(kit.ports, work_store=queue)
    workflow = build(ports, kit.extension)
    for phase in ("left", "right"):
        record = kit.node(phase)
        workflow.apply_phase_completion(
            kit.request,
            record.sub_agent_id,
            {"target": "join", "report": phase},
            attempt_id=record.phase_attempt_id,
        )
    original = next(
        work
        for work in (queue.get(ref.workspace_id, ref.key) for ref in queue.ready())
        if work.payload["phase"] == "join"
    )
    queue.transition(original, "interrupted")
    rebuilt = build(ports, kit.extension)
    rebuilt.prepare_phase(kit.request)
    rebuilt.prepare_phase(kit.request)
    recovered = [
        work
        for work in (queue.get(ref.workspace_id, ref.key) for ref in queue.ready())
        if work.payload["phase"] == "join"
    ]
    assert len(recovered) == 1
    assert recovered[0].payload["attempt_id"] == original.payload["attempt_id"]
    rebuilt.enter_scheduled_phase(kit.request, recovered[0].payload)
    rebuilt.enter_scheduled_phase(kit.request, original.payload)
    assert len(kit.extension.joins) == 1
    assert (
        kit.chat.get_execution("workspace", kit.request.execution_id).state.value
        == "completed"
    )


def test_recovery_reconciles_a_stored_arrival_and_terminal_without_repeating_reviews(
    kit,
):
    kit.complete("start", "split")
    kit.executions.interrupt_phase = "right"
    with pytest.raises(ExecutionStoreError, match="response lost"):
        kit.complete("right", "join")
    rebuilt = build(kit.ports, kit.extension)
    rebuilt.prepare_phase(kit.request)
    assert kit.node("right").status.value == "completed"
    assert kit.chat.get_execution(
        "workspace", kit.request.execution_id
    ).active_phases == ("left",)
    assert kit.extension.joins == []
    # A second lost acknowledgement happens after the pure join was accepted.
    recovered = kit.chat.replay_events("workspace", kit.request.execution_id).events
    assert any(
        event.payload.get("activePhases") == ["left"]
        and event.payload.get("controlSequence") == 2
        for event in recovered
    )
    kit.executions.interrupt_phase = "join"
    kit.complete("left", "join")
    assert len(kit.extension.joins) == 1
    assert (
        kit.chat.get_execution("workspace", kit.request.execution_id).state.value
        == "completed"
    )


def test_nested_regions_repeat_without_reusing_arrivals_or_attempt_authority(kit):
    workflow = build(kit.ports, kit.extension, nested=True)
    trigger = kit.chat.send_message(
        "workspace", kit.request.chat_id, actor_id="operator", body="Nested review"
    )
    request = workflow.launch(
        "workspace",
        kit.request.chat_id,
        actor_id="operator",
        inputs={},
        trigger_message_id=trigger.message_id,
    )
    workflow.prepare_phase(request)

    def node(phase):
        return next(
            item
            for item in kit.ports.conversations.store.list_sub_agents("workspace")
            if item.workflow_execution_id == request.execution_id
            and item.phase == phase
            and item.phase_executor
        )

    def complete(phase, target):
        record = node(phase)
        return workflow.apply_phase_completion(
            request,
            record.sub_agent_id,
            {"target": target, "report": phase},
            attempt_id=record.phase_attempt_id,
        )

    complete("start", "split")
    complete("right", "join")
    complete("left", "inner_split")
    first_attempt = node("source_check").phase_attempt_id
    complete("source_check", "inner_join")
    assert kit.extension.joins == []
    complete("timing_check", "inner_join")
    assert len(kit.extension.joins) == 1
    complete("repair", "left")
    complete("left", "inner_split")
    assert node("source_check").phase_attempt_id != first_attempt
    from thesis_runtime.subagents.control import ControlToolService

    control = ControlToolService(conversations=kit.ports.conversations)
    with pytest.raises(PhaseCompletionRejected, match="attempt"):
        control.complete_phase(
            "workspace",
            node("source_check").sub_agent_id,
            completion={"target": "inner_join", "report": "stale", "intentEpoch": 0},
            expected_attempt_id=first_attempt,
        )
    complete("source_check", "inner_join")
    assert len(kit.extension.joins) == 1
    complete("timing_check", "inner_join")
    complete("repair", "join")
    assert len(kit.extension.joins) == 3
    assert (
        kit.chat.get_execution("workspace", request.execution_id).state.value
        == "completed"
    )


def test_phase_completion_requires_real_workflow_authority_and_an_applier(kit):
    """Replaces fake-lifecycle unit tests at the actual conversation boundary."""
    from thesis_runtime.contracts.records import SubAgentKind
    from thesis_runtime.conversations.delivery import Anchor
    from thesis_runtime.subagents.control import ControlToolService
    from thesis_runtime.subagents.specs import SubAgentSpec

    conversations = kit.ports.conversations
    control = ControlToolService(conversations=conversations)
    helper = conversations.spawn(
        Anchor("workspace", kit.request.chat_id),
        SubAgentSpec(role="helper", kind=SubAgentKind.ONE_SHOT),
        "Inspect",
        SubAgentKind.ONE_SHOT,
    )
    with pytest.raises(PhaseCompletionRejected, match="workflow phase node"):
        control.complete_phase(
            "workspace",
            helper.conversation_id,
            completion={"report": "helper", "intentEpoch": 0},
            expected_attempt_id=None,
        )
    record = kit.node("start")
    with pytest.raises(PhaseCompletionRejected, match="no workflow is available"):
        control.complete_phase(
            "workspace",
            record.sub_agent_id,
            completion={"target": "split", "report": "start", "intentEpoch": 0},
            expected_attempt_id=record.phase_attempt_id,
        )


def test_completion_tool_preserves_current_attempt_authority(kit):
    """Dispatch the actual tool/service boundary, including trusted attempt identity."""
    from dataclasses import replace

    from thesis_runtime.contracts.context import ToolContext
    from thesis_runtime.subagents.control import ControlToolService
    from thesis_runtime.subagents.control_tools import control_registered_tools
    from thesis_runtime.tools.registry import SessionToolRegistry

    record = kit.node("start")

    def apply(workspace, node, completion):
        return kit.workflow.apply_phase_completion(
            kit.request, node.sub_agent_id, completion, attempt_id=node.phase_attempt_id
        )

    service = ControlToolService(
        conversations=kit.ports.conversations, phase_completions=apply
    )
    context = ToolContext(
        workspace_id="workspace",
        chat_id=kit.request.chat_id,
        conversation_id=record.sub_agent_id,
        actor_id="operator",
        sub_agent_id=record.sub_agent_id,
        workflow_execution_id=kit.request.execution_id,
        phase_attempt_id=record.phase_attempt_id,
        phase_executor=True,
        allowed_tools=("request_phase_completion",),
    )
    arguments = {"completion": {"target": "split", "report": "start"}}
    stale = replace(context, phase_attempt_id="stale-attempt")
    rejected = SessionToolRegistry(control_registered_tools(service, stale)).dispatch(
        "request_phase_completion", arguments, context=stale
    )
    assert not rejected.ok
    assert kit.executions.get(kit.request.execution_id).transitions == ()
    accepted = SessionToolRegistry(control_registered_tools(service, context)).dispatch(
        "request_phase_completion", arguments, context=context
    )
    assert accepted.ok and accepted.terminal
    assert kit.node("start").status.value == "completed"
    assert kit.chat.get_execution(
        "workspace", kit.request.execution_id
    ).active_phases == ("left", "right")
    assert (
        len(
            [
                t
                for t in kit.executions.get(kit.request.execution_id).transitions
                if t.from_phase == "start"
            ]
        )
        == 1
    )


def test_execution_history_rejects_the_removed_transition_format(kit):
    from thesis_runtime.workflows.execution_store import ExecutionDocument

    kit.complete("start", "split")
    payload = kit.executions.get(kit.request.execution_id).as_dict()
    transition = payload["transitions"][0]
    transition["toPhase"] = transition.pop("route")
    transition["completion"] = transition.pop("output")
    with pytest.raises(ExecutionStoreError, match="outdated workflow history"):
        ExecutionDocument.from_dict(payload)
