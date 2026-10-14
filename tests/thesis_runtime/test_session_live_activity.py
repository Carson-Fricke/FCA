"""Live session turns, recoverable failures, and cooperative cancellation."""

from __future__ import annotations

import ssl
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderRejectedError,
    ProviderUnreachableError,
    Role,
    ToolCall,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolRequest,
)
from thesis_runtime.contracts.records import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
)
from thesis_runtime.sessions.session import AgentSession, SessionTurn, tool_chunks
from thesis_runtime.subagents.store import InMemorySubAgentStore
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
    ToolHandler,
)


def _completion(*calls: ToolCall, content: str = "") -> ModelCompletion:
    return ModelCompletion(
        content=content,
        model="test-model",
        provider="test-provider",
        role="primary",
        prompt_tokens=0,
        completion_tokens=0,
        tool_calls=calls,
    )


class _Provider:
    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        return {
            "projection": "test-neutral",
            "messages": [message.as_dict() for message in messages],
            "role": role,
            "tools": None if tools is None else list(tools),
            "response_format": response_format,
        }

    def __init__(self, *responses: ModelCompletion | Exception) -> None:
        self.responses = iter(responses)
        self.requests: list[tuple[SessionMessage, ...]] = []

    def stream(self, *, message_id=None, **kwargs):
        from tests.thesis_runtime.provider_fakes import completion_frames

        yield from completion_frames(self.complete(**kwargs), message_id)

    def complete(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> SessionMessage:
        self.requests.append(tuple(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            if isinstance(response, (ssl.SSLError, httpx.TransportError)):
                raise ProviderUnreachableError(str(response)) from response
            raise response
        return SessionMessage(
            message_id=f"output-{uuid.uuid4().hex}",
            direction="outgoing",
            content=MessageContent(
                text=response.content,
                source="model",
                requests=tuple(
                    ToolRequest(c.id, c.name, c.arguments) for c in response.tool_calls
                ),
            ),
        )


class _Store(InMemorySubAgentStore):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[AgentInvocation] = []

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
        self.records.append(invocation)
        return super().save_invocation(invocation)


def _session(
    provider: _Provider,
    handler: ToolHandler,
    *,
    schema: Mapping[str, object] | None = None,
    store: _Store | None = None,
) -> AgentSession:
    return AgentSession(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="conversation",
        provider=provider,
        registry=SessionToolRegistry(
            [
                RegisteredTool(
                    descriptor={
                        "name": "work",
                        "parameters": schema or {"type": "object"},
                    },
                    handler=handler,
                )
            ]
        ),
        store=store,
    )


_INPUT = (
    SessionMessage(
        "input", "incoming", MessageContent(text="Do the work", source="user")
    ),
)
_CALL = ToolCall(id="call-1", name="work", arguments="{}")


def test_assistant_pending_envelope_is_published_before_handler() -> None:
    published: list[SessionTurn] = []

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        assert len(published) == 1
        assert published[0].message.direction == "outgoing"
        assert published[0].message.content.requests == (
            ToolRequest(_CALL.id, _CALL.name, _CALL.arguments),
        )
        return ToolExecution(ok=True, structured={"answer": 42})

    provider = _Provider(_completion(_CALL), _completion(content="Finished"))
    result = _session(provider, handler).run(_INPUT, on_turn=published.append)

    assert result.status == "completed"
    assert tuple(published) == result.turns
    assert [turn.message.direction for turn in published] == [
        "outgoing",
        "incoming",
        "outgoing",
    ]
    assert published[1].datums[0].structured == {"answer": 42}
    assert published[-1].message.content.text == "Finished"


@pytest.mark.parametrize("operation", ["read", "write", "execute", "interact"])
def test_catalog_operation_survives_delivery_and_live_projection(operation) -> None:
    provider = _Provider(_completion(_CALL), _completion(content="Finished"))
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                descriptor={"name": "work", "parameters": {"type": "object"}},
                operation=operation,
                handler=lambda _, context: ToolExecution(
                    ok=True,
                    structured={
                        "operation": "forged",
                        "content": "Complete returned evidence",
                    },
                ),
            )
        ]
    )
    result = AgentSession(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="chat",
        provider=provider,
        registry=registry,
    ).run(_INPUT)
    turn = result.turns[1]
    assert turn.message.content.result is not None
    assert provider.requests[1][-1].content.result is not None
    assert turn.message.content.result["operation"] == operation
    assert provider.requests[1][-1].content.result["operation"] == operation
    chunk = next(tool_chunks(turn))
    assert chunk.data is not None
    assert chunk.data["operation"] == operation
    assert chunk.structured == {
        "operation": "forged",
        "content": "Complete returned evidence",
    }


