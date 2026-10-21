"""Unified durable agent session: the model tool-calling turn loop.

One :class:`AgentSession` serves a top-level chat reply, a chat-spawned
sub-agent, and a workflow phase sub-agent.  It sends the merged tool catalog
(harness tools plus Thesis control tools) with every completion, persists each
model invocation and each tool call as a first-class durable record, threads
tool results back to the model as canonical incoming messages, and repeats until the model
answers in text, a terminal control tool is accepted, or the execution slice
yields for automatic continuation.

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from types import MappingProxyType
from typing import Literal

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum, normalize_datums
from thesis_runtime.contracts.errors import (
    ProviderRejectedError,
    ProviderUnreachableError,
)
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis_runtime.contracts.reply import ToolResultChunk
from thesis_runtime.conversations.inbox import SessionInbox
from thesis_runtime.conversations.timeline import TimelineWriter
from thesis_runtime.sessions.model_invocation import (
    CompletionProvider,
    InvocationPersistence,
    ModelInvocation,
    ModelSteered,
)
from thesis_runtime.sessions.recovery import (
    MAX_RECOVERY_DELAY,
    RECOVERY_DELAYS,
    wait_for_recovery,
)
from thesis_runtime.storage.ownership import assert_owned
from thesis_runtime.tools.json_recovery import extract_json_object
from thesis_runtime.tools.registry import (
    SessionToolRegistry,
    ToolExecution,
)

#: Model/tool round trips in one execution slice; the conversation resumes yielded work.
DEFAULT_MAX_TOOL_ITERATIONS = 64

AGENT_SYSTEM_PROMPT = (
    "Use the registered tools to carry out the operator's request. "
    "Use structured results as evidence and report failures truthfully. "
    "A started workflow owns its phases and operator interaction. "
    "Await asynchronous helpers or end the turn to receive their results."
)


@dataclass(frozen=True, slots=True)
class SessionTurn:
    """One durable turn of a session transcript.

    ``started_at`` is the wall-clock start of the model/tool attempt and
    ``duration_ms`` its monotonic elapsed milliseconds. Model timing includes
    streamed progress publication but excludes the initial audit and final commit;
    tool timing excludes persistence and callbacks. Skipped calls use their result
    timestamp and zero duration.
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
    operation = (turn.message.content.result or {}).get("operation")
    if operation is not None:
        data["operation"] = operation
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
        tool_context: ToolContext | None = None,
        provider: CompletionProvider,
        registry: SessionToolRegistry,
        store: InvocationPersistence | None = None,
        sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        actor_id: str = "thesis-runtime",
        role: Literal["primary", "coding", "critique", "research"] = "primary",
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] | None = None,
        intent_epoch: int = 0,
        timeline: TimelineWriter | None = None,
    ) -> None:
        if max_tool_iterations < 1:
            raise ValueError("max_tool_iterations must be positive")
        self._tool_context = tool_context or ToolContext(
            workspace_id,
            chat_id,
            conversation_id,
            actor_id,
            sub_agent_id=sub_agent_id,
            workflow_execution_id=workflow_execution_id,
            phase_attempt_id=phase_attempt_id,
            intent_epoch=intent_epoch,
        )
        self._workspace_id = workspace_id
        self._chat_id = chat_id
        self._conversation_id = conversation_id
        self._provider = provider
        self._timeline = timeline
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
        is_steered: Callable[[], bool] | None = None,
        admit_dispatch: Callable[[], bool] | None = None,
        refresh_registry: Callable[[], tuple[SessionToolRegistry, int]] | None = None,
    ) -> SessionResult:
        """Run to text completion, terminal control tool, or the iteration cap.

        ``is_steered`` stops an active model stream for new operator input.
        Received output is sealed through the same outgoing persistence boundary,
        with no tool requests or consumption ACK, before starting a fresh request
        containing that partial evidence and the new input.
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
        again. Recoverable provider failures retain FAILED invocation audits and
        non-executable partial output, then request again after 1/2/4 second
        cooperative delays (honoring Retry-After up to 60 seconds). This resumes
        the existing session; it never replays completed tool calls. Permanent
        provider errors and exhausted recovery return ``interrupted`` with partial
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
        last_structural_failures: tuple[tuple[str, str], ...] = ()
        repeated_structural_failures = 0
        provider_failures = 0
        seen_operator_inputs: set[str] = set()
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
            operator_inputs = {
                message.message_id
                for message in pending
                if message.content.source == "user"
            }
            if operator_inputs - seen_operator_inputs:
                provider_failures = 0
            seen_operator_inputs.update(operator_inputs)
            if any(message.content.source == "user" for message in pending):
                last_structural_failures = ()
                repeated_structural_failures = 0
            model_invocation_id = self._id_factory("invocation")
            model_started_at = self._clock()
            model_output = ModelInvocation(
                AgentInvocation(
                    workspace_id=self._workspace_id,
                    created_at=model_started_at,
                    created_by=self._actor_id,
                    invocation_id=model_invocation_id,
                    chat_id=self._chat_id,
                    conversation_id=self._conversation_id,
                    kind=AgentInvocationKind.MODEL,
                    state=AgentInvocationState.STARTED,
                    sub_agent_id=self._sub_agent_id,
                    workflow_execution_id=self._workflow_execution_id,
                    phase_attempt_id=self._phase_attempt_id,
                    intent_epoch=self._intent_epoch,
                    root_conversation_id=self._chat_id,
                    parent_conversation_id=None
                    if self._conversation_id == self._chat_id
                    else self._chat_id,
                ),
                f"message-{model_invocation_id}",
                self._timeline,
                is_cancelled or (lambda: False),
                clock=self._clock,
                steered=is_steered,
            )
            if (is_cancelled is not None and is_cancelled()) or (
                admit_dispatch is not None and not admit_dispatch()
            ):
                return SessionResult(
                    status="cancelled",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                )
            steering = False
            assert_owned()
            try:
                completion = model_output.execute(
                    self._provider, working, self._role, catalog or None, self._store
                )
            except ModelSteered as interruption:
                completion = interruption.message
                steering = True
            except (ProviderUnreachableError, ProviderRejectedError) as error:
                if not model_output.failure_committed:
                    # An audit/persistence failure is not a provider attempt.
                    raise
                failure = model_output.failure(error)
                if model_output.has_partial_output:
                    partial = model_output.partial_message(failure=failure)
                    partial_turn = SessionTurn(
                        message=partial,
                        invocation_id=model_invocation_id,
                        started_at=model_output.started_at,
                        duration_ms=model_output.duration_ms,
                    )
                    # execute already retained FAILED. Persist evidence without
                    # converting failure into success, input ACK or tool dispatch.
                    if on_turn is not None:
                        on_turn(partial_turn)
                    inbox.record_output(partial)
                    turns.append(partial_turn)
                retry_after = (
                    error.retry_after
                    if isinstance(error, ProviderRejectedError)
                    else None
                )
                if (
                    error.recoverable
                    and (retry_after is None or retry_after <= MAX_RECOVERY_DELAY)
                    and provider_failures < len(RECOVERY_DELAYS)
                    and iteration + 1 < self._max_tool_iterations
                    and not (is_cancelled is not None and is_cancelled())
                ):
                    delay = max(RECOVERY_DELAYS[provider_failures], retry_after or 0)
                    provider_failures += 1
                    inbox.deliver(
                        SessionMessage(
                            message_id=f"recovery-{model_invocation_id}",
                            direction="incoming",
                            content=MessageContent(
                                text=f"Model provider interrupted generation. Automatically continuing this agent "
                                f"in {delay:g}s (recovery {provider_failures}/{len(RECOVERY_DELAYS)}). "
                                "Continue the existing task from retained messages and tool results; "
                                "reconcile retained job IDs rather than repeating submissions. "
                                "If the response was unusable, take a smaller actionable step; "
                                "author incrementally rather than reasoning through the entire program again.",
                                source="runtime",
                                result={
                                    "providerRecovery": {
                                        "invocationId": model_invocation_id,
                                        "attempt": provider_failures,
                                        "delaySeconds": delay,
                                        "failure": failure,
                                    }
                                },
                            ),
                        )
                    )
                    wait_for_recovery(delay, cancelled=is_cancelled, steered=is_steered)
                    continue
                return SessionResult(
                    status="interrupted",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                    failure=failure,
                )
            provider_failures = 0
            # Steering persisted during this request precedes its outgoing record,
            # but cannot authorize actions in the already completed request.
            if before_model_call is not None:
                fresh = before_model_call()
                for message in fresh:
                    inbox.deliver(message)
                working_ids = {message.message_id for message in working}
                if any(
                    message.content.source == "user"
                    and message.message_id not in working_ids
                    for message in fresh
                ):
                    completion = model_output.partial_message(completion)
                    steering = True
            reasoning = (completion.content.result or {}).get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
            turn = SessionTurn(
                message=completion,
                invocation_id=model_invocation_id,
                started_at=model_output.started_at,
                duration_ms=model_output.duration_ms,
            )
            turns.append(turn)

            def persist_output(turn: SessionTurn = turn) -> None:
                if on_turn is not None:
                    on_turn(turn)
                inbox.record_output(turn.message)

            model_output.commit(
                persist_output,
                failure={
                    "kind": "operator_steering",
                    "message": "Model generation interrupted by new operator input.",
                    "certainty": "observed_failure",
                    "retry_safe": False,
                }
                if steering
                else None,
            )
            if steering:
                continue
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
            structural_failures: list[tuple[str, str]] = []
            structural_only = True
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
                tool_invocation_id = self._record_tool_invocation(
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
                        execution = self._registry.dispatch(
                            call.name,
                            arguments,
                            context=replace(
                                self._tool_context,
                                intent_epoch=self._intent_epoch,
                                model_invocation_id=model_invocation_id,
                                tool_invocation_id=tool_invocation_id,
                                idempotency_key=tool_invocation_id,
                            ),
                        )
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
                failure = execution.failure or {}
                if failure.get("kind") == "invalid_arguments":
                    structural_failures.append(
                        (
                            call.name,
                            json.dumps(failure.get("violations"), sort_keys=True),
                        )
                    )
                else:
                    structural_only = False
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
                self._record_tool_invocation(
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
                    operation=self._registry.operation(call.name),
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
            signature = tuple(structural_failures) if structural_only else ()
            if any(message.content.source == "user" for message in inbox.pending()):
                signature = ()
            repeated_structural_failures = (
                repeated_structural_failures + 1
                if signature and signature == last_structural_failures
                else (1 if signature else 0)
            )
            last_structural_failures = signature
            if repeated_structural_failures >= 3:
                return SessionResult(
                    status="interrupted",
                    content="".join(reasoning_parts),
                    turns=tuple(turns),
                    failure={
                        "kind": "tool_schema_loop",
                        "message": "Repeated tool argument schema failures. "
                        "The rejected calls did not execute; inspect the tool schema "
                        "and model provider before retrying.",
                        "certainty": "not_started",
                        "retry_safe": False,
                        "tools": sorted({name for name, _ in signature}),
                    },
                )
        return SessionResult(
            status="yielded",
            content="".join(reasoning_parts),
            turns=tuple(turns),
        )

    def _record_tool_invocation(
        self,
        *,
        state: AgentInvocationState,
        parent_invocation_id: str | None,
        tool_name: str | None = None,
        invocation_id: str | None = None,
        output_hash: str | None = None,
        input_hash: str | None = None,
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
            kind=AgentInvocationKind.TOOL,
            state=state,
            sub_agent_id=self._sub_agent_id,
            parent_invocation_id=parent_invocation_id,
            workflow_execution_id=self._workflow_execution_id,
            phase_attempt_id=self._phase_attempt_id,
            tool_name=tool_name,
            intent_epoch=self._intent_epoch,
            output_hash=output_hash,
            input_hash=input_hash,
            request_message_id=request_message_id,
            tool_request_id=tool_request_id,
            failure=None if failure is None else MappingProxyType(dict(failure)),
            updated_at=now,
            root_conversation_id=self._chat_id,
            parent_conversation_id=None
            if self._conversation_id == self._chat_id
            else self._chat_id,
        )
        self._store.save_invocation(invocation)
        return resolved_id


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "AgentSession",
    "CompletionProvider",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "SessionResult",
    "SessionToolRegistry",
    "SessionTurn",
    "ToolExecution",
    "tool_chunks",
]
