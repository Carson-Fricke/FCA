"""Coverage for the merged catalog, trusted context, and chat control tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.agent.runtime.context import ToolContext, ToolContextError
from thesis.agent.runtime.control import (
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
    ControlToolService,
)
from thesis.agent.runtime.control_tools import (
    CONTROL_NODE_TOOL_NAMES,
    ControlToolRuntime,
    control_registered_tools,
)
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.datum import DatumType
from thesis.agent.runtime.delivery import Anchor
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    DEFAULT_ROOT_BUDGET_CEILING,
    AnsweringActor,
    OperatorActorKind,
    RolePolicySpec,
    SpawnAuthorization,
    SpawnDenial,
    SpawnPolicy,
)
from thesis.agent.runtime.registry import THESIS_TOOL_NAMES, build_session_registry
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.workflow_tools import (
    WORKFLOW_TOOL_NAMES,
    WorkflowLauncher,
    workflow_registered_tools,
)
from thesis.agent.session_reply import SessionReplyProvider
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    SubAgentKind,
    SubAgentStatus,
    TranscriptItemKind,
)
from thesis.chat.responder import ReplyChunk, ToolResultChunk
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.chat.streaming import InMemoryChatStreamBroker
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, ToolCall
from thesis.workflows.core.registry import WorkflowRegistration, WorkflowRegistry
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.registry import register_hypothesis_development

_WORKSPACE = "ws-test"
_CHAT = "chat-1"
_ACTOR = "operator-1"


def _context(
    *,
    allowed: Sequence[str] | None = None,
    sub_agent_id: str | None = None,
    workflow_execution_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        actor_id=_ACTOR,
        sub_agent_id=sub_agent_id,
        workflow_execution_id=workflow_execution_id,
        allowed_tools=allowed,
    )


class _HarnessProvider:
    """Minimal harness provider publishing a fixed tool catalog."""

    def __init__(self, names: Sequence[str]) -> None:
        self._names = tuple(names)
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def list_tools(self) -> tuple[HarnessToolSpec, ...]:
        return tuple(
            HarnessToolSpec(
                name=name, description=name, input_schema={"type": "object"}
            )
            for name in self._names
        )

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        payload = dict(arguments or {})
        self.calls.append((name, payload))
        return HarnessToolResult(
            tool=name, ok=True, structured={"echo": payload}, text="ok"
        )


class _AsDict:
    def __init__(self, data: Mapping[str, object]) -> None:
        self._data = dict(data)

    def as_dict(self) -> dict[str, object]:
        return dict(self._data)


class _ControlStub:
    """Duck-typed control runtime capturing trusted spawn attribution."""

    def __init__(self) -> None:
        self.spawns: list[dict[str, object]] = []

    def spawn(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        spec: SubAgentSpec,
        actor_id: str,
        conversation_id: str,
        prompt: str,
        parent_sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> _AsDict:
        self.spawns.append(
            {
                "workspace_id": workspace_id,
                "chat_id": chat_id,
                "spec": spec,
                "actor_id": actor_id,
                "conversation_id": conversation_id,
                "parent_sub_agent_id": parent_sub_agent_id,
                "idempotency_key": idempotency_key,
            }
        )
        return _AsDict({"sub_agent_id": "sa-1", "status": "pending"})

    def await_sub_agent(self, workspace_id: str, sub_agent_id: str) -> _AsDict:
        return _AsDict(
            {"sub_agent_id": sub_agent_id, "status": "running", "terminal": False}
        )

    def request_operator_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        message: str,
        reason: str = "",
    ) -> _AsDict:
        return _AsDict(
            {"subAgentId": sub_agent_id, "question": message, "reason": reason}
        )


class _RecordingRegistry(WorkflowRegistry):
    """A registry that records the ids the tool resolved."""

    def __init__(self) -> None:
        super().__init__()
        self.resolved: list[str] = []

    def resolve(self, workflow_id: str) -> WorkflowRegistration[Any, Any, Any]:
        self.resolved.append(workflow_id)
        return super().resolve(workflow_id)


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._registry = _RecordingRegistry()
        register_hypothesis_development(self._registry, execute=self._execute)

    @property
    def workflows(self) -> WorkflowRegistry:
        return self._registry

    @property
    def resolved(self) -> list[str]:
        return self._registry.resolved

    def _execute(
        self,
        context: WorkflowContext,
        *,
        ideas: str,
        investigation_id: str | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "workspace_id": context.workspace_id,
                "chat_id": context.chat_id,
                "actor_id": context.actor_id,
                "ideas": ideas,
                "investigation_id": investigation_id,
                "trigger_message_id": context.trigger_message_id,
                "anchor_sub_agent_id": context.anchor_sub_agent_id,
            }
        )
        return {"executionId": "exec-1", "phase": "requested", "summary": "started"}


def _tool_names(registry: SessionToolRegistry) -> set[str]:
    names: set[str] = set()
    for spec in registry.catalog():
        function = spec.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def test_tool_context_rejects_blank_identity_and_sorts_policy() -> None:
    with pytest.raises(ToolContextError):
        ToolContext(
            workspace_id=" ",
            chat_id=_CHAT,
            conversation_id=_CHAT,
            actor_id=_ACTOR,
        )
    context = _context(allowed=("b", "a", "a"))
    assert context.allowed_tools == ("a", "b")
    assert context.permits("a")
    assert not context.permits("c")


def test_registry_merges_harness_control_and_workflow_tools_for_a_sub_agent() -> None:
    registry = build_session_registry(
        context=_context(sub_agent_id="sa-1"),
        harness_tools=cast(HarnessToolProvider, _HarnessProvider(("run_backtest",))),
        control=cast(ControlToolRuntime, _ControlStub()),
        workflow=cast(WorkflowLauncher, _Launcher()),
    )

    names = _tool_names(registry)
    assert "run_backtest" in names
    # A chat-spawned sub-agent outside a workflow keeps the launcher.
    assert set(THESIS_TOOL_NAMES) <= names


def test_registry_hides_the_workflow_launcher_from_workflow_agents() -> None:
    registry = build_session_registry(
        context=_context(sub_agent_id="sa-1", workflow_execution_id="exec-1"),
        control=cast(ControlToolRuntime, _ControlStub()),
        workflow=cast(WorkflowLauncher, _Launcher()),
    )

    assert set(WORKFLOW_TOOL_NAMES).isdisjoint(_tool_names(registry))


def test_registry_advertises_the_workflow_launcher_at_the_top_level() -> None:
    registry = build_session_registry(
        context=_context(),
        control=cast(ControlToolRuntime, _ControlStub()),
        workflow=cast(WorkflowLauncher, _Launcher()),
    )

    assert set(WORKFLOW_TOOL_NAMES) <= _tool_names(registry)


def test_completion_tools_are_offered_only_to_node_scopes() -> None:
    root = {
        tool.name
        for tool in control_registered_tools(
            cast(ControlToolRuntime, _ControlStub()), _context()
        )
    }
    node = {
        tool.name
        for tool in control_registered_tools(
            cast(ControlToolRuntime, _ControlStub()),
            _context(sub_agent_id="sa-1"),
        )
    }

    assert set(CONTROL_NODE_TOOL_NAMES).isdisjoint(root)
    assert set(CONTROL_NODE_TOOL_NAMES) <= node


def test_registry_advertises_exactly_the_configured_tool_policy() -> None:
    policy = (
        "validate_study",
        "request_workflow_input",
        "request_phase_completion",
    )
    registry = build_session_registry(
        context=_context(
            sub_agent_id="sa-1", workflow_execution_id="exec-1", allowed=policy
        ),
        harness_tools=cast(
            HarnessToolProvider,
            _HarnessProvider(
                (
                    "run_backtest",
                    "run_study",
                    "validate_study",
                    "backtest_config_template",
                )
            ),
        ),
        control=cast(ControlToolRuntime, _ControlStub()),
        workflow=cast(WorkflowLauncher, _Launcher()),
    )

    # The catalog is the configured policy, not the registered catalog: only
    # the declared drafting and phase-control tools are advertised.
    assert _tool_names(registry) == set(policy)


def test_registry_with_an_explicit_empty_policy_advertises_nothing() -> None:
    registry = build_session_registry(
        context=_context(sub_agent_id="sa-1", allowed=()),
        harness_tools=cast(HarnessToolProvider, _HarnessProvider(("run_backtest",))),
        control=cast(ControlToolRuntime, _ControlStub()),
    )

    assert _tool_names(registry) == set()
    denied = registry.dispatch("run_backtest", {})
    assert denied.ok is False
    assert denied.failure is not None
    assert denied.failure["kind"] == "denied"


def test_registry_dispatch_validates_arguments_against_the_tool_schema() -> None:
    calls: list[Mapping[str, object]] = []

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        calls.append(arguments)
        return ToolExecution(ok=True, text="ran")

    registry = SessionToolRegistry(
        [
            RegisteredTool(
                spec={
                    "type": "function",
                    "function": {
                        "name": "validated_tool",
                        "description": "test",
                        "parameters": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["model"],
                            "properties": {
                                "model": {
                                    "type": "object",
                                    "required": ["source"],
                                    "properties": {
                                        "source": {"type": "string", "minLength": 1}
                                    },
                                }
                            },
                        },
                    },
                },
                handler=handler,
            )
        ]
    )

    rejected = registry.dispatch("validated_tool", {"config": {"hypothesis": {}}})
    assert rejected.ok is False
    assert rejected.failure is not None
    assert rejected.failure["kind"] == "invalid_arguments"
    message = str(rejected.failure["message"])
    assert "model: required property is missing" in message
    assert "config: unexpected property" in message
    assert calls == []

    accepted = registry.dispatch(
        "validated_tool", {"model": {"source": "use harness_strategy_sdk;"}}
    )
    assert accepted.ok is True
    assert calls == [{"model": {"source": "use harness_strategy_sdk;"}}]


def test_registry_policy_denies_tools_outside_the_policy() -> None:
    registry = build_session_registry(
        context=_context(allowed=("run_backtest",)),
        harness_tools=cast(
            HarnessToolProvider, _HarnessProvider(("run_backtest", "run_study"))
        ),
    )

    assert isinstance(registry, SessionToolRegistry)
    advertised = _tool_names(registry)
    assert advertised == {"run_backtest"}
    denied = registry.dispatch("run_study", {})
    assert denied.ok is False
    assert denied.failure is not None
    assert denied.failure["kind"] == "denied"


def test_workflow_tool_launches_with_trusted_context() -> None:
    launcher = _Launcher()
    (tool,) = workflow_registered_tools(cast(WorkflowLauncher, launcher), _context())

    execution = tool.handler(
        {"workflow": "hypothesis-development", "idea": "MSTR leads IBIT"}
    )

    assert execution.ok is True
    assert execution.structured["executionId"] == "exec-1"
    assert launcher.calls[0]["workspace_id"] == _WORKSPACE
    assert launcher.calls[0]["chat_id"] == _CHAT
    assert launcher.calls[0]["actor_id"] == _ACTOR
    assert launcher.calls[0]["ideas"] == "MSTR leads IBIT"


def test_workflow_tool_resolves_the_workflow_through_the_registry() -> None:
    """Dispatch is a registry lookup, not an id equality or named method."""
    launcher = _Launcher()
    (tool,) = workflow_registered_tools(cast(WorkflowLauncher, launcher), _context())

    execution = tool.handler(
        {"workflow": "hypothesis-development", "idea": "MSTR leads IBIT"}
    )

    assert execution.ok is True
    assert launcher.resolved == ["hypothesis-development"]
    assert launcher.workflows.ids() == ("hypothesis-development",)


def test_workflow_tool_anchors_on_the_requesting_message() -> None:
    launcher = _Launcher()
    context = ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        actor_id=_ACTOR,
        trigger_message_id="message-42",
    )
    (tool,) = workflow_registered_tools(cast(WorkflowLauncher, launcher), context)

    execution = tool.handler(
        {"workflow": "hypothesis-development", "idea": "MSTR leads IBIT"}
    )

    assert execution.ok is True
    assert launcher.calls[0]["trigger_message_id"] == "message-42"


def test_workflow_tool_dispatches_from_a_chat_spawned_sub_agent() -> None:
    launcher = _Launcher()
    (tool,) = workflow_registered_tools(
        cast(WorkflowLauncher, launcher), _context(sub_agent_id="sa-1")
    )

    execution = tool.handler(
        {"workflow": "hypothesis-development", "idea": "MSTR leads IBIT"}
    )

    assert execution.ok is True
    assert launcher.calls[0]["chat_id"] == _CHAT
    assert launcher.calls[0]["ideas"] == "MSTR leads IBIT"


def test_workflow_tool_rejects_an_unknown_workflow() -> None:
    (tool,) = workflow_registered_tools(cast(WorkflowLauncher, _Launcher()), _context())

    execution = tool.handler({"workflow": "not-a-workflow", "idea": "x"})

    assert execution.ok is False
    assert execution.failure is not None
    assert "unknown workflow" in str(execution.failure["message"])


def test_phase_completion_tool_advertises_the_declared_schema() -> None:
    control = _ControlStub()

    def _mapping(value: object) -> Mapping[str, object]:
        assert isinstance(value, Mapping)
        return value

    def registered(schema: object) -> Mapping[str, object]:
        tools = {
            tool.name: tool.spec
            for tool in control_registered_tools(
                cast(ControlToolRuntime, control),
                _context(sub_agent_id="phase-1"),
                phase_completion_schema=cast("Mapping[str, object] | None", schema),
            )
        }
        function = tools["request_phase_completion"]["function"]
        return _mapping(_mapping(function)["parameters"])

    default = registered(None)
    assert _mapping(default["properties"])["completion"] == {"type": "object"}

    advertised = registered(
        {
            "type": "object",
            "properties": {"hypothesis": {"type": "object"}},
            "required": ["hypothesis"],
        }
    )
    assert advertised["required"] == ["completion"]
    completion = _mapping(_mapping(advertised["properties"])["completion"])
    assert completion["required"] == ["hypothesis"]
    assert _mapping(completion["properties"])["hypothesis"] == {"type": "object"}


def test_spawn_uses_trusted_parent_not_model_arguments() -> None:
    control = _ControlStub()
    context = ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        actor_id=_ACTOR,
        sub_agent_id="parent-1",
        allowed_tools=("spawn_sub_agent",),
    )
    tools = {
        tool.name: tool
        for tool in control_registered_tools(cast(ControlToolRuntime, control), context)
    }

    execution = tools["spawn_sub_agent"].handler(
        {
            "role": "research",
            "kind": "one_shot",
            "task": "evaluate the sample",
            "allowed_tools": ["run_study"],
            "delegated_budget": 3,
            "parent_sub_agent_id": "attacker-controlled",
            "workspace_id": "attacker-workspace",
        }
    )

    assert execution.ok is True
    spawn = control.spawns[0]
    assert spawn["parent_sub_agent_id"] == "parent-1"
    assert spawn["workspace_id"] == _WORKSPACE
    assert spawn["idempotency_key"] is None
    assert isinstance(spawn["spec"], SubAgentSpec)
    assert spawn["spec"].delegated_budget == 3

    # The handle datum carries the child's identity and delegated task so the
    # caller's transcript can render the spawn card without a second lookup.
    assert execution.structured["sub_agent_id"] == "sa-1"
    assert execution.structured["role"] == "research"
    assert execution.structured["task"] == "evaluate the sample"


def test_spawn_policy_never_widens_the_callers_tools() -> None:
    decision = SpawnPolicy().authorize(
        requested_role="research",
        requested_tools=("run_backtest", "launch_trader"),
        requested_budget=1,
        caller_tools=("run_backtest",),
        parent_sub_agent_id="parent-1",
    )

    assert isinstance(decision, SpawnAuthorization)
    assert decision.allowed_tools == ("run_backtest",)
    assert decision.dropped_tools == ("launch_trader",)


def test_spawn_policy_caps_a_root_budget_by_the_ceiling() -> None:
    decision = SpawnPolicy().authorize(
        requested_role="research",
        requested_tools=(),
        requested_budget=99_999,
        caller_tools=("run_backtest",),
        parent_sub_agent_id=None,
    )

    assert isinstance(decision, SpawnAuthorization)
    assert decision.delegated_budget == DEFAULT_ROOT_BUDGET_CEILING
    assert decision.allowed_tools == ("run_backtest",)


def test_spawn_policy_narrows_a_declared_role() -> None:
    policy = SpawnPolicy(
        roles=(RolePolicySpec(role="critique", allowed_tools=("get_run",)),)
    )

    decision = policy.authorize(
        requested_role="critique",
        requested_tools=("get_run", "run_backtest"),
        requested_budget=0,
        caller_tools=("get_run", "run_backtest"),
        parent_sub_agent_id="parent-1",
    )

    assert isinstance(decision, SpawnAuthorization)
    assert decision.allowed_tools == ("get_run",)


def test_spawn_policy_can_close_roots_and_unknown_roles() -> None:
    closed = SpawnPolicy(allow_root_spawns=False, allow_unknown_roles=False)

    root = closed.authorize(
        requested_role="research",
        requested_tools=(),
        requested_budget=0,
        caller_tools=("run_backtest",),
        parent_sub_agent_id=None,
    )
    unknown = closed.authorize(
        requested_role="research",
        requested_tools=(),
        requested_budget=0,
        caller_tools=("run_backtest",),
        parent_sub_agent_id="parent-1",
    )

    assert isinstance(root, SpawnDenial)
    assert root.code == "root_spawn_denied"
    assert isinstance(unknown, SpawnDenial)
    assert unknown.code == "unknown_role"


def test_spawn_tool_applies_the_policy_to_the_child_spec() -> None:
    control = _ControlStub()
    tools = {
        tool.name: tool
        for tool in control_registered_tools(
            cast(ControlToolRuntime, control),
            _context(allowed=("spawn_sub_agent", "run_backtest")),
            policy=SpawnPolicy(),
        )
    }

    execution = tools["spawn_sub_agent"].handler(
        {
            "role": "research",
            "task": "evaluate",
            "allowed_tools": ["run_backtest", "launch_trader"],
            "delegated_budget": 99_999,
        }
    )

    assert execution.ok is True
    spec = control.spawns[0]["spec"]
    assert isinstance(spec, SubAgentSpec)
    assert spec.allowed_tools == ("run_backtest",)
    assert spec.delegated_budget == DEFAULT_ROOT_BUDGET_CEILING
    authorization = execution.structured["authorization"]
    assert isinstance(authorization, Mapping)
    assert authorization["droppedTools"] == ["launch_trader"]


def test_top_level_operator_question_renders_as_the_tool_result_only() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat.create_message(_WORKSPACE, thread.chat_id, actor_id=_ACTOR, body="go")
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    service = ControlToolService(
        conversations=Conversations(chat=chat, store=store, lifecycle=lifecycle)
    )
    registry = build_session_registry(
        context=_context(allowed=("request_operator_input",)),
        control=service,
    )
    provider = _FakeProvider(
        [
            _completion(
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="request_operator_input",
                        arguments=(
                            '{"message": "Which investigation should I link?", '
                            '"reason": "ambiguous"}'
                        ),
                    ),
                )
            ),
        ]
    )

    def reply_source(workspace_id: str, chat_id: str, history: Sequence[ChatMessage]):
        return SessionReplyProvider(
            workspace_id=workspace_id,
            chat_id=chat_id,
            conversation_id=chat_id,
            provider=provider,
            registry=registry,
        )(list(history))

    conversations = Conversations(
        chat=chat,
        broker=InMemoryChatStreamBroker(),
        reply_for=reply_source,
    )

    conversations.root(_WORKSPACE, thread.chat_id).run_turn()

    # The operator question renders as the request_operator_input tool result
    # at the root anchor; no operator-question chat message is ever written.
    stored = chat.list_messages(_WORKSPACE, thread.chat_id)
    tool_messages = [message for message in stored if message.role is MessageRole.TOOL]
    assert len(tool_messages) == 1
    tool_result = tool_messages[0].tool_result
    assert tool_result is not None
    assert tool_result["datumType"] == DatumType.OPERATOR_QUESTION.value
    assert tool_result["structured"] == {
        "subAgentId": None,
        "question": "Which investigation should I link?",
        "reason": "ambiguous",
    }
    assert all(message.role is not MessageRole.ASSISTANT for message in stored)
    assert not any(
        message.data is not None and message.data.get("kind") == "operator_question"
        for message in stored
    )


def test_sub_agent_completion_tools_deliver_the_result_to_the_caller() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat_id = thread.chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    parent = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="lead", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
    )
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", kind=SubAgentKind.CONVERSATION),
        actor_id=_ACTOR,
        parent_sub_agent_id=parent.sub_agent_id,
    )
    scheduled: list[str] = []
    conversations = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        spawn_thread=lambda target: scheduled.append("woke"),
    )
    service = ControlToolService(conversations=conversations)
    context = ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=chat_id,
        conversation_id=child.sub_agent_id,
        actor_id=_ACTOR,
        sub_agent_id=child.sub_agent_id,
        parent_sub_agent_id=parent.sub_agent_id,
        allowed_tools=CONTROL_NODE_TOOL_NAMES,
    )
    tools = {tool.name: tool for tool in control_registered_tools(service, context)}
    assert TOOL_REQUEST_SUB_AGENT_COMPLETION in tools
    assert TOOL_REQUEST_PHASE_COMPLETION in tools

    execution = tools[TOOL_REQUEST_SUB_AGENT_COMPLETION].handler(
        {
            "completion": {"verdict": "supported"},
            # A model argument can never redirect the completion to another node.
            "sub_agent_id": parent.sub_agent_id,
        }
    )

    assert execution.ok is True
    assert execution.terminal is True
    assert execution.structured["subAgentId"] == child.sub_agent_id
    completed = lifecycle.get(_WORKSPACE, child.sub_agent_id)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"verdict": "supported", "intentEpoch": 0}
    caller_still_live = lifecycle.get(_WORKSPACE, parent.sub_agent_id)
    assert caller_still_live is not None
    assert caller_still_live.status is SubAgentStatus.PENDING

    delivered = [
        message
        for message in chat.list_messages(_WORKSPACE, chat_id)
        if message.data is not None
        and message.data.get("childSubAgentId") == child.sub_agent_id
    ]
    assert len(delivered) == 1
    assert delivered[0].data is not None
    assert delivered[0].data["subAgentId"] == parent.sub_agent_id
    assert delivered[0].data["result"] == {"verdict": "supported", "intentEpoch": 0}
    # The result delivery woke the caller's conversation exactly once.
    assert scheduled == ["woke"]
    assert conversations.in_flight(_WORKSPACE, chat_id, parent.sub_agent_id) is True


def test_phase_completion_tool_marks_the_phase_node_terminal() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat_id = thread.chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="workflow-node", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    phase = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(
            role="falsification", kind=SubAgentKind.ONE_SHOT, phase="falsification"
        ),
        actor_id=_ACTOR,
        parent_sub_agent_id=node.sub_agent_id,
    )
    scheduled: list[str] = []
    conversations = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        spawn_thread=lambda target: scheduled.append("woke"),
    )
    service = ControlToolService(conversations=conversations)
    context = ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=chat_id,
        conversation_id=phase.sub_agent_id,
        actor_id=_ACTOR,
        sub_agent_id=phase.sub_agent_id,
        parent_sub_agent_id=node.sub_agent_id,
        allowed_tools=CONTROL_NODE_TOOL_NAMES,
    )
    tools = {tool.name: tool for tool in control_registered_tools(service, context)}

    execution = tools[TOOL_REQUEST_PHASE_COMPLETION].handler(
        {"completion": {"advance": True}}
    )

    assert execution.ok is True
    assert execution.terminal is True
    completed = lifecycle.get(_WORKSPACE, phase.sub_agent_id)
    assert completed is not None
    assert completed.status is SubAgentStatus.COMPLETED
    assert completed.result == {"advance": True, "intentEpoch": 0}
    # A phase agent's parent is the workflow node: the completion is recorded
    # there but never schedules a turn, because no agent owns that record.
    assert scheduled == []
    assert conversations.in_flight(_WORKSPACE, chat_id, node.sub_agent_id) is False


def test_a_paused_child_stays_for_the_operator_answer() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat_id = thread.chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    holder: list[Conversations] = []

    def registry_factory(context: ToolContext):
        return build_session_registry(
            context=context,
            control=ControlToolService(conversations=holder[0]),
        )

    conversations = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        provider=_FakeProvider(
            [
                _completion(
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="request_operator_input",
                            arguments='{"message": "Approve the model?"}',
                        ),
                    )
                )
            ]
        ),
        registry_factory=registry_factory,
        spawn_thread=lambda target: None,
    )
    holder.append(conversations)
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(
            role="research",
            kind=SubAgentKind.ONE_SHOT,
            allowed_tools=("request_operator_input",),
        ),
        "do the thing",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    child.run_turn()

    paused = child.record
    assert paused is not None
    assert paused.status is SubAgentStatus.NEEDS_INPUT
    assert paused.accepts_input is True
    assert not any(
        message.data is not None and message.data.get("kind") == "child_result"
        for message in chat.list_messages(_WORKSPACE, chat_id)
    )

    inputs = conversations.answer(
        _WORKSPACE,
        chat_id,
        child.conversation_id,
        body="approved",
        actor=AnsweringActor(actor_id=_ACTOR, kind=OperatorActorKind.HUMAN_OPERATOR),
    )

    assert inputs.duplicate is False
    resumed = lifecycle.get(_WORKSPACE, child.conversation_id)
    assert resumed is not None
    assert resumed.status is SubAgentStatus.RUNNING


class _FakeProvider:
    def __init__(self, completions: Sequence[ModelCompletion]) -> None:
        self._completions = list(completions)

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: str = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del messages, role, tools, response_format
        return self._completions.pop(0)


def _completion(
    *, content: str = "", tool_calls: tuple[ToolCall, ...] = ()
) -> ModelCompletion:
    return ModelCompletion(
        content=content,
        model="test-model",
        provider="test",
        role="primary",
        prompt_tokens=1,
        completion_tokens=1,
        tool_calls=tool_calls,
    )


def test_session_reply_projects_text_and_tool_chunks() -> None:
    provider = _FakeProvider(
        [
            _completion(tool_calls=(ToolCall(id="c1", name="probe", arguments="{}"),)),
            _completion(content="final answer"),
        ]
    )
    from thesis.agent.runtime.session import RegisteredTool, ToolExecution

    registry = SessionToolRegistry(
        [
            RegisteredTool(
                spec={
                    "type": "function",
                    "function": {"name": "probe", "description": "p", "parameters": {}},
                },
                handler=lambda arguments: ToolExecution(
                    ok=True, structured={"x": 1}, text="ok"
                ),
            )
        ]
    )
    reply = SessionReplyProvider(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        provider=provider,
        registry=registry,
        chunk_size=64,
    )

    chunks = list(reply(()))

    tool_chunks = [chunk for chunk in chunks if isinstance(chunk, ToolResultChunk)]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool == "probe"
    text = "".join(
        chunk if isinstance(chunk, str) else chunk.text
        for chunk in chunks
        if isinstance(chunk, (str, ReplyChunk))
    )
    assert "final answer" in text


def test_spawned_child_completes_and_delivers_to_the_chat() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat_id = thread.chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    woke: list[object] = []
    conversations = Conversations(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        provider=_FakeProvider([_completion(content="child done")]),
        registry_factory=lambda context: SessionToolRegistry(),
        spawn_thread=lambda target: woke.append(target),
    )
    child = conversations.spawn(
        Anchor(_WORKSPACE, chat_id),
        SubAgentSpec(role="research", allowed_tools=("run_study",)),
        "do the thing",
        SubAgentKind.ONE_SHOT,
        actor_id=_ACTOR,
    )

    child.run_turn()

    terminal = child.record
    assert terminal is not None
    assert terminal.status.value == "completed"
    assert terminal.result == {"summary": "child done"}
    messages = chat.list_messages(_WORKSPACE, chat_id)
    assert any(
        message.role is MessageRole.TOOL
        and message.kind is TranscriptItemKind.SUB_AGENT
        for message in messages
    )
    # The prompt delivery woke the child, and its delivered result woke the
    # root conversation the child returns to.
    assert len(woke) == 2
    assert conversations.in_flight(_WORKSPACE, chat_id, "") is True