def test_tool_tls_failure_is_returned_to_next_model_request() -> None:
    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        raise ssl.SSLError("certificate verify failed")

    store = _Store()
    provider = _Provider(_completion(_CALL), _completion(content="Handled"))
    result = _session(provider, handler, store=store).run(_INPUT)

    assert result.status == "completed"
    tool_message = provider.requests[1][-1]
    assert tool_message.direction == "incoming"
    assert tool_message.content.correlation is not None
    assert tool_message.content.correlation.request_id == _CALL.id
    payload = tool_message.content.result
    assert payload is not None
    assert isinstance(payload["failure"], Mapping)
    assert isinstance(payload["failure"]["message"], str)
    assert payload["ok"] is False
    assert "certificate verify failed" in payload["failure"]["message"]
    assert payload["failure"]["certainty"] == "unknown_outcome"
    assert payload["failure"]["retry_safe"] is False
    assert store.records[3].state == AgentInvocationState.FAILED


@pytest.mark.parametrize(
    ("error", "kind", "certainty"),
    [
        (
            ProviderUnreachableError("offline"),
            "provider_unreachable",
            "unknown_outcome",
        ),
        (
            ProviderRejectedError("rate limited", status=429),
            "provider_rejected",
            "observed_failure",
        ),
        (ssl.SSLError("TLS failed"), "provider_unreachable", "unknown_outcome"),
        (
            httpx.ReadTimeout("read timed out"),
            "provider_unreachable",
            "unknown_outcome",
        ),
        (
            httpx.WriteTimeout("write timed out"),
            "provider_unreachable",
            "unknown_outcome",
        ),
        (httpx.ConnectError("offline"), "provider_unreachable", "unknown_outcome"),
    ],
)
def test_provider_failure_preserves_callbacks_and_records_failed_invocation(
    error: Exception, kind: str, certainty: str, monkeypatch
) -> None:
    published: list[SessionTurn] = []
    store = _Store()
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    provider = _Provider(_completion(_CALL), error, error, error, error)
    result = _session(
        provider, lambda _, context: ToolExecution(ok=True), store=store
    ).run(_INPUT, on_turn=published.append)

    assert result.status == "interrupted"
    assert len(provider.requests) == 5
    assert result.failure is not None
    assert result.failure["kind"] == kind
    assert result.failure["certainty"] == certainty
    assert tuple(published) == result.turns
    assert [turn.message.direction for turn in result.turns] == ["outgoing", "incoming"]
    invocation = store.records[-1]
    assert invocation.kind == AgentInvocationKind.MODEL
    assert invocation.state == AgentInvocationState.FAILED
    assert invocation.failure == result.failure
    assert invocation.invocation_id == store.records[-2].invocation_id
    if isinstance(error, ProviderRejectedError):
        assert result.failure["status"] == 429


@pytest.mark.parametrize("explicit_datum", [False, True])
def test_serialization_failure_is_nonterminal_and_recoverable(
    explicit_datum: bool,
) -> None:
    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        bad = {"not_json": object()}
        return ToolExecution(
            ok=True,
            structured={} if explicit_datum else bad,
            datums=(Datum("work", structured=bad),) if explicit_datum else (),
            terminal=True,
        )

    provider = _Provider(_completion(_CALL), _completion(content="Recovered"))
    published: list[SessionTurn] = []
    result = _session(provider, handler).run(_INPUT, on_turn=published.append)

    assert result.content == "Recovered"
    payload = provider.requests[1][-1].content.result
    assert payload is not None
    assert isinstance(payload["failure"], Mapping)
    assert payload["ok"] is False
    assert payload["failure"]["certainty"] == "unknown_outcome"
    assert payload["failure"]["retry_safe"] is False
    assert published[1].datums[0].ok is False


def test_malformed_schema_fails_before_advertisement() -> None:
    from jsonschema.exceptions import SchemaError

    schema = {"properties": {"name": {"type": "string", "pattern": "["}}}
    provider = _Provider(_completion(content="Unused"))
    with pytest.raises(SchemaError):
        _session(provider, lambda args, context: ToolExecution(ok=True), schema=schema)
    assert not provider.requests


def test_normalization_failure_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> tuple[Datum, ...]:
        raise ValueError("cannot normalize")

    monkeypatch.setattr("thesis_runtime.sessions.session.normalize_datums", broken)
    provider = _Provider(_completion(_CALL), _completion(content="Recovered"))
    result = _session(provider, lambda _, context: ToolExecution(ok=True)).run(_INPUT)

    assert result.status == "completed"
    assert result.turns[1].datums[0].ok is False
    assert "cannot normalize" in str(provider.requests[1][-1].content.result)


