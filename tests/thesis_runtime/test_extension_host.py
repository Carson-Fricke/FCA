"""An unrelated workflow and hosted tool run through the real generic host."""

from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolRequest,
)
from thesis_runtime.contracts.records import WorkflowDefinition
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.sessions.session import AgentSession
from thesis_runtime.tools.registry import SessionToolRegistry
from thesis_runtime.tools.remote import hosted_tool
from thesis_runtime.tools.workflows import workflow_registered_tools
from thesis_runtime.workflows.builder import WorkflowBuilder
from thesis_runtime.workflows.execution_store import InMemoryExecutionStore
from thesis_runtime.workflows.phase import DeterministicPhase, TerminalPhase
from thesis_runtime.workflows.registry import (
    UnknownWorkflowError,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis_runtime.workflows.workflow import WorkflowDoctrine, WorkflowPorts


class DocumentExtension:
    def invoke(
        self, operation: str, payload: Mapping[str, Any]
    ) -> Mapping[str, object]:
        assert payload["protocolVersion"] == 2
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
                "document": payload["inputs"]["document"],
            }
        if operation == "state":
            history = payload["history"]
            return {
                "phase": history[-1]["route"] if history else "render",
                "intentEpoch": 0,
            }
        if operation == "validate":
            return {"failure": None}
        if operation == "seed":
            return {"text": None}
        if operation == "execute":
            return {
                "completion": {
                    "target": "published",
                    "document": payload["request"]["document"],
                }
            }
        if operation == "report":
            return {
                "artifact": {"document": payload["request"]["document"]},
                "summary": "Rendered",
            }
        raise AssertionError(operation)


def test_unrelated_workflow_launch_completion_replay_and_version_pinned_recovery() -> (
    None
):
    chat, executions = ChatService(), InMemoryExecutionStore()
    thread = chat.create_chat("workspace", title="Documents", owner_actor_id="operator")
    trigger = chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Publish"
    )

    def now():
        return datetime(2026, 1, 1, tzinfo=UTC)

    resumed = []
    registry = WorkflowRegistry()
    workflows = {}
    for version in ("2.0.0",):
        definition = WorkflowDefinition(
            workflow_id="documents",
            version=version,
            title="Documents",
            phases=("render", "published"),
        )
        workflow = (
            WorkflowBuilder()
            .workflow("documents", version=version, title="Documents")
            .phase(DeterministicPhase(phase="render", contract="execute"))
            .phase(TerminalPhase(phase="published"))
            .routes({"render": {"published": ("published.input",)}, "published": {}})
            .doctrine(
                WorkflowDoctrine(
                    definition,
                    "render",
                    "publisher",
                    (),
                    frozenset(),
                    {},
                    {},
                    {},
                    "Publishing",
                    {},
                    DocumentExtension(),
                )
            )
            .ports(
                WorkflowPorts(
                    chat,
                    now,
                    lambda prefix, version=version: prefix + "-" + version,
                    execution_store=executions,
                    conversations=Conversations(chat=chat),
                )
            )
            .build()
        )
        workflows[version] = workflow
        registry.register(
            WorkflowRegistration(
                execute=lambda context, inputs, workflow=workflow: workflow.execute(
                    context, inputs=inputs
                ),
                workflow_factory=lambda workflow=workflow: workflow,
                launch_schema={
                    "type": "object",
                    "required": ["document"],
                    "properties": {"document": {"type": "string"}},
                    "additionalProperties": False,
                },
                resume=lambda identity, request, work, version=version: resumed.append(
                    version
                ),
            )
        )
    context = ToolContext(
        "workspace",
        thread.chat_id,
        thread.chat_id,
        "operator",
        trigger_message_id=trigger.message_id,
    )
    tools = SessionToolRegistry(
        workflow_registered_tools(SimpleNamespace(workflows=registry), context)
    )
    rejected = tools.dispatch(
        "start_workflow",
        {"workflow": "documents", "input": {"idea": "invalid"}},
        context=context,
    )
    assert not rejected.ok and chat.list_executions("workspace") == ()
    result = tools.dispatch(
        "start_workflow",
        {"workflow": "documents", "input": {"document": "report"}},
        context=context,
    )
    assert result.ok, result.failure
    document = executions.get(str(result.structured["executionId"]))
    workflow = workflows["2.0.0"]
    from thesis_runtime.workflows.extensions import RunRequest

    request = RunRequest.bind(
        chat.get_execution("workspace", document.execution_id), document.launch_request
    )
    assert workflow.prepare_phase(request) is None
    accepted = executions.get(document.execution_id)
    assert (
        len(accepted.transitions) == 1
        and accepted.transitions[0].completion["document"] == "report"
    )
    assert (
        chat.get_execution("workspace", document.execution_id).state.value
        == "completed"
    )
    registry.resume(accepted.identity, accepted.launch_request)
    assert resumed == ["2.0.0"]
    from dataclasses import replace

    with pytest.raises(UnknownWorkflowError, match="outdated"):
        registry.resume(
            replace(accepted.identity, workflow_version="1.0.0"),
            accepted.launch_request,
        )
    assert resumed == ["2.0.0"]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.resolve("documents"))
    with pytest.raises(UnknownWorkflowError):
        registry.resume(
            replace(accepted.identity, workflow_version="missing"),
            accepted.launch_request,
        )


