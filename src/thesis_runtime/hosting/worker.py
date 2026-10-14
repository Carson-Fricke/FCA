"""Bind generic durable worker execution and version-pinned workflow recovery."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    from_record,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    ChatMessage,
    RecordNotFound,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import ROOT_ANCHOR_ID, Anchor
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.events import (
    ActorScope,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis_runtime.execution.worker import DurableWorker, Work, WorkStore
from thesis_runtime.sessions.model_invocation import ModelInvocation
from thesis_runtime.storage.ownership import OwnershipLost, OwnershipStore
from thesis_runtime.workflows.execution_store import ExecutionIdentity, ExecutionStore
from thesis_runtime.workflows.registry import WorkflowRegistry
from thesis_runtime.workflows.state import TERMINAL_EXECUTION_STATES


@dataclass(frozen=True)
class RuntimeHost:
    chat_service: ChatService
    conversations: Conversations | None
    workflows: WorkflowRegistry | None


def build_worker(
    stack: RuntimeHost,
    work_store: WorkStore,
    ownership: OwnershipStore,
    executions: ExecutionStore,
    recovery_records: Callable[[], tuple[ExecutionIdentity, ...]] | None = None,
) -> DurableWorker:
    def conversation(work: Work) -> None:
        if stack.conversations is None:
            raise RuntimeError("worker requires a configured model provider")
        chat_id = str(work.payload["chat_id"])
        anchor_id = str(work.payload["anchor_id"]) or ROOT_ANCHOR_ID
        # Serialize the intake snapshot with operator steering; model execution
        # itself does not hold this bounded mutation lock.
        with stack.chat_service.mutation_guard(
            work.workspace_id, "intake-ready:" + chat_id
        ):
            stack.chat_service.get_chat(work.workspace_id, chat_id)
            consumed = stack.chat_service.read_consumed_input(
                work.workspace_id, chat_id, anchor_id
            )
        if (
            int(cast(int, work.payload.get("input_position", 0))) > 0
            and int(cast(int, work.payload["input_position"])) <= consumed
        ):
            return
        stack.conversations.run_node_turn(work.workspace_id, chat_id, anchor_id)

    def workflow(work: Work) -> None:
        if stack.workflows is None:
            raise RuntimeError("worker requires a configured workflow runtime")
        execution_id = str(work.payload["execution_id"])
        execution = stack.chat_service.get_execution(work.workspace_id, execution_id)
        if execution.state in TERMINAL_EXECUTION_STATES:
            return
        document = executions.get(execution_id)
        stack.workflows.resume(document.identity, document.launch_request, work.payload)

    def interrupted(work: Work) -> None:
        if stack.conversations is None:
            return
        if work.kind == "conversation":
            chat_id = str(work.payload["chat_id"])
            anchor_id = str(work.payload["anchor_id"]) or None
        else:
            execution = stack.chat_service.get_execution(
                work.workspace_id, str(work.payload["execution_id"])
            )
            chat_id, anchor_id = execution.chat_id, execution.conversation_id
            if anchor_id == chat_id:
                anchor_id = None
        try:
            stack.chat_service.get_chat(work.workspace_id, chat_id)
        except RecordNotFound:
            return
        for invocation in stack.chat_service.persistence.list_conversation(
            AgentInvocation,
            work.workspace_id,
            chat_id,
            conversation_id=anchor_id or chat_id,
        ):
            if (
                invocation.kind is not AgentInvocationKind.MODEL
                or invocation.state is not AgentInvocationState.STARTED
            ):
                continue
            output_id = f"message-{invocation.invocation_id}"
            committed = stack.chat_service.persistence.get(
                ChatMessage, work.workspace_id, output_id
            )
            if committed is not None:
                content = from_record(committed).content
                if (content.result or {}).get("interrupted") is True:
                    ModelInvocation(
                        invocation,
                        output_id,
                        stack.chat_service.timeline,
                        lambda: False,
                        store=stack.conversations.store,
                    ).interrupt(
                        {
                            "kind": "operator_steering",
                            "message": "Model generation interrupted by new operator input.",
                            "certainty": "observed_failure",
                            "retry_safe": False,
                        }
                    )
                else:
                    stack.conversations.store.save_invocation(
                        replace(invocation, state=AgentInvocationState.SUCCEEDED)
                    )
            else:
                ModelInvocation(
                    invocation,
                    output_id,
                    stack.chat_service.timeline,
                    lambda: False,
                    store=stack.conversations.store,
                ).interrupt(
                    {
                        "kind": "execution_interrupted",
                        "message": "Model execution was interrupted.",
                        "certainty": "unknown_outcome",
                        "retry_safe": False,
                    }
                )
        message_id = "interrupted:" + work.key
        stack.conversations.deliver_session_message(
            Anchor(work.workspace_id, chat_id, anchor_id),
            SessionMessage(
                message_id,
                "incoming",
                MessageContent(
                    "Execution was interrupted. Previously dispatched work may still be running. "
                    "Inspect retained job IDs and results before intentionally starting new work.",
                    "runtime",
                ),
            ),
            schedule=False,
        )

    def repair() -> None:
        if recovery_records is None or stack.workflows is None:
            return
        for identity in recovery_records():
            # Each workflow reconciles missing dispatch and its pure joins; mutations are never replayed.
            try:
                execution = stack.chat_service.get_execution(
                    identity.workspace_id, identity.execution_id
                )
                if execution.state in TERMINAL_EXECUTION_STATES:
                    continue
                document = executions.get(identity.execution_id)
                try:
                    stack.workflows.resume(document.identity, document.launch_request)
                except OwnershipLost:
                    raise
                except Exception as error:
                    logging.getLogger(__name__).exception(
                        "workflow recovery failed: %s", identity.execution_id
                    )
                    message = f"{type(error).__name__}: {str(error)[:2000]}"
                    payload = {
                        "activePhases": list(execution.active_phases),
                        "text": f"Phase recovery failed: {message}",
                        "recoveryError": message,
                    }
                    # Retain distinct failures without flooding the timeline on
                    # each repair pass. This evidence never completes a phase.
                    events = stack.chat_service.event_store.replay(
                        identity.execution_id
                    ).events
                    if any(
                        event.event_type == ExecutionEventType.WORKFLOW_PROGRESS
                        and event.intent_epoch == execution.intent_epoch
                        and event.payload == payload
                        for event in events
                    ):
                        continue
                    failure = ExecutionEvent(
                        execution_id=identity.execution_id,
                        workspace_id=identity.workspace_id,
                        event_type=ExecutionEventType.WORKFLOW_PROGRESS,
                        correlation_id=identity.execution_id,
                        actor_id="runtime-worker",
                        actor_scope=ActorScope.WORKFLOW,
                        occurred_at=datetime.now(UTC),
                        payload=payload,
                        intent_epoch=execution.intent_epoch,
                    )
                else:
                    continue
                # Resume owns its short acceptance guards; recovery never holds one across extension work.
                stack.chat_service.event_store.append(failure)
            except RecordNotFound:
                continue
            except OwnershipLost:
                continue
            except Exception:
                logging.getLogger(__name__).exception(
                    "could not inspect or persist workflow recovery: %s",
                    identity.execution_id,
                )

    return DurableWorker(
        work_store,
        ownership,
        {"conversation": conversation, "workflow": workflow},
        interrupted,
        repair=repair,
    )