@pytest.mark.parametrize("cancel_before_handler", [False, True])
def test_cancellation_pairs_remaining_calls_and_stops_requests(
    cancel_before_handler: bool,
) -> None:
    cancelled = False
    dispatched: list[Mapping[str, object]] = []
    published: list[SessionTurn] = []
    calls = (_CALL, ToolCall(id="call-2", name="work", arguments="{}"))

    def on_turn(turn: SessionTurn) -> None:
        nonlocal cancelled
        published.append(turn)
        if turn.message.direction == "outgoing" and cancel_before_handler:
            cancelled = True

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        nonlocal cancelled
        dispatched.append(arguments)
        cancelled = True
        return ToolExecution(ok=True, terminal=True)

    provider = _Provider(_completion(*calls))
    result = _session(provider, handler).run(
        _INPUT, on_turn=on_turn, is_cancelled=lambda: cancelled
    )

    assert result.status == "cancelled"
    assert len(provider.requests) == 1
    assert len(dispatched) == (0 if cancel_before_handler else 1)
    assert tuple(published) == result.turns
    assert [
        turn.message.content.correlation.request_id
        for turn in result.turns[1:]
        if turn.message.content.correlation
    ] == [c.id for c in calls]
    skipped = result.turns[1:] if cancel_before_handler else result.turns[2:]
    for turn in skipped:
        payload = turn.message.content.result
        assert payload is not None
        assert isinstance(payload["failure"], Mapping)
        assert payload["ok"] is False
        assert payload["failure"]["kind"] == "cancelled"
        assert payload["failure"]["certainty"] == "not_started"
        assert turn.datums[0].failure == payload["failure"]


def test_cancellation_before_model_request_creates_no_invocation() -> None:
    store = _Store()
    provider = _Provider()
    result = _session(
        provider, lambda _, context: ToolExecution(ok=True), store=store
    ).run(_INPUT, is_cancelled=lambda: True)

    assert result.status == "cancelled"
    assert result.turns == ()
    assert not provider.requests
    assert not store.records


@pytest.mark.parametrize("fault_role", ["outgoing", "incoming"])
def test_callback_fault_is_not_converted_to_tool_failure(fault_role: str) -> None:
    def on_turn(turn: SessionTurn) -> None:
        if turn.message.direction == fault_role:
            raise ProviderUnreachableError("persistence unavailable")

    provider = _Provider(_completion(_CALL))
    with pytest.raises(ProviderUnreachableError, match="persistence unavailable"):
        _session(provider, lambda _, context: ToolExecution(ok=True)).run(
            _INPUT, on_turn=on_turn
        )
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "state", [AgentInvocationState.STARTED, AgentInvocationState.FAILED]
)
def test_model_audit_failure_never_starts_provider_recovery(state):
    class BrokenStore(_Store):
        def save_invocation(self, invocation):
            if (
                invocation.kind is AgentInvocationKind.MODEL
                and invocation.state is state
            ):
                raise ProviderUnreachableError("audit unavailable")
            return super().save_invocation(invocation)

    provider = _Provider(
        ProviderUnreachableError("provider offline"), _completion(content="Recovered")
    )
    with pytest.raises(ProviderUnreachableError, match="audit unavailable"):
        _session(
            provider, lambda _, context: ToolExecution(ok=True), store=BrokenStore()
        ).run(_INPUT)
    assert len(provider.requests) == (0 if state is AgentInvocationState.STARTED else 1)


def test_durable_store_fault_is_not_converted_to_tool_failure() -> None:
    class BrokenStore(_Store):
        def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
            if invocation.kind == AgentInvocationKind.TOOL:
                raise OSError("cannot persist")
            return super().save_invocation(invocation)

    provider = _Provider(_completion(_CALL))
    with pytest.raises(OSError, match="cannot persist"):
        _session(
            provider, lambda _, context: ToolExecution(ok=True), store=BrokenStore()
        ).run(_INPUT)
    assert len(provider.requests) == 1


def test_handler_cancellation_is_delivered_and_stops_model() -> None:
    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        raise CancelledError("cancelled")

    provider = _Provider(_completion(_CALL))
    result = _session(provider, handler).run(_INPUT)
    assert result.status == "cancelled"
    assert result.turns[1].message.content.result is not None
    assert isinstance(result.turns[1].message.content.result["failure"], Mapping)
    assert (
        result.turns[1].message.content.result["failure"]["certainty"]
        == "unknown_outcome"
    )
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "error", [ValueError("malformed model result"), OSError("unrelated IO fault")]
)
def test_unexpected_provider_fault_propagates(error: Exception) -> None:
    provider = _Provider(error)
    with pytest.raises(type(error), match=str(error)):
        _session(provider, lambda _, context: ToolExecution(ok=True)).run(_INPUT)


def test_reported_tool_failure_cannot_be_terminal() -> None:
    provider = _Provider(_completion(_CALL), _completion(content="Recovered"))
    result = _session(
        provider,
        lambda _, context: ToolExecution(
            ok=False, failure={"kind": "rejected"}, terminal=True
        ),
    ).run(_INPUT)

    assert result.content == "Recovered"
    payload = provider.requests[1][-1].content.result
    assert payload is not None
    failure = payload["failure"]
    assert isinstance(failure, Mapping)
    assert failure["certainty"] == "observed_failure"
    assert failure["retry_safe"] is False


