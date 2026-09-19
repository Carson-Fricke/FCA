"""Unified durable agent session: the model tool-calling turn loop.

One :class:`AgentSession` serves a top-level chat reply, a chat-spawned
sub-agent, and a workflow phase sub-agent.  It sends the merged tool catalog
(harness tools plus Thesis control tools) with every completion, persists each
model invocation and each tool call as a first-class durable record, threads
tool results back to the model as ``tool`` messages, and repeats until the model
answers in text, a terminal control tool is accepted, or the safety budget stops
the turn.

The session never decides a workflow transition: a terminal control tool returns
a constructed completion, and the owning deterministic state machine evaluates
it.  This is the single abstraction the specification requires the chat tool loop
and the workflow model path to converge on.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from thesis.agent.runtime.datum import Datum, normalize_datums
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, Role, ToolCall

#: How many model/tool round trips one session allows before forcing a stop.
DEFAULT_MAX_TOOL_ITERATIONS = 64


class CompletionProvider(Protocol):
    """The provider boundary a session completes against."""

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion: ...


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """The typed outcome of one dispatched tool call.

    ``datums`` carries the typed values the tool returned.  When it is empty the
    session derives one generic datum from ``structured``/``text``/``failure``,
    typed by the tool name, so every tool result is renderable.
    """

    ok: bool
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    failure: Mapping[str, object] | None = None
    terminal: bool = False
    datums: tuple[Datum, ...] = ()

    def as_content(self) -> str:
        payload: dict[str, object] = {
            "ok": self.ok,
            "structured": dict(self.structured),
            "text": self.text,
        }
        if self.failure is not None:
            payload["failure"] = dict(self.failure)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


ToolHandler = Callable[[Mapping[str, object]], ToolExecution]


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """One tool advertised to the model and its local handler."""

    spec: Mapping[str, object]
    handler: ToolHandler

    @property
    def name(self) -> str:
        function = self.spec.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str):
                return name
        raise ValueError("registered tool spec is missing a function name")


class SessionToolRegistry:
    """A default-deny catalog of tools available to one session.

    ``policy`` is the effective allowed-tool policy for the calling sub-agent.
    When supplied, only permitted tools are advertised and a request for any
    other tool is rejected before its handler runs; the registry still knows the
    full catalog so the same instance can serve several policies.
    """

    def __init__(
        self,
        tools: Sequence[RegisteredTool] = (),
        *,
        policy: Sequence[str] | None = None,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool)
        self._policy: frozenset[str] | None = (
            None if policy is None else frozenset(policy)
        )

    @property
    def policy(self) -> tuple[str, ...] | None:
        """Return the effective allowed-tool policy, or ``None`` for all."""
        if self._policy is None:
            return None
        return tuple(sorted(self._policy))

    def register(self, tool: RegisteredTool) -> None:
        self._tools[tool.name] = tool

    def permits(self, name: str) -> bool:
        """Return whether the effective policy allows ``name``."""
        return self._policy is None or name in self._policy

    def catalog(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            tool.spec for tool in self._tools.values() if self.permits(tool.name)
        )

    def dispatch(self, name: str, arguments: Mapping[str, object]) -> ToolExecution:
        tool = self._tools.get(name)
        if tool is None:
            return ToolExecution(
                ok=False,
                failure={"kind": "unknown_tool", "message": f"unknown tool: {name}"},
            )
        if not self.permits(name):
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "denied",
                    "message": f"tool is not permitted by the effective policy: {name}",
                    "tool": name,
                },
            )
        try:
            return tool.handler(arguments)
        except Exception as error:  # noqa: BLE001 - a tool fault is a normal turn
            return ToolExecution(
                ok=False,
                failure={"kind": "tool", "message": str(error) or type(error).__name__},
            )


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One durable turn of a session transcript."""

    role: str
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    invocation_id: str | None = None
    datums: tuple[Datum, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionResult:
    """The terminal result of one session run."""

    status: str
    content: str
    turns: tuple[SessionTurn, ...]
    completion: Mapping[str, object] | None = None

    @property
    def stopped_for_completion(self) -> bool:
        return self.status == "completed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _tool_arguments(call: ToolCall) -> Mapping[str, object] | None:
    raw = call.arguments.strip()
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


class AgentSession:
    """Run one durable model/tool turn loop for one conversation owner."""

    def __init__(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        provider: CompletionProvider,
        registry: SessionToolRegistry,
        store: SubAgentStore | None = None,
        sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        actor_id: str = "thesis-runtime",
        role: Role = "primary",
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if max_tool_iterations < 1:
            raise ValueError("max_tool_iterations must be positive")
        self._workspace_id = workspace_id
        self._chat_id = chat_id
        self._conversation_id = conversation_id
        self._provider = provider
        self._registry = registry
        self._store = store
        self._sub_agent_id = sub_agent_id
        self._workflow_execution_id = workflow_execution_id
        self._phase_attempt_id = phase_attempt_id
        self._actor_id = actor_id
        self._role = role
        self._max_tool_iterations = max_tool_iterations
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def run(
        self,
        messages: Sequence[ProviderChatMessage],
        *,
        before_model_call: Callable[[], Sequence[ProviderChatMessage]] | None = None,
    ) -> SessionResult:
        """Run to text completion, terminal control tool, or the iteration cap.

        ``before_model_call`` drains messages that arrived after the turn began
        into the live conversation between model iterations, so the model sees
        the latest inbound data without restarting the turn.  It may only
        return ``user``-role messages: the loop point is always after a tool
        result, so appending user turns there keeps tool-call pairing valid.
        """
        working = list(messages)
        turns: list[SessionTurn] = []
        catalog = self._registry.catalog()
        reasoning_parts: list[str] = []
        for _ in range(self._max_tool_iterations):
            if before_model_call is not None:
                fresh = [
                    message for message in before_model_call() if message.role == "user"
                ]
                if fresh:
                    working.extend(fresh)
            model_invocation_id = self._record_invocation(
                kind=AgentInvocationKind.MODEL,
                state=AgentInvocationState.STARTED,
                parent_invocation_id=None,
            )
            completion = self._provider.complete(
                messages=working,
                role=self._role,
                tools=catalog or None,
            )
            self._record_invocation(
                kind=AgentInvocationKind.MODEL,
                state=AgentInvocationState.SUCCEEDED,
                parent_invocation_id=None,
                invocation_id=model_invocation_id,
                output_hash=None,
            )
            if completion.reasoning:
                reasoning_parts.append(completion.reasoning)
            if not completion.tool_calls:
                turns.append(
                    SessionTurn(
                        role="assistant",
                        content=completion.content,
                        invocation_id=model_invocation_id,
                    )
                )
                return SessionResult(
                    status="completed",
                    content=completion.content,
                    turns=tuple(turns),
                )
            working.append(
                ProviderChatMessage(
                    role="assistant",
                    content=completion.content,
                    tool_calls=completion.tool_calls,
                )
            )
            turns.append(
                SessionTurn(
                    role="assistant",
                    content=completion.content,
                    invocation_id=model_invocation_id,
                    tool_calls=completion.tool_calls,
                )
            )
            for call in completion.tool_calls:
                arguments = _tool_arguments(call)
                tool_invocation_id = self._record_invocation(
                    kind=AgentInvocationKind.TOOL,
                    state=AgentInvocationState.STARTED,
                    parent_invocation_id=model_invocation_id,
                    tool_name=call.name,
                )
                if arguments is None:
                    execution = ToolExecution(
                        ok=False,
                        failure={
                            "kind": "tool",
                            "message": "tool arguments were not a JSON object",
                        },
                    )
                else:
                    execution = self._registry.dispatch(call.name, arguments)
                self._record_invocation(
                    kind=AgentInvocationKind.TOOL,
                    state=(
                        AgentInvocationState.SUCCEEDED
                        if execution.ok
                        else AgentInvocationState.FAILED
                    ),
                    parent_invocation_id=model_invocation_id,
                    tool_name=call.name,
                    invocation_id=tool_invocation_id,
                    failure=execution.failure,
                )
                working.append(
                    ProviderChatMessage(
                        role="tool",
                        content=execution.as_content(),
                        tool_call_id=call.id,
                    )
                )
                turns.append(
                    SessionTurn(
                        role="tool",
                        content=execution.as_content(),
                        tool_name=call.name,
                        tool_call_id=call.id,
                        invocation_id=tool_invocation_id,
                        datums=normalize_datums(
                            execution.datums,
                            fallback_type=call.name,
                            ok=execution.ok,
                            structured=execution.structured,
                            text=execution.text,
                            failure=execution.failure,
                        ),
                    )
                )
                if execution.terminal:
                    return SessionResult(
                        status="completed",
                        content="".join(reasoning_parts),
                        turns=tuple(turns),
                        completion=execution.structured,
                    )
        return SessionResult(
            status="iterations_exhausted",
            content="".join(reasoning_parts),
            turns=tuple(turns),
        )

    def _record_invocation(
        self,
        *,
        kind: AgentInvocationKind,
        state: AgentInvocationState,
        parent_invocation_id: str | None,
        tool_name: str | None = None,
        invocation_id: str | None = None,
        output_hash: str | None = None,
        failure: Mapping[str, object] | None = None,
    ) -> str:
        resolved_id = invocation_id or self._id_factory("invocation")
        if self._store is None:
            return resolved_id
        now = self._clock()
        invocation = AgentInvocation(
            workspace_id=self._workspace_id,
            created_at=now,
            created_by=self._actor_id,
            invocation_id=resolved_id,
            chat_id=self._chat_id,
            conversation_id=self._conversation_id,
            kind=kind,
            state=state,
            sub_agent_id=self._sub_agent_id,
            parent_invocation_id=parent_invocation_id,
            workflow_execution_id=self._workflow_execution_id,
            phase_attempt_id=self._phase_attempt_id,
            tool_name=tool_name,
            output_hash=output_hash,
            failure=None if failure is None else MappingProxyType(dict(failure)),
            updated_at=now,
        )
        self._store.save_invocation(invocation)
        return resolved_id


__all__ = [
    "AgentSession",
    "CompletionProvider",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "RegisteredTool",
    "SessionResult",
    "SessionToolRegistry",
    "SessionTurn",
    "ToolExecution",
    "ToolHandler",
]
