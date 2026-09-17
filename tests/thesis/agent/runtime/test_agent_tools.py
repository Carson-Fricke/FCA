"""Coverage for the merged catalog, trusted context, and chat control tools."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import pytest

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.agent.runtime.child_runner import ChildRunner
from thesis.agent.runtime.context import ToolContext, ToolContextError
from thesis.agent.runtime.control_tools import (
    ControlToolRuntime,
    control_registered_tools,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    DEFAULT_ROOT_BUDGET_CEILING,
    RolePolicySpec,
    SpawnAuthorization,
    SpawnDenial,
    SpawnPolicy,
)
from thesis.agent.runtime.registry import THESIS_TOOL_NAMES, build_session_registry
from thesis.agent.runtime.session import SessionToolRegistry
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import InMemorySubAgentStore
from thesis.agent.runtime.workflow_tools import (
    WorkflowLauncher,
    workflow_registered_tools,
)
from thesis.agent.session_reply import SessionReplyProvider
from thesis.chat.contracts import MessageRole, TranscriptItemKind
from thesis.chat.responder import ReplyChunk, ToolResultChunk
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, ToolCall

_WORKSPACE = "ws-test"
_CHAT = "chat-1"
_ACTOR = "operator-1"


def _context(*, allowed: Sequence[str] = ()) -> ToolContext:
    return ToolContext(
        workspace_id=_WORKSPACE,
        chat_id=_CHAT,
        conversation_id=_CHAT,
        actor_id=_ACTOR,
        allowed_tools=tuple(allowed),
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


class _Launcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def start_hypothesis_development(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "chat_id": chat_id,
                "actor_id": actor_id,
                "ideas": ideas,
                "investigation_id": investigation_id,
                "trigger_message_id": trigger_message_id,
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


def test_registry_merges_harness_control_and_workflow_tools() -> None:
    registry = build_session_registry(
        context=_context(),
        harness_tools=cast(HarnessToolProvider, _HarnessProvider(("run_backtest",))),
        control=cast(ControlToolRuntime, _ControlStub()),
        workflow=cast(WorkflowLauncher, _Launcher()),
    )

    names = _tool_names(registry)
    assert "run_backtest" in names
    assert set(THESIS_TOOL_NAMES) <= names


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


def test_workflow_tool_rejects_an_unknown_workflow() -> None:
    (tool,) = workflow_registered_tools(cast(WorkflowLauncher, _Launcher()), _context())

    execution = tool.handler({"workflow": "not-a-workflow", "idea": "x"})

    assert execution.ok is False
    assert execution.failure is not None
    assert "unknown workflow" in str(execution.failure["message"])


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


def test_spawn_policy_is_permissive_by_default() -> None:
    decision = SpawnPolicy().authorize(
        requested_role="anything-goes",
        requested_tools=("run_backtest", "run_study"),
        requested_budget=7,
        caller_tools=("run_backtest", "run_study", "run_sweep"),
        parent_sub_agent_id=None,
    )

    assert isinstance(decision, SpawnAuthorization)
    assert decision.role == "anything-goes"
    assert decision.allowed_tools == ("run_backtest", "run_study")
    assert decision.delegated_budget == 7
    assert decision.dropped_tools == ()


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


def test_top_level_chat_can_request_operator_input() -> None:
    published: list[tuple[str, str, str, str]] = []
    tools = {
        tool.name: tool
        for tool in control_registered_tools(
            cast(ControlToolRuntime, _ControlStub()),
            _context(allowed=("request_operator_input",)),
            publish_operator_question=lambda workspace, chat, message, reason: (
                published.append((workspace, chat, message, reason))
                or {"messageId": "message-1", "kind": "operator_question"}
            ),
        )
    }

    execution = tools["request_operator_input"].handler(
        {"message": "Which investigation should I link?", "reason": "ambiguous"}
    )

    assert execution.ok is True
    assert execution.terminal is True
    assert published == [
        (_WORKSPACE, _CHAT, "Which investigation should I link?", "ambiguous")
    ]


def test_top_level_chat_without_a_question_surface_fails_closed() -> None:
    tools = {
        tool.name: tool
        for tool in control_registered_tools(
            cast(ControlToolRuntime, _ControlStub()),
            _context(allowed=("request_operator_input",)),
        )
    }

    execution = tools["request_operator_input"].handler({"message": "hello?"})

    assert execution.ok is False
    assert execution.failure is not None
    assert execution.failure["kind"] == "tool"


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


def test_child_runner_completes_and_delivers_to_the_chat() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    chat_id = thread.chat_id
    store = InMemorySubAgentStore()
    lifecycle = SubAgentLifecycle(chat, store=store)
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="research", allowed_tools=("run_study",)),
        actor_id=_ACTOR,
    )
    scheduled: list[tuple[str, str, str]] = []
    runner = ChildRunner(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        provider=_FakeProvider([_completion(content="child done")]),
        registry_factory=lambda context: SessionToolRegistry(),
        schedule=lambda workspace, chat_id, sub_agent_id: scheduled.append(
            (workspace, chat_id, sub_agent_id)
        ),
    )

    terminal = runner.run(child, "do the thing")

    assert terminal.status.value == "completed"
    assert terminal.result == {"summary": "child done"}
    messages = chat.list_messages(_WORKSPACE, chat_id)
    assert any(
        message.role is MessageRole.TOOL
        and message.kind is TranscriptItemKind.SUB_AGENT
        for message in messages
    )
    assert scheduled == [(_WORKSPACE, chat_id, "")]


def test_child_runner_anchors_the_trigger_on_its_own_task_message() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    thread = chat.create_chat(_WORKSPACE, title="Test chat", owner_actor_id=_ACTOR)
    store = InMemorySubAgentStore()
    lifecycle = SubAgentLifecycle(chat, store=store)
    child = lifecycle.spawn(
        _WORKSPACE,
        thread.chat_id,
        spec=SubAgentSpec(role="research", allowed_tools=("start_workflow",)),
        actor_id=_ACTOR,
    )
    contexts: list[ToolContext] = []

    def registry_factory(context: ToolContext) -> SessionToolRegistry:
        contexts.append(context)
        return SessionToolRegistry()

    runner = ChildRunner(
        chat=chat,
        store=store,
        lifecycle=lifecycle,
        provider=_FakeProvider([_completion(content="done")]),
        registry_factory=registry_factory,
    )

    runner.run(child, "develop this idea")

    task_message = next(
        message
        for message in chat.list_messages(_WORKSPACE, thread.chat_id)
        if message.data is not None and message.data.get("kind") == "child_task"
    )
    assert task_message.role is MessageRole.USER
    assert contexts[0].sub_agent_id == child.sub_agent_id
    assert contexts[0].trigger_message_id == task_message.message_id