def test_turn_metadata_uses_wall_clock_start_and_monotonic_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 10, 3, tzinfo=UTC)
    # A backwards wall-clock jump must not affect elapsed durations.
    wall_times = iter(
        (start, start, start, start - timedelta(hours=1), start, start, start)
    )
    elapsed = iter((20.0, 20.25))
    monkeypatch.setattr(
        "thesis_runtime.sessions.session.monotonic", lambda: next(elapsed)
    )
    model_elapsed = iter((10.0, 10.0, 10.0, 10.125, 30.0, 30.0, 30.0, 30.5))
    monkeypatch.setattr(
        "thesis_runtime.sessions.model_invocation.monotonic",
        lambda: next(model_elapsed),
    )
    provider = _Provider(
        _completion(ToolCall(id="arguments", name="work", arguments='{"value":42}')),
        _completion(content="Finished"),
    )
    session = _session(provider, lambda _, context: ToolExecution(ok=True))
    monkeypatch.setattr(session, "_clock", lambda: next(wall_times))
    published: list[SessionTurn] = []
    result = session.run(_INPUT, on_turn=published.append)

    assert tuple(published) == result.turns
    assert [turn.started_at for turn in result.turns] == [
        start,
        start - timedelta(hours=1),
        start,
    ]
    assert [turn.duration_ms for turn in result.turns] == [125, 250, 500]
    assert [turn.arguments for turn in result.turns] == [None, {"value": 42}, None]
    assert SessionTurn(message=_INPUT[0]).started_at is None


@pytest.mark.parametrize("cancel_during_pairing", [False, True])
def test_terminal_completion_pairs_remaining_calls_without_dispatch(
    cancel_during_pairing: bool,
) -> None:
    dispatched: list[Mapping[str, object]] = []
    published: list[SessionTurn] = []
    cancelled = False
    calls = (
        _CALL,
        ToolCall(id="call-2", name="work", arguments='{"unused":true}'),
        ToolCall(id="call-3", name="work", arguments="not json"),
    )

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        dispatched.append(arguments)
        return ToolExecution(ok=True, terminal=True, structured={"accepted": True})

    def on_turn(turn: SessionTurn) -> None:
        nonlocal cancelled
        published.append(turn)
        correlation = turn.message.content.correlation
        if (
            correlation is not None
            and correlation.request_id == "call-2"
            and cancel_during_pairing
        ):
            cancelled = True

    store = _Store()
    provider = _Provider(_completion(*calls))
    result = _session(provider, handler, store=store).run(
        _INPUT, on_turn=on_turn, is_cancelled=lambda: cancelled
    )

    assert result.status == "completed"
    assert result.completion == {"accepted": True}
    assert len(provider.requests) == 1
    assert dispatched == [{}]
    assert tuple(published) == result.turns
    assert [
        turn.message.content.correlation.request_id
        for turn in result.turns[1:]
        if turn.message.content.correlation
    ] == [c.id for c in calls]
    assert result.turns[2].arguments == {"unused": True}
    assert result.turns[3].arguments is None
    for turn in result.turns[2:]:
        payload = turn.message.content.result
        assert payload is not None
        assert isinstance(payload["failure"], Mapping)
        assert payload["ok"] is False
        assert payload["failure"]["kind"] == "skipped"
        assert payload["failure"]["certainty"] == "not_started"
        assert payload["failure"]["retry_safe"] is True
        assert turn.started_at is not None
        assert turn.duration_ms == 0
        invocation = store.get_invocation("workspace", turn.invocation_id or "")
        assert invocation is not None
        assert invocation.state == AgentInvocationState.FAILED

    replay = tuple(turn.message for turn in result.turns)
    recovered = _Provider(_completion(content="Resume"))
    _session(recovered, handler).run((*_INPUT, *replay))
    assert recovered.requests[0][-3:] == replay[-3:]


@pytest.mark.parametrize("raw_arguments", ['```json\n{"value":42}\n```', "not json"])
def test_tool_argument_metadata_preserves_recovery_and_invalid_arguments(
    raw_arguments: str,
) -> None:
    provider = _Provider(
        _completion(ToolCall(id="args", name="work", arguments=raw_arguments)),
        _completion(content="Finished"),
    )
    result = _session(provider, lambda _, context: ToolExecution(ok=True)).run(_INPUT)

    assert result.turns[0].message.content.requests[0].arguments == raw_arguments
    assert result.turns[1].arguments == (
        None if raw_arguments == "not json" else {"value": 42}
    )
    assert result.turns[1].started_at is not None
    assert result.turns[1].duration_ms is not None
