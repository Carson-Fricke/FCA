"""Single-owner durable intake and execution acceptance race contracts."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.contracts.activity import decode_activity, is_external_input
from thesis_runtime.contracts.codec import record_from_payload
from thesis_runtime.contracts.records import (
    WORKFLOW_NODE_ROLE,
    ChatContractError,
    ChatMessage,
    ChatThread,
    InvalidLifecycleTransition,
    MessageRole,
    RecordNotFound,
    SubAgentRecord,
    SubAgentStatus,
    ToolJob,
    ToolJobState,
    WorkflowDefinition,
    WorkflowExecution,
)
from thesis_runtime.conversations.service import ChatService, InMemoryChatPersistence
from thesis_runtime.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventType,
)

WORKSPACE = "workspace-a"


def _execution(service: ChatService) -> WorkflowExecution:
    chat = service.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    return service.launch_workflow(
        WORKSPACE,
        chat.chat_id,
        workflow=WorkflowDefinition("research", "1.0.0", "Research", ("creation",)),
        actor_id="operator",
        body="Start research",
    )


def _accept(service: ChatService, execution: WorkflowExecution, commit, **kwargs):
    return service.accept_transition(
        WORKSPACE,
        execution.execution_id,
        expected_intent_epoch=execution.intent_epoch,
        expected_phase="creation",
        expected_node_id=None,
        expected_attempt_id=None,
        commit=commit,
        **kwargs,
    )


def test_durable_positions_restart_and_scope(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    service = ChatService(FileChatPersistence(root))
    chats = [
        service.create_chat(workspace, title="Research", owner_actor_id="operator")
        for workspace in (WORKSPACE, WORKSPACE, "workspace-b")
    ]
    for chat in chats:
        for anchor in (None, "child-a", "child-b"):
            for index in range(5):
                message = service.create_message(
                    chat.workspace_id,
                    chat.chat_id,
                    actor_id="operator",
                    body=f"Input {index}",
                    role=MessageRole.USER if index % 2 else MessageRole.TOOL,
                    data={"subAgentId": anchor, "activityKind": "delivery"},
                )
                assert message.input_position == index + 1

    restarted = ChatService(FileChatPersistence(root))
    for chat in chats:
        for anchor in (None, "child-a", "child-b"):
            message = restarted.send_message(
                chat.workspace_id,
                chat.chat_id,
                actor_id="operator",
                body="After restart",
                data={"subAgentId": anchor, "activityKind": "delivery"},
            )
            assert message.input_position == 6
            inputs = sorted(
                (
                    row
                    for row in restarted.list_messages(chat.workspace_id, chat.chat_id)
                    if decode_activity(row).sub_agent_id == anchor
                ),
                key=lambda row: row.input_position,
            )
            assert [row.input_position for row in inputs] == list(range(1, 7))
            assert len({row.message_id for row in inputs}) == 6


@pytest.mark.parametrize(
    ("role", "data"),
    [
        (MessageRole.ASSISTANT, {}),
        (MessageRole.TOOL, {"activityKind": "runtime_tool"}),
        (
            MessageRole.TOOL,
            {"activityKind": "runtime_tool", "toolActivity": "finished"},
        ),
        (MessageRole.USER, {"activityKind": "model_output"}),
    ],
)
def test_outputs_never_consume_positions(role, data) -> None:
    service = ChatService()
    execution = _execution(service)
    output = service.create_message(
        WORKSPACE,
        execution.chat_id,
        actor_id="model",
        body="Output",
        role=role,
        data=data,
    )
    assert not is_external_input(output)
    assert output.input_position == 0
    message = service.send_message(
        WORKSPACE, execution.chat_id, actor_id="operator", body="Input"
    )
    assert message.input_position == 2


def test_concurrent_intake_is_unique_and_monotonic() -> None:
    service = ChatService()
    execution = _execution(service)

    def send(index: int) -> ChatMessage:
        return service.send_message(
            WORKSPACE,
            execution.chat_id,
            actor_id="operator",
            body=f"Input {index}",
            data={"subAgentId": "child"},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        messages = tuple(pool.map(send, range(120)))
    assert sorted(row.input_position for row in messages) == list(range(1, 121))
    persisted = service.list_messages(WORKSPACE, execution.chat_id)
    inputs = [row for row in persisted if decode_activity(row).sub_agent_id == "child"]
    assert [row.input_position for row in inputs] == list(range(1, 121))
    assert len({row.message_id for row in inputs}) == 120


def test_failed_message_persistence_does_not_consume_position() -> None:
    class Persistence(InMemoryChatPersistence):
        fail = False

        def save(self, record):
            if self.fail and isinstance(record, ChatMessage):
                raise RuntimeError("storage unavailable")
            return super().save(record)

    persistence = Persistence()
    service = ChatService(persistence)
    execution = _execution(service)
    persistence.fail = True
    with pytest.raises(RuntimeError, match="storage unavailable"):
        service.send_message(
            WORKSPACE, execution.chat_id, actor_id="operator", body="Not accepted"
        )
    persistence.fail = False
    message = service.send_message(
        WORKSPACE, execution.chat_id, actor_id="operator", body="Accepted"
    )
    assert message.input_position == 2


def test_steering_wins_before_acceptance_callback() -> None:
    service = ChatService()
    execution = _execution(service)
    entered = Event()
    release = Event()
    called = Event()

    def steer() -> None:
        with service.execution_guard(WORKSPACE, execution.execution_id):
            entered.set()
            assert release.wait(5)
            service.steer_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator", body="Change"
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        steering = pool.submit(steer)
        assert entered.wait(5)
        acceptance = pool.submit(_accept, service, execution, called.set)
        release.set()
        steering.result(timeout=5)
        with pytest.raises(InvalidLifecycleTransition, match="epoch is stale"):
            acceptance.result(timeout=5)
    assert not called.is_set()


@pytest.mark.parametrize("operation", ["steer", "stop", "phase"])
def test_acceptance_blocks_mutations_until_projection(operation: str) -> None:
    service = ChatService()
    execution = _execution(service)
    committing = Event()
    release = Event()
    mutating = Event()

    def commit() -> str:
        committing.set()
        assert release.wait(5)
        return "committed"

    def mutate() -> WorkflowExecution:
        mutating.set()
        if operation == "steer":
            return service.steer_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator", body="Change"
            )
        if operation == "stop":
            return service.stop_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator"
            )
        return service.set_execution_phases(
            WORKSPACE, execution.execution_id, ("next",)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        acceptance = pool.submit(
            _accept, service, execution, commit, target_phases=("completed",)
        )
        assert committing.wait(5)
        mutation = pool.submit(mutate)
        assert mutating.wait(5)
        assert not mutation.done()
        release.set()
        assert acceptance.result(timeout=5) == "committed"
        updated = mutation.result(timeout=5)
    assert updated.phase == ("next" if operation == "phase" else "completed")
    if operation == "steer":
        assert updated.intent_epoch == 1
    with pytest.raises(InvalidLifecycleTransition):
        _accept(service, execution, lambda: pytest.fail("duplicate commit"))


def test_failed_commit_does_not_project_phase() -> None:
    service = ChatService()
    execution = _execution(service)

    def conflict() -> None:
        raise RuntimeError("CAS conflict")

    with pytest.raises(RuntimeError, match="CAS conflict"):
        _accept(service, execution, conflict, target_phases=("completed",))
    assert service.get_execution(WORKSPACE, execution.execution_id) == execution


@pytest.mark.parametrize("mutation", ["epoch", "phase", "stop", "cancel"])
def test_stale_or_terminal_acceptance_never_calls_commit(mutation: str) -> None:
    service = ChatService()
    execution = _execution(service)
    if mutation == "epoch":
        service.steer_workflow(
            WORKSPACE, execution.execution_id, actor_id="operator", body="Change"
        )
    elif mutation == "phase":
        service.set_execution_phases(WORKSPACE, execution.execution_id, ("next",))
    elif mutation == "stop":
        service.stop_workflow(WORKSPACE, execution.execution_id, actor_id="operator")
    else:
        service.cancel_workflow(WORKSPACE, execution.execution_id, actor_id="operator")
    with pytest.raises(InvalidLifecycleTransition):
        _accept(service, execution, lambda: pytest.fail("stale commit"))


@pytest.mark.parametrize(
    "mutation",
    [None, "executor", "workflow", "parent", "phase", "attempt", "status", "reentry"],
)
def test_acceptance_checks_current_phase_owner(mutation: str | None) -> None:
    service = ChatService()
    execution = _execution(service)
    owner = SubAgentRecord(
        workspace_id=WORKSPACE,
        created_at=execution.created_at,
        created_by="operator",
        sub_agent_id="workflow-node",
        chat_id=execution.chat_id,
        role=WORKFLOW_NODE_ROLE,
        workflow_execution_id=execution.execution_id,
        status=SubAgentStatus.RUNNING,
        conversation_id="workflow-node",
        root_conversation_id=execution.chat_id,
        parent_conversation_id=execution.chat_id,
    )
    service.persistence.save(owner)
    service.event_store.append(
        ExecutionEvent(
            execution_id=execution.execution_id,
            workspace_id=WORKSPACE,
            event_type=ExecutionEventType.WORKFLOW_PHASE,
            correlation_id=execution.execution_id,
            actor_id="workflow",
            actor_scope=ActorScope.WORKFLOW,
            occurred_at=execution.created_at,
            payload={"phase": "creation"},
        )
    )
    attempt = f"{execution.execution_id}:creation:1"
    executor = SubAgentRecord(
        workspace_id=WORKSPACE,
        created_at=execution.created_at,
        created_by="operator",
        chat_id=execution.chat_id,
        workflow_execution_id=execution.execution_id,
        status=SubAgentStatus.RUNNING,
        sub_agent_id="phase-node",
        conversation_id="phase-node",
        parent_conversation_id=owner.sub_agent_id,
        role="phase-agent",
        parent_sub_agent_id=owner.sub_agent_id,
        phase="creation",
        phase_attempt_id=attempt,
        phase_executor=True,
        root_conversation_id=execution.chat_id,
    )
    changes = {
        "executor": {"phase_executor": False},
        "workflow": {"workflow_execution_id": "other-execution"},
        "parent": {
            "parent_sub_agent_id": "other-owner",
            "parent_conversation_id": "other-owner",
        },
        "phase": {"phase": "other-phase"},
        "attempt": {"phase_attempt_id": f"{execution.execution_id}:creation:0"},
        "reentry": {"phase_attempt_id": f"{execution.execution_id}:creation:2"},
        "status": {"status": SubAgentStatus.COMPLETED},
    }
    from dataclasses import replace

    service.persistence.save(replace(executor, **changes.get(mutation, {})))
    if mutation == "reentry":
        service.event_store.append(
            ExecutionEvent(
                execution_id=execution.execution_id,
                workspace_id=WORKSPACE,
                event_type=ExecutionEventType.WORKFLOW_PHASE,
                correlation_id=execution.execution_id,
                actor_id="workflow",
                actor_scope=ActorScope.WORKFLOW,
                occurred_at=execution.created_at,
                payload={"phase": "creation"},
            )
        )
    called = Event()

    def accept() -> None:
        service.accept_transition(
            WORKSPACE,
            execution.execution_id,
            expected_intent_epoch=0,
            expected_phase="creation",
            expected_node_id=executor.sub_agent_id,
            expected_attempt_id=attempt,
            commit=called.set,
            target_phases=("completed",),
        )

    if mutation is None:
        accept()
        assert called.is_set()
    else:
        with pytest.raises(InvalidLifecycleTransition, match="current phase executor"):
            accept()
        assert not called.is_set()


def test_invalid_ownership_metadata_is_rejected() -> None:
    service = ChatService()
    execution = _execution(service)
    with pytest.raises(ValueError, match="subAgentId"):
        service.send_message(
            WORKSPACE,
            execution.chat_id,
            actor_id="operator",
            body="Input",
            data={"subAgentId": 123},
        )


def test_distributed_ownership_requires_an_injected_store() -> None:
    with pytest.raises(ValueError, match="ownership"):
        ChatService(single_process=False)


def test_mixed_intake_steering_and_acceptance_has_no_lock_cycle() -> None:
    service = ChatService()
    execution = _execution(service)

    def operate(index: int) -> None:
        if index % 3 == 0:
            service.steer_workflow(
                WORKSPACE,
                execution.execution_id,
                actor_id="operator",
                body=f"Steer {index}",
            )
        elif index % 3 == 1:
            service.create_message(
                WORKSPACE,
                execution.chat_id,
                actor_id="child",
                body=f"Delivery {index}",
                role=MessageRole.TOOL,
                data={
                    "subAgentId": "child",
                    "activityKind": "delivery",
                    "deliveryKey": f"delivery-{index}",
                },
            )
        else:
            observed = service.get_execution(WORKSPACE, execution.execution_id)
            try:
                _accept(service, observed, lambda: None)
            except InvalidLifecycleTransition:
                pass

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(operate, index) for index in range(180)]
        for future in futures:
            future.result(timeout=10)
    updated = service.get_execution(WORKSPACE, execution.execution_id)
    assert updated.intent_epoch == 60
    assert len(updated.steering_message_ids) == 60
    messages = service.list_messages(WORKSPACE, execution.chat_id)
    for anchor, count in ((None, 61), ("child", 60)):
        positions = sorted(
            message.input_position
            for message in messages
            if decode_activity(message).sub_agent_id == anchor
        )
        assert positions == list(range(1, count + 1))


def test_consumed_positions_restart_and_scope(tmp_path: Path) -> None:
    root = tmp_path / "chat"
    first = ChatService(FileChatPersistence(root))
    chats = [
        first.create_chat(workspace, title="Research", owner_actor_id="operator")
        for workspace in (WORKSPACE, WORKSPACE, "workspace-b")
    ]
    for index, chat in enumerate(chats):
        for anchor in ("", "child-a", "child-b"):
            assert (
                first.read_consumed_input(chat.workspace_id, chat.chat_id, anchor) == 0
            )
            first.acknowledge_input(chat.workspace_id, chat.chat_id, anchor, index + 5)
            first.acknowledge_input(chat.workspace_id, chat.chat_id, anchor, 1)

    # Ownership transfers on restart; the two services never write concurrently.
    restarted = ChatService(FileChatPersistence(root))
    for index, chat in enumerate(chats):
        for anchor in ("", "child-a", "child-b"):
            assert (
                restarted.read_consumed_input(chat.workspace_id, chat.chat_id, anchor)
                == index + 5
            )
        restarted.send_message(
            chat.workspace_id, chat.chat_id, actor_id="operator", body="After restart"
        )
        restarted.acknowledge_input(chat.workspace_id, chat.chat_id, "child-a", 20)
        snapshot = restarted.get_chat(chat.workspace_id, chat.chat_id)
        assert snapshot.input_consumed_positions == {
            "": index + 5,
            "child-a": 20,
            "child-b": index + 5,
        }
        assert (
            restarted.read_consumed_input(chat.workspace_id, chat.chat_id, "new") == 0
        )


def test_chat_without_consumption_positions_is_outdated() -> None:
    service = ChatService()
    chat = service.create_chat(WORKSPACE, title="Current", owner_actor_id="operator")
    payload = chat.as_dict()
    del payload["input_consumed_positions"]
    with pytest.raises(ChatContractError, match="outdated"):
        record_from_payload(ChatThread, payload)


def test_consumed_positions_unknown_chats_are_rejected() -> None:
    service = ChatService()
    with pytest.raises(RecordNotFound):
        service.read_consumed_input(WORKSPACE, "missing", "")
    with pytest.raises(RecordNotFound):
        service.acknowledge_input(WORKSPACE, "missing", "", 0)


@pytest.mark.parametrize("position", [-1, True, 1.5])
def test_invalid_consumed_position_is_rejected(position) -> None:
    service = ChatService()
    execution = _execution(service)
    with pytest.raises(ValueError, match="non-negative integer"):
        service.acknowledge_input(WORKSPACE, execution.chat_id, "", position)


def test_acknowledgement_serializes_with_message_admission() -> None:
    saved = Event()
    release = Event()

    class Persistence(InMemoryChatPersistence):
        pause = False

        def save(self, record):
            result = super().save(record)
            if self.pause and isinstance(record, ChatMessage):
                saved.set()
                assert release.wait(5)
            return result

    persistence = Persistence()
    service = ChatService(persistence)
    execution = _execution(service)
    persistence.pause = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        message = pool.submit(
            service.send_message,
            WORKSPACE,
            execution.chat_id,
            actor_id="child",
            body="Input",
            data={"subAgentId": "child"},
        )
        assert saved.wait(5)
        acknowledgement = pool.submit(
            service.acknowledge_input, WORKSPACE, execution.chat_id, "", 1
        )
        assert not acknowledgement.done()
        release.set()
        delivered = message.result(timeout=5)
        acknowledgement.result(timeout=5)
    service.acknowledge_input(
        WORKSPACE, execution.chat_id, "child", delivered.input_position
    )
    service.create_message(
        WORKSPACE,
        execution.chat_id,
        actor_id="model",
        body="Output",
        role=MessageRole.ASSISTANT,
    )
    assert service.get_chat(WORKSPACE, execution.chat_id).input_consumed_positions == {
        "": 1,
        "child": 1,
    }


@pytest.mark.parametrize("operation", ["archive", "references", "launch", "tool"])
def test_chat_mutations_preserve_acknowledgements(operation: str) -> None:
    service = ChatService()
    execution = _execution(service)
    service.acknowledge_input(WORKSPACE, execution.chat_id, "", 1)
    service.acknowledge_input(WORKSPACE, execution.chat_id, "child", 3)
    if operation == "archive":
        service.archive_chat(WORKSPACE, execution.chat_id)
    elif operation == "references":
        service.link_resources(
            WORKSPACE, execution.chat_id, {"example.document": ("doc-1",)}
        )
    elif operation == "launch":
        service.launch_workflow(
            WORKSPACE,
            execution.chat_id,
            workflow=WorkflowDefinition("research", "1.0.0", "Research", ("creation",)),
            actor_id="operator",
            body="Start again",
        )
    else:
        service.submit_tool_job(
            WORKSPACE, execution.execution_id, tool_name="research", actor_id="operator"
        )
    assert service.get_chat(WORKSPACE, execution.chat_id).input_consumed_positions == {
        "": 1,
        "child": 3,
    }


def test_concurrent_anchor_acknowledgements_and_bookkeeping_preserve_maxima() -> None:
    service = ChatService()
    execution = _execution(service)
    anchors = ("", "child-a", "child-b")

    def operate(index: int) -> None:
        anchor = anchors[index % len(anchors)]
        service.acknowledge_input(WORKSPACE, execution.chat_id, anchor, index + 1)
        service.send_message(
            WORKSPACE,
            execution.chat_id,
            actor_id="operator",
            body=f"Input {index}",
            data={"subAgentId": anchor or None},
        )
        service.acknowledge_input(WORKSPACE, execution.chat_id, anchor, 0)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(operate, index) for index in range(120)]
        for future in futures:
            future.result(timeout=10)
    assert service.get_chat(WORKSPACE, execution.chat_id).input_consumed_positions == {
        "": 118,
        "child-a": 119,
        "child-b": 120,
    }


@pytest.mark.parametrize("mutation", ["steer", "accept", "stop", "cancel"])
def test_tool_submission_rereads_execution_after_preparation(mutation: str) -> None:
    preparing = Event()
    release = Event()

    def identity(prefix: str) -> str:
        if prefix == "job":
            preparing.set()
            assert release.wait(5)
        return f"{prefix}-{uuid4().hex}"

    service = ChatService(id_factory=identity)
    execution = _execution(service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        submission = pool.submit(
            service.submit_tool_job,
            WORKSPACE,
            execution.execution_id,
            tool_name="research",
            actor_id="operator",
        )
        assert preparing.wait(5)
        if mutation == "steer":
            service.steer_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator", body="Change"
            )
        elif mutation == "accept":
            _accept(service, execution, lambda: None, target_phases=("next",))
        elif mutation == "stop":
            service.stop_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator"
            )
        else:
            service.cancel_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator"
            )
        changed = service.get_execution(WORKSPACE, execution.execution_id)
        release.set()
        if mutation in ("stop", "cancel"):
            with pytest.raises(InvalidLifecycleTransition):
                submission.result(timeout=5)
            assert not service.persistence.list(ToolJob, WORKSPACE)
        else:
            job = submission.result(timeout=5)
            assert job.intent_epoch == changed.intent_epoch
            assert (
                job.job_id
                in service.get_execution(WORKSPACE, execution.execution_id).job_ids
            )
    updated = service.get_execution(WORKSPACE, execution.execution_id)
    assert updated.phase == changed.phase
    assert updated.intent_epoch == changed.intent_epoch
    assert updated.state == changed.state


@pytest.mark.parametrize("mutation", ["steer", "accept"])
def test_tool_submission_storage_is_in_execution_boundary(mutation: str) -> None:
    storing = Event()
    release = Event()
    mutating = Event()

    class Persistence(InMemoryChatPersistence):
        def save(self, record):
            if isinstance(record, ToolJob):
                storing.set()
                assert release.wait(5)
            return super().save(record)

    service = ChatService(Persistence())
    execution = _execution(service)

    def mutate() -> None:
        mutating.set()
        if mutation == "steer":
            service.steer_workflow(
                WORKSPACE, execution.execution_id, actor_id="operator", body="Change"
            )
        else:
            _accept(service, execution, lambda: None, target_phases=("next",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        submission = pool.submit(
            service.submit_tool_job,
            WORKSPACE,
            execution.execution_id,
            tool_name="research",
            actor_id="operator",
        )
        assert storing.wait(5)
        mutation_future = pool.submit(mutate)
        assert mutating.wait(5)
        assert not mutation_future.done()
        release.set()
        job = submission.result(timeout=5)
        mutation_future.result(timeout=5)
    updated = service.get_execution(WORKSPACE, execution.execution_id)
    assert updated.intent_epoch == (1 if mutation == "steer" else 0)
    assert updated.phase == ("creation" if mutation == "steer" else "next")
    assert updated.job_ids == (job.job_id,)


def test_tool_start_rereads_result_after_guard_acquisition() -> None:
    observed = Event()
    release = Event()

    class Service(ChatService):
        pause = False

        def get_tool_job(self, workspace_id: str, job_id: str) -> ToolJob:
            job = super().get_tool_job(workspace_id, job_id)
            if self.pause:
                self.pause = False
                observed.set()
                assert release.wait(5)
            return job

    service = Service()
    execution = _execution(service)
    job = service.submit_tool_job(
        WORKSPACE, execution.execution_id, tool_name="research", actor_id="operator"
    )
    service.pause = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(
            service.start_tool_job, WORKSPACE, job.job_id, actor_id="tool"
        )
        assert observed.wait(5)
        service.record_tool_result(WORKSPACE, job.job_id, result={"ok": True})
        release.set()
        with pytest.raises(InvalidLifecycleTransition):
            starting.result(timeout=5)
    assert service.get_tool_job(WORKSPACE, job.job_id).state is ToolJobState.SUCCEEDED


def test_public_execution_guard_composes_acceptance_and_finalization() -> None:
    service = ChatService()
    execution = _execution(service)
    waiting = Event()

    def cancel() -> None:
        waiting.set()
        service.cancel_workflow(WORKSPACE, execution.execution_id, actor_id="operator")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.execution_guard(WORKSPACE, execution.execution_id):
            _accept(service, execution, lambda: None, target_phases=("next",))
            cancellation = pool.submit(cancel)
            assert waiting.wait(5)
            assert not cancellation.done()
            assert (
                service.get_execution(WORKSPACE, execution.execution_id).phase == "next"
            )
        cancellation.result(timeout=5)
    assert service.get_execution(WORKSPACE, execution.execution_id).phase == "next"
