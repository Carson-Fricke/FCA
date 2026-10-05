"""Unified durable agent session: the model tool-calling turn loop.

One :class:`AgentSession` serves a top-level chat reply, a chat-spawned
sub-agent, and a workflow phase sub-agent.  It sends the merged tool catalog
(harness tools plus Thesis control tools) with every completion, persists each
model invocation and each tool call as a first-class durable record, threads
tool results back to the model as canonical incoming messages, and repeats until the model
answers in text, a terminal control tool is accepted, or the safety budget stops
the turn.

The session never decides a workflow transition: a terminal control tool returns
a constructed completion, and the owning deterministic state machine evaluates
it.  This is the single abstraction the specification requires the chat tool loop
and the workflow model path to converge on.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import monotonic
from types import MappingProxyType
from typing import Literal, Protocol

from thesis.agent.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
)
from thesis.agent.runtime.datum import Datum, normalize_datums
from thesis.agent.runtime.inbox import SessionInbox
from thesis.agent.runtime.json_recovery import extract_json_object
from thesis.agent.runtime.schema import render_violations, validate_arguments
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis.chat.responder import ToolResultChunk
from thesis.providers.invocations import (
    ProviderRejectedError,
    ProviderUnreachableError,
)

#: How many model/tool round trips one session allows before forcing a stop.
DEFAULT_MAX_TOOL_ITERATIONS = 64

AGENT_SYSTEM_PROMPT = (
    "You are the Thesis research agent. Use the available tools to execute "
    "deterministic research: start a workflow with start_workflow when the "
    "operator asks to develop or falsify an idea, spawn a helper with "
    "spawn_sub_agent for independent work, and call the quant-harness tools "
    "directly for backtests, studies, sweeps, and artifacts. A spawned helper "
    "runs asynchronously: call await_sub_agent to wait for its result, or end "
    "your turn and its result is delivered to this conversation when it "
    "finishes. Rely on the "
    "structured tool results as evidence and surface any failure instead of "
    "assuming success. A workflow outcome notification (including a failure) "
    "is delivered to this conversation: report it to the operator and stop. "
    "Start another execution only when the operator asked for one - a fresh "
    "request or their standing instruction - never on your own initiative. "
    "Workflow phase transitions are owned by the workflow, never by you. "
    "Starting a workflow hands the work to it: it routes its own phases and "
    "data, asks the operator for anything it needs from inside the phase that "
    "needs it, and reports back as its own configuration dictates (some "
    "workflows report only at terminal, others report throughout their "
    "lifecycle). After start_workflow "
    "returns, reply with one short sentence saying you started it and stop "
    "there. Do not gather or prepare the workflow's inputs, do not spawn "
    "helpers to look for them, do not relay or answer the workflow's requests, "
    "and do not perform any of its work."
)


class SessionPersistenceError(RuntimeError):
    """A durable activity write failed inside an otherwise recoverable tool."""


class CompletionProvider(Protocol):
    """The provider boundary a session completes against."""

    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Literal["primary", "coding", "critique", "research"],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Return the pure, JSON-compatible opaque audit of this exact request."""
        ...

    def complete(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Literal["primary", "coding", "critique", "research"],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> SessionMessage: ...


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
        return json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
        )


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

    @property
    def input_schema(self) -> Mapping[str, object]:
        """Return the JSON Schema the tool advertised for its arguments."""
        function = self.spec.get("function")
        if isinstance(function, Mapping):
            parameters = function.get("parameters")
            if isinstance(parameters, Mapping):
                return parameters
        return {}


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
                failure={
                    "kind": "unknown_tool",
                    "message": f"unknown tool: {name}",
                    "certainty": "not_started",
                    "retry_safe": True,
                },
            )
        if not self.permits(name):
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "denied",
                    "message": f"tool is not permitted by the effective policy: {name}",
                    "tool": name,
                    "certainty": "not_started",
                    "retry_safe": True,
                },
            )
        # One schema abstraction for every dispatched tool call: the arguments
        # must fit the schema the tool advertised before its handler can run.
        started = False
        try:
            misses = validate_arguments(tool.input_schema, arguments)
            if misses:
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "invalid_arguments",
                        "message": f"invalid arguments: {render_violations(misses)}",
                        "tool": name,
                        "certainty": "not_started",
                        "retry_safe": True,
                        "violations": [
                            {"path": miss.path, "message": miss.message}
                            for miss in misses
                        ],
                    },
                )
            started = True
            execution = tool.handler(arguments)
            if not execution.ok:
                execution = replace(
                    execution,
                    terminal=False,
                    failure={
                        "certainty": (execution.failure or {}).get(
                            "executionCertainty", "observed_failure"
                        ),
                        "retry_safe": False,
                        **(execution.failure or {}),
                    },
                )
            return execution
        except (CancelledError, SessionPersistenceError):
            raise
        except Exception as error:  # noqa: BLE001 - a tool fault is a normal turn
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "tool",
                    "message": str(error) or type(error).__name__,
                    "certainty": "unknown_outcome" if started else "not_started",
                    "retry_safe": not started,
                },
            )


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One durable turn of a session transcript.

    ``started_at`` is the wall-clock start of the model/tool attempt and
    ``duration_ms`` its monotonic elapsed milliseconds, excluding persistence
    and callbacks. Skipped calls use their result timestamp and zero duration.
    ``arguments`` holds decoded tool arguments (including tolerant recovery),
    or ``None`` for non-tool turns and invalid JSON objects. The assistant's
    message retains the original request argument strings for replay.
    """

    message: SessionMessage
    invocation_id: str | None = None
    datums: tuple[Datum, ...] = ()
    started_at: datetime | None = None
    duration_ms: int | None = None
    arguments: Mapping[str, object] | None = None


def tool_chunks(turn: SessionTurn) -> Iterator[ToolResultChunk]:
    """Project a tool result once, identically for every conversation anchor."""
    correlation = turn.message.content.correlation
    tool = correlation.tool_name if correlation is not None else "tool"
    call_id = correlation.request_id if correlation is not None else None
    data: dict[str, object] = {}
    if turn.started_at is not None:
        data["startedAt"] = turn.started_at.isoformat()
    if turn.duration_ms is not None:
        data["durationMs"] = turn.duration_ms
    if turn.arguments is not None:
        data["arguments"] = dict(turn.arguments)
    if turn.invocation_id is not None:
        data["invocationId"] = turn.invocation_id
    if turn.datums:
        for datum in turn.datums:
            yield ToolResultChunk(
                tool=tool,
                call_id=call_id,
                ok=datum.ok,
                summary=datum.summary or datum.text.strip() or f"{tool} result",
                structured=datum.structured,
                text=datum.text,
                failure=datum.failure,
                datum_type=datum.datum_type,
                data=data,
            )
        return
    payload = turn.message.content.result or {}
    structured, failure, text = (
        payload.get("structured"),
        payload.get("failure"),
        payload.get("text"),
    )
    yield ToolResultChunk(
        tool=tool,
        call_id=call_id,
        ok=bool(payload.get("ok")),
        summary=text if isinstance(text, str) and text.strip() else f"{tool} result",
        structured=structured if isinstance(structured, Mapping) else {},
        text=text if isinstance(text, str) else "",
        failure=failure if isinstance(failure, Mapping) else None,
        data=data,
    )


@dataclass(frozen=True, slots=True)
class SessionResult:
    """The terminal result of one session run."""

    status: str
    content: str
    turns: tuple[SessionTurn, ...]
    completion: Mapping[str, object] | None = None
    failure: Mapping[str, object] | None = None

    @property
    def stopped_for_completion(self) -> bool:
        return self.status == "completed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _tool_arguments(call: ToolRequest) -> Mapping[str, object] | None:
    raw = call.arguments.strip()
    if not raw:
        return {}
    try:
        decoded: object = json.loads(raw)
    except ValueError:
        # Tolerant recovery: a model that wrapped its JSON in prose or a
        # markdown fence still gets one in-turn repair-free dispatch.
        decoded = extract_json_object(raw)
        if decoded is None:
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
        role: Literal["primary", "coding", "critique", "research"] = "primary",
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] | None = None,
        intent_epoch: int = 0,
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
        self._intent_epoch = intent_epoch
        self._max_tool_iterations = max_tool_iterations
        self._clock = clock
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid.uuid4().hex}")

    def run(
        self,
        messages: Sequence[SessionMessage],
        *,
        before_model_call: Callable[[], Sequence[SessionMessage]] | None = None,
        reconcile: Callable[[Sequence[SessionMessage]], Sequence[SessionMessage]]
        | None = None,
        deliver: Callable[[SessionMessage], SessionMessage] | None = None,
        inbox: SessionInbox | None = None,
        on_consumed: Callable[[Sequence[SessionMessage]], None] | None = None,
        on_turn: Callable[[SessionTurn], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        admit_dispatch: Callable[[], bool] | None = None,
        refresh_registry: Callable[[], tuple[SessionToolRegistry, int]] | None = None,
    ) -> SessionResult:
        """Run to text completion, terminal control tool, or the iteration cap.

        ``before_model_call`` drains messages that arrived after the turn began
        into the live conversation between model iterations, so the model sees
        the latest inbound data without restarting the turn. Every arrival uses
        ``inbox.deliver``, whose optional writer returns its durable identity and
        position. Inputs are acknowledged only after a successful completion
        and its outgoing ``on_turn`` persistence callback.
        The same pull also runs after provider success, before outgoing
        persistence, to preserve durable arrival order. Those arrivals remain
        pending; their registry refresh belongs to the next model boundary.
        ``reconcile`` receives the known scope at every model boundary and returns
        its authoritative durable ordering plus newly admitted incoming rows.
        The reader must filter to that scope and retain its history bounds;
        bootstrap instructions may be omitted. Reconciliation never acknowledges
        inputs or introduces unknown outputs.
        ``on_consumed`` acknowledges that exact snapshot in the durable store;
        producers must not acknowledge arrivals in ``before_model_call``.

        ``on_turn`` receives each outgoing envelope before tool dispatch and
        each delivered result for observation. It persists outputs only: incoming
        persistence belongs exclusively to the inbox writer. Returned ``turns``
        contains those same events. Callback/store faults propagate.

        ``is_cancelled`` is checked before model requests and every dispatch.
        Cancellation is latched: an active synchronous call may finish, while
        remaining calls get paired ``cancelled`` results and no model is called
        again. Expected provider errors return ``interrupted`` with partial
        turns and ``failure``; other model faults propagate. Failure certainty
        is ``not_started``, ``observed_failure``, or ``unknown_outcome``;
        ``retry_safe`` is true only when execution definitely did not start.
        Provider adapters normalize transport faults to ProviderUnreachableError.
        After a terminal tool is accepted, remaining calls receive paired
        ``skipped``/``not_started`` results without dispatch; the accepted
        completion is preserved, even if cancellation arrives while pairing.
        """
        if inbox is not None and (messages or deliver is not None):
            raise ValueError("supply either an inbox or history and a delivery writer")
        inbox = inbox if inbox is not None else SessionInbox(messages, persist=deliver)
        turns: list[SessionTurn] = []
        reasoning_parts: list[str] = []
        cancelled = False
        for iteration in range(self._max_tool_iterations):
            if is_cancelled is not None and is_cancelled():
                return SessionResult(
                    status="cancelled",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                )
            fresh: Sequence[SessionMessage] = ()
            if before_model_call is not None:
                fresh = before_model_call()
                for message in fresh:
                    inbox.deliver(message)
            reconciled_inputs = False
            if reconcile is not None:
                pending_ids = {message.message_id for message in inbox.pending()}
                inbox.reconcile(reconcile(inbox.transcript()))
                reconciled_inputs = any(
                    message.message_id not in pending_ids for message in inbox.pending()
                )
            if refresh_registry is not None and (
                fresh or reconciled_inputs or (iteration > 0 and inbox.pending())
            ):
                self._registry, self._intent_epoch = refresh_registry()
            catalog = self._registry.catalog()
            if is_cancelled is not None and is_cancelled():
                return SessionResult(
                    status="cancelled",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                )
            working, pending = inbox.snapshot()
            request_messages = tuple(
                {
                    "message_id": message.message_id,
                    "direction": message.direction,
                    "position": message.position,
                }
                for message in working
            )
            provider_request = self._provider.request_snapshot(
                messages=working,
                role=self._role,
                tools=catalog or None,
            )
            if not isinstance(provider_request, Mapping):
                raise TypeError("provider request snapshot must be a mapping")
            request_json = json.dumps(
                dict(provider_request),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            provider_request = json.loads(request_json)
            input_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
            model_invocation_id = self._record_invocation(
                kind=AgentInvocationKind.MODEL,
                state=AgentInvocationState.STARTED,
                parent_invocation_id=None,
                request_messages=request_messages,
                provider_request=provider_request,
                input_hash=input_hash,
            )
            model_started_at = self._clock()
            model_started = monotonic()
            if (is_cancelled is not None and is_cancelled()) or (
                admit_dispatch is not None and not admit_dispatch()
            ):
                return SessionResult(
                    status="cancelled",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                )
            try:
                completion = self._provider.complete(
                    messages=working,
                    role=self._role,
                    tools=catalog or None,
                )
                if (
                    completion.direction == "outgoing"
                    and not completion.content.text.strip()
                    and not completion.content.requests
                ):
                    raise ProviderRejectedError(
                        "provider returned an empty completion without requests"
                    )
            except (ProviderUnreachableError, ProviderRejectedError) as error:
                failure: dict[str, object] = {
                    "kind": (
                        "provider_rejected"
                        if isinstance(error, ProviderRejectedError)
                        else "provider_unreachable"
                    ),
                    "message": str(error) or type(error).__name__,
                    "certainty": (
                        "observed_failure"
                        if isinstance(error, ProviderRejectedError)
                        else "unknown_outcome"
                    ),
                    "retry_safe": False,
                }
                if isinstance(error, ProviderRejectedError):
                    failure["status"] = error.status
                self._record_invocation(
                    kind=AgentInvocationKind.MODEL,
                    state=AgentInvocationState.FAILED,
                    parent_invocation_id=None,
                    invocation_id=model_invocation_id,
                    failure=failure,
                    request_messages=request_messages,
                    provider_request=provider_request,
                    input_hash=input_hash,
                )
                return SessionResult(
                    status="interrupted",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                    failure=failure,
                )
            model_duration_ms = int((monotonic() - model_started) * 1000)
            if completion.direction != "outgoing":
                raise ValueError("completion provider must return an outgoing message")
            # Steering persisted during this request precedes its outgoing record,
            # but cannot authorize actions in the already completed request.
            if before_model_call is not None:
                for message in before_model_call():
                    inbox.deliver(message)
            self._record_invocation(
                kind=AgentInvocationKind.MODEL,
                state=AgentInvocationState.SUCCEEDED,
                parent_invocation_id=None,
                invocation_id=model_invocation_id,
                output_hash=None,
                request_messages=request_messages,
                provider_request=provider_request,
                input_hash=input_hash,
            )
            reasoning = (completion.content.result or {}).get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
            turn = SessionTurn(
                message=completion,
                invocation_id=model_invocation_id,
                started_at=model_started_at,
                duration_ms=model_duration_ms,
            )
            turns.append(turn)
            if on_turn is not None:
                on_turn(turn)
            inbox.record_output(completion)
            if on_consumed is not None and pending:
                on_consumed(pending)
            inbox.acknowledge(pending)
            if not completion.content.requests:
                return SessionResult(
                    status=(
                        "cancelled"
                        if is_cancelled is not None and is_cancelled()
                        else "completed"
                    ),
                    content=completion.content.text,
                    turns=tuple(turns),
                )
            terminal_completion: Mapping[str, object] | None = None
            for call in completion.content.requests:
                cancelled = cancelled or (is_cancelled is not None and is_cancelled())
                tool_input_hash = hashlib.sha256(
                    json.dumps(
                        call.as_dict(),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                tool_invocation_id = self._record_invocation(
                    kind=AgentInvocationKind.TOOL,
                    state=AgentInvocationState.STARTED,
                    parent_invocation_id=model_invocation_id,
                    tool_name=call.name,
                    request_message_id=completion.message_id,
                    tool_request_id=call.id,
                    input_hash=tool_input_hash,
                )
                tool_started_at = self._clock()
                tool_started = monotonic()
                arguments = _tool_arguments(call)
                cancelled = cancelled or (is_cancelled is not None and is_cancelled())
                cancelled = cancelled or (
                    admit_dispatch is not None and not admit_dispatch()
                )
                skipped = terminal_completion is not None or cancelled
                if terminal_completion is not None:
                    execution = ToolExecution(
                        ok=False,
                        failure={
                            "kind": "skipped",
                            "message": "tool call skipped after terminal completion",
                            "certainty": "not_started",
                            "retry_safe": True,
                        },
                    )
                elif cancelled:
                    execution = ToolExecution(
                        ok=False,
                        failure={
                            "kind": "cancelled",
                            "message": "tool call cancelled before execution",
                            "certainty": "not_started",
                            "retry_safe": True,
                        },
                    )
                elif arguments is None:
                    execution = ToolExecution(
                        ok=False,
                        failure={
                            "kind": "tool",
                            "certainty": "not_started",
                            "retry_safe": True,
                            "message": (
                                "tool arguments were not a JSON object, even "
                                "after tolerant recovery of fenced or embedded "
                                "JSON; send one raw JSON object"
                            ),
                        },
                    )
                else:
                    try:
                        execution = self._registry.dispatch(call.name, arguments)
                    except CancelledError:
                        cancelled = True
                        execution = ToolExecution(
                            ok=False,
                            failure={
                                "kind": "cancelled",
                                "message": "tool execution cancelled with unknown outcome",
                                "certainty": "unknown_outcome",
                                "retry_safe": False,
                            },
                        )
                try:
                    datums = normalize_datums(
                        execution.datums,
                        fallback_type=call.name,
                        ok=execution.ok,
                        structured=execution.structured,
                        text=execution.text,
                        failure=execution.failure,
                    )
                    # Validate every datum before constructing the canonical result.
                    json.dumps([datum.as_dict() for datum in datums], allow_nan=False)
                    execution.as_content()
                except CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - result faults are tool faults
                    execution = ToolExecution(
                        ok=False,
                        failure={
                            "kind": "tool",
                            "message": str(error) or type(error).__name__,
                            "certainty": "unknown_outcome",
                            "retry_safe": False,
                        },
                    )
                    datums = (Datum(call.name, ok=False, failure=execution.failure),)
                compact_datums = []
                if len(execution.text) > 512 and all(
                    datum.text != execution.text for datum in datums
                ):
                    datums = (
                        *datums,
                        Datum(call.name, ok=execution.ok, text=execution.text),
                    )
                for datum in datums:
                    failure = dict(datum.failure) if datum.failure is not None else None
                    if failure is not None and failure.get("payload") == dict(
                        datum.structured
                    ):
                        failure.pop("payload")
                    text = datum.text
                    if text and datum.structured:
                        try:
                            if json.loads(text) == dict(datum.structured):
                                text = ""
                        except ValueError:
                            pass
                    summary = datum.summary
                    if summary == datum.text and len(summary or "") > 512:
                        summary = f"{call.name} result"
                    compact_datums.append(
                        replace(datum, failure=failure, text=text, summary=summary)
                    )
                datums = tuple(compact_datums)
                tool_duration_ms = (
                    0 if skipped else int((monotonic() - tool_started) * 1000)
                )
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
                    failure=(
                        None
                        if execution.failure is None
                        else {
                            key: value
                            for key, value in execution.failure.items()
                            if key != "payload"
                        }
                    ),
                    request_message_id=completion.message_id,
                    tool_request_id=call.id,
                    input_hash=tool_input_hash,
                )
                payload: dict[str, object] = {"ok": execution.ok}
                if execution.failure is not None:
                    payload["failure"] = {
                        key: value
                        for key, value in execution.failure.items()
                        if key != "payload"
                        or not any(
                            value == dict(datum.structured)
                            or value == (datum.failure or {}).get("payload")
                            for datum in datums
                        )
                    }
                # Preserve distinct receipt/summary data, not repeated datum bodies.
                if execution.structured and all(
                    dict(datum.structured) != dict(execution.structured)
                    for datum in datums
                ):
                    payload["structured"] = dict(execution.structured)
                payload.update(
                    datums=[datum.as_dict() for datum in datums],
                    invocationId=tool_invocation_id,
                    startedAt=tool_started_at.isoformat(),
                    durationMs=tool_duration_ms,
                    arguments=None if arguments is None else dict(arguments),
                )
                delivered = inbox.deliver(
                    SessionMessage(
                        message_id=self._id_factory("message"),
                        direction="incoming",
                        content=MessageContent(
                            text=execution.text
                            if len(execution.text) <= 512
                            else f"{call.name} result",
                            source="tool",
                            correlation=ToolCorrelation(
                                request_message_id=completion.message_id,
                                request_id=call.id,
                                tool_name=call.name,
                            ),
                            result=payload,
                        ),
                    )
                )
                turn = SessionTurn(
                    message=delivered,
                    invocation_id=tool_invocation_id,
                    datums=datums,
                    started_at=tool_started_at,
                    duration_ms=tool_duration_ms,
                    arguments=(
                        None if arguments is None else MappingProxyType(dict(arguments))
                    ),
                )
                turns.append(turn)
                if on_turn is not None:
                    on_turn(turn)
                cancelled = cancelled or (is_cancelled is not None and is_cancelled())
                if execution.terminal and not cancelled:
                    terminal_completion = execution.structured
            if terminal_completion is not None:
                return SessionResult(
                    status="completed",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                    completion=terminal_completion,
                )
            if cancelled:
                return SessionResult(
                    status="cancelled",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
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
        input_hash: str | None = None,
        request_messages: tuple[Mapping[str, object], ...] = (),
        provider_request: Mapping[str, object] | None = None,
        request_message_id: str | None = None,
        tool_request_id: str | None = None,
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
            intent_epoch=self._intent_epoch,
            output_hash=output_hash,
            input_hash=input_hash,
            request_messages=request_messages,
            provider_request=provider_request,
            request_message_id=request_message_id,
            tool_request_id=tool_request_id,
            failure=None if failure is None else MappingProxyType(dict(failure)),
            updated_at=now,
        )
        self._store.save_invocation(invocation)
        return resolved_id


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "AgentSession",
    "CompletionProvider",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "RegisteredTool",
    "SessionResult",
    "SessionToolRegistry",
    "SessionTurn",
    "ToolExecution",
    "ToolHandler",
    "tool_chunks",
]