@pytest.mark.parametrize("reply", ["success", "mismatch", "interruption"])
def test_hosted_tool_receives_trusted_context_and_never_replays_unknown_outcome(
    reply: str,
) -> None:
    calls = []

    class Transport:
        def invoke(self, request):
            calls.append(request)
            assert request["arguments"] == {"document": "report"}
            context = request["context"]
            assert (
                context["workspaceId"] == "trusted" and context["actorId"] == "operator"
            )
            assert context["idempotencyKey"] == context["toolInvocationId"]
            if reply == "interruption":
                raise TimeoutError("response lost after dispatch")
            return {
                "protocolVersion": 1,
                "invocationId": context["toolInvocationId"]
                if reply == "success"
                else "wrong",
                "outcome": {
                    "ok": True,
                    "structured": {"published": True},
                    "text": "Published",
                },
            }

    class Provider:
        def request_snapshot(self, **kwargs):
            return {
                "projection": "test",
                "messages": [message.as_dict() for message in kwargs["messages"]],
            }

        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        def complete(self, **kwargs):
            if not calls:
                return SessionMessage(
                    "request",
                    "outgoing",
                    MessageContent(
                        "Publish",
                        "agent",
                        requests=(
                            ToolRequest("call", "publish", '{"document":"report"}'),
                        ),
                    ),
                )
            return SessionMessage(
                "answer", "outgoing", MessageContent("Finished", "agent")
            )

    tool = hosted_tool(
        {
            "name": "publish",
            "parameters": {
                "type": "object",
                "properties": {"document": {"type": "string"}},
                "required": ["document"],
                "additionalProperties": False,
            },
        },
        Transport(),
        operation="write",
    )
    result = AgentSession(
        workspace_id="trusted",
        chat_id="chat",
        conversation_id="chat",
        actor_id="operator",
        provider=Provider(),
        registry=SessionToolRegistry([tool]),
    ).run((SessionMessage("input", "incoming", MessageContent("Publish", "user")),))
    assert result.status == "completed" and len(calls) == 1
    outcome = next(
        turn.message.content.result
        for turn in result.turns
        if turn.message.direction == "incoming"
    )
    assert outcome is not None
    assert outcome["ok"] is (reply == "success")
    if reply != "success":
        failure = outcome["failure"]
        assert isinstance(failure, Mapping)
        assert failure["certainty"] == "unknown_outcome"
        assert failure["retry_safe"] is False
