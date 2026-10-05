"""Durable neutral request evidence precedes execution and survives state updates."""

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from operator import setitem
from typing import Any, cast

import pytest
from azure.cosmos import ContainerProxy
from tests.thesis.cosmos.test_chunks import CHUNK_TYPE, LARGE, LimitedContainer

from thesis.agent.messages import MessageContent, SessionMessage, ToolRequest
from thesis.agent.runtime.inbox import SessionInbox
from thesis.agent.runtime.session import (
    AgentSession,
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.store import InMemorySubAgentStore, PersistenceSubAgentStore
from thesis.chat.contracts import (
    AgentInvocation,
    AgentInvocationKind,
    AgentInvocationState,
    ChatContractError,
)
from thesis.chat.persistence import record_from_payload, record_payload
from thesis.cosmos.records import CosmosChatPersistence
from thesis.cosmos.timeline import CosmosTimeline
from thesis.providers.invocations import ProviderUnreachableError, content_digest


class _Store(InMemorySubAgentStore):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[AgentInvocation] = []

    def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
        self.records.append(invocation)
        return super().save_invocation(invocation)


class _SnapshotProvider:
    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: str,
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


def _session(provider, store: _Store, handler=None) -> AgentSession:
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                {"function": {"name": "work", "parameters": {"type": "object"}}},
                handler,
            )
        ]
        if handler is not None
        else []
    )
    return AgentSession(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="conversation",
        provider=provider,
        registry=registry,
        store=store,
    )


@pytest.mark.parametrize("fails", [False, True])
def test_model_snapshot_is_persisted_before_provider_and_preserved(fails: bool) -> None:
    store = _Store()
    history = (
        SessionMessage(
            "prompt", "instruction", MessageContent("Instructions", "instruction")
        ),
        SessionMessage("old-answer", "outgoing", MessageContent("Old answer", "model")),
        SessionMessage(
            "input",
            "incoming",
            MessageContent("Evidence", "child", result={"values": [1, 2]}),
            7,
        ),
    )
    expected = [
        {
            "message_id": message.message_id,
            "direction": message.direction,
            "position": message.position,
        }
        for message in history
    ]

    class Provider(_SnapshotProvider):
        def complete(
            self, *, messages: Sequence[SessionMessage], **kwargs
        ) -> SessionMessage:
            started = store.records[-1]
            assert started.state == AgentInvocationState.STARTED
            assert started.kind == AgentInvocationKind.MODEL
            assert started.as_dict()["request_messages"] == expected
            audit = self.request_snapshot(messages=messages, **kwargs)
            assert started.as_dict()["provider_request"] == audit
            assert started.input_hash == content_digest(audit)
            assert tuple(messages) == history
            if fails:
                raise ProviderUnreachableError("offline")
            return SessionMessage(
                "answer", "outgoing", MessageContent("Answer", "model")
            )

    result = _session(Provider(), store).run(history)
    assert result.status == ("interrupted" if fails else "completed")
    assert len(store.records) == 2
    started, terminal = store.records
    assert terminal.invocation_id == started.invocation_id
    assert terminal.state == (
        AgentInvocationState.FAILED if fails else AgentInvocationState.SUCCEEDED
    )
    assert terminal.request_messages == started.request_messages
    assert terminal.provider_request == started.provider_request
    assert terminal.input_hash == started.input_hash
    assert record_from_payload(AgentInvocation, record_payload(terminal)) == terminal
    serialized = terminal.as_dict()["request_messages"]
    assert isinstance(serialized, list)
    assert serialized == expected
    assert all("content" not in reference for reference in serialized)


@pytest.mark.parametrize("fails", [False, True])
def test_tool_correlation_and_original_arguments_hash_precede_dispatch(
    fails: bool,
) -> None:
    store = _Store()
    request = ToolRequest("call-" + "x" * 200, "work", '```json\n{"value":42}\n```')
    output = SessionMessage(
        "request-message", "outgoing", MessageContent("", "model", requests=(request,))
    )

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs) -> SessionMessage:
            return output

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        started = store.records[-1]
        assert started.state == AgentInvocationState.STARTED
        assert started.kind == AgentInvocationKind.TOOL
        assert started.request_message_id == output.message_id
        assert started.tool_request_id == request.id
        assert started.input_hash == content_digest(request.as_dict())
        assert arguments == {"value": 42}
        return ToolExecution(
            ok=not fails,
            terminal=not fails,
            failure={"kind": "rejected"} if fails else None,
        )

    session = _session(Provider(), store, handler)
    session._max_tool_iterations = 1
    session.run(())
    started, terminal = store.records[-2:]
    assert (
        terminal.request_message_id == started.request_message_id == output.message_id
    )
    assert terminal.tool_request_id == started.tool_request_id == request.id
    assert terminal.input_hash == started.input_hash
    assert terminal.provider_request is None
    assert record_from_payload(AgentInvocation, record_payload(terminal)) == terminal


def test_request_snapshot_is_deeply_immutable_and_detached() -> None:
    values = {"content": {"result": {"values": [1, 2]}}}
    invocation = AgentInvocation(
        workspace_id="workspace",
        created_at=datetime.now(UTC),
        created_by="agent",
        invocation_id="invocation",
        chat_id="chat",
        conversation_id="conversation",
        kind=AgentInvocationKind.MODEL,
        request_messages=(values,),
        provider_request={"projection": "test-neutral", "messages": [values]},
    )
    values["content"]["result"]["values"].append(3)
    assert invocation.as_dict()["request_messages"] == [
        {"content": {"result": {"values": [1, 2]}}}
    ]
    with pytest.raises(TypeError):
        setitem(
            cast(MutableMapping[str, object], invocation.request_messages[0]),
            "content",
            {},
        )
    assert invocation.as_dict()["provider_request"] == {
        "projection": "test-neutral",
        "messages": [{"content": {"result": {"values": [1, 2]}}}],
    }
    assert invocation.provider_request is not None
    messages = invocation.provider_request["messages"]
    assert isinstance(messages, tuple)
    with pytest.raises(TypeError):
        setitem(cast(MutableMapping[str, object], messages[0]), "content", {})


def test_legacy_invocation_decode_defaults_new_snapshot_fields() -> None:
    invocation = AgentInvocation(
        workspace_id="workspace",
        created_at=datetime.now(UTC),
        created_by="agent",
        invocation_id="invocation",
        chat_id="chat",
        conversation_id="conversation",
        kind=AgentInvocationKind.MODEL,
    )
    payload = record_payload(invocation)
    for key in (
        "request_messages",
        "provider_request",
        "request_message_id",
        "tool_request_id",
    ):
        del payload[key]
    assert record_from_payload(AgentInvocation, payload) == invocation
    with pytest.raises(ChatContractError, match="finite JSON"):
        replace(invocation, request_messages=({"bad": float("nan")},))
    with pytest.raises(ChatContractError, match="finite JSON"):
        replace(invocation, provider_request={"bad": float("nan")})


def test_output_persistence_failure_leaves_inputs_pending_and_skips_tools() -> None:
    store = _Store()
    initial = SessionMessage("input", "incoming", MessageContent("Work", "user"), 1)
    inbox = SessionInbox((initial,), pending_ids={initial.message_id})
    dispatched: list[Mapping[str, object]] = []
    acknowledged: list[Sequence[SessionMessage]] = []

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs) -> SessionMessage:
            return SessionMessage(
                "output",
                "outgoing",
                MessageContent(
                    "", "model", requests=(ToolRequest("call", "work", "{}"),)
                ),
            )

    def broken(turn) -> None:
        raise OSError("output persistence unavailable")

    with pytest.raises(OSError, match="output persistence unavailable"):
        _session(Provider(), store, lambda arguments: dispatched.append(arguments)).run(
            (), inbox=inbox, on_turn=broken, on_consumed=acknowledged.append
        )
    assert inbox.snapshot() == ((initial,), (initial,))
    assert dispatched == []
    assert acknowledged == []
    assert all(record.kind == AgentInvocationKind.MODEL for record in store.records)


def test_started_snapshot_write_failure_prevents_provider_execution() -> None:
    class BrokenStore(_Store):
        def save_invocation(self, invocation: AgentInvocation) -> AgentInvocation:
            assert invocation.state == AgentInvocationState.STARTED
            assert invocation.request_messages
            raise OSError("snapshot persistence unavailable")

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs) -> SessionMessage:
            pytest.fail("provider must not execute without its durable snapshot")

    initial = SessionMessage("input", "incoming", MessageContent("Work", "user"))
    inbox = SessionInbox((initial,), pending_ids={"input"})
    with pytest.raises(OSError, match="snapshot persistence unavailable"):
        _session(Provider(), BrokenStore()).run((), inbox=inbox)
    assert inbox.snapshot() == ((initial,), (initial,))


def test_output_persistence_precedes_acknowledgement_and_dispatch() -> None:
    store = _Store()
    initial = SessionMessage("input", "incoming", MessageContent("Work", "user"))
    inbox = SessionInbox((initial,), pending_ids={"input"})
    stages: list[str] = []

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs) -> SessionMessage:
            stages.append("provider")
            return SessionMessage(
                "output",
                "outgoing",
                MessageContent(
                    "", "model", requests=(ToolRequest("call", "work", "{}"),)
                ),
            )

    def output_writer(turn) -> None:
        if turn.message.direction == "outgoing":
            assert inbox.pending() == (initial,)
            stages.append("output persisted")

    def acknowledge(messages: Sequence[SessionMessage]) -> None:
        assert stages == ["provider", "output persisted"]
        stages.append("acknowledged")

    def handler(arguments: Mapping[str, object]) -> ToolExecution:
        assert inbox.pending() == ()
        assert stages == ["provider", "output persisted", "acknowledged"]
        stages.append("dispatch")
        return ToolExecution(ok=True, terminal=True)

    _session(Provider(), store, handler).run(
        (), inbox=inbox, on_turn=output_writer, on_consumed=acknowledge
    )
    assert stages == ["provider", "output persisted", "acknowledged", "dispatch"]


def test_audit_persists_exact_advertised_catalog_before_execution() -> None:
    store = _Store()

    class Provider(_SnapshotProvider):
        def complete(
            self, *, messages: Sequence[SessionMessage], **kwargs
        ) -> SessionMessage:
            audit = self.request_snapshot(messages=messages, **kwargs)
            assert store.records[-1].as_dict()["provider_request"] == audit
            assert store.records[-1].input_hash == content_digest(audit)
            assert audit["tools"] == [
                {"function": {"name": "work", "parameters": {"type": "object"}}}
            ]
            return SessionMessage(
                "answer", "outgoing", MessageContent("Answer", "model")
            )

    _session(Provider(), store, lambda _: ToolExecution(ok=True)).run(())
    record = store.records[-1]
    assert record.provider_request is not None
    changed = record.as_dict()["provider_request"]
    assert isinstance(changed, dict)
    changed["tools"] = None
    assert content_digest(changed) != record.input_hash


def test_provider_mutation_cannot_change_started_or_terminal_audit() -> None:
    store = _Store()
    audit = {
        "projection": "test-neutral",
        "messages": [],
        "tools": [{"name": "original"}],
    }
    expected_hash = content_digest(audit)

    class Provider(_SnapshotProvider):
        def request_snapshot(self, **kwargs) -> Mapping[str, object]:
            return audit

        def complete(self, **kwargs) -> SessionMessage:
            audit["tools"] = []
            return SessionMessage(
                "answer", "outgoing", MessageContent("Answer", "model")
            )

    _session(Provider(), store).run(())
    for record in store.records:
        assert record.input_hash == expected_hash
        assert record.as_dict()["provider_request"] == {
            "projection": "test-neutral",
            "messages": [],
            "tools": [{"name": "original"}],
        }


@pytest.mark.parametrize("fault", ["exception", "non_mapping", "non_json"])
def test_invalid_audit_prevents_invocation_and_provider_execution(fault: str) -> None:
    store = _Store()

    class Provider(_SnapshotProvider):
        def request_snapshot(self, **kwargs):
            if fault == "exception":
                raise ValueError("projection failed")
            if fault == "non_mapping":
                return []
            return {"bad": object()}

        def complete(self, **kwargs) -> SessionMessage:
            pytest.fail("provider must not execute without a valid request audit")

    with pytest.raises((TypeError, ValueError)):
        _session(Provider(), store).run(())
    assert store.records == []


def test_empty_completion_preserves_failed_invocation_audit_without_consumption() -> (
    None
):
    store = _Store()
    initial = SessionMessage("input", "incoming", MessageContent("Work", "user"))
    inbox = SessionInbox((initial,), pending_ids={initial.message_id})

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs) -> SessionMessage:
            return SessionMessage("empty", "outgoing", MessageContent("", "model"))

    result = _session(Provider(), store).run((), inbox=inbox)
    assert result.status == "interrupted"
    started, failed = store.records
    assert failed.state == AgentInvocationState.FAILED
    assert failed.provider_request == started.provider_request
    assert failed.request_messages == started.request_messages
    assert failed.input_hash == started.input_hash
    assert failed.failure == result.failure
    assert inbox.snapshot() == ((initial,), (initial,))


@pytest.mark.parametrize("failure", [None, "chunk", "record"])
def test_large_cosmos_provider_audit_is_durable_before_provider_execution(
    failure: str | None,
) -> None:
    container = LimitedContainer("workspaceId")
    persistence = CosmosChatPersistence(
        cast(ContainerProxy, container),
        timeline=CosmosTimeline(cast(ContainerProxy, container)),
    )
    store = PersistenceSubAgentStore(persistence)
    if failure == "chunk":
        container.fail_chunk_after = 1
    elif failure == "record":
        container.fail_record = True
    calls = []
    audit = {"messages": [{"content": LARGE}], "tools": [{"schema": LARGE}]}

    class Provider(_SnapshotProvider):
        def request_snapshot(self, **kwargs):
            return audit

        def complete(self, **kwargs):
            calls.append("provider")
            started = persistence.list(AgentInvocation, "workspace")[0]
            assert started.state == AgentInvocationState.STARTED
            assert started.as_dict()["provider_request"] == audit
            assert started.as_dict()["request_messages"] == [
                {"message_id": "input", "direction": "incoming", "position": 4}
            ]
            return SessionMessage(
                "answer", "outgoing", MessageContent("Answer", "model")
            )

    initial = SessionMessage("input", "incoming", MessageContent(LARGE, "user"), 4)
    session = AgentSession(
        workspace_id="workspace",
        chat_id="chat",
        conversation_id="conversation",
        provider=Provider(),
        registry=SessionToolRegistry(),
        store=store,
    )
    if failure:
        with pytest.raises(RuntimeError, match="write failed"):
            session.run((initial,))
        assert not calls
        assert not persistence.list(AgentInvocation, "workspace")
    else:
        assert session.run((initial,)).status == "completed"
        assert calls == ["provider"]
        terminal = persistence.list(AgentInvocation, "workspace")[0]
        assert terminal.as_dict()["provider_request"] == audit
        assert all("content" not in item for item in terminal.request_messages)
        records = [
            item for item in container.writes if item["recordType"] == "AgentInvocation"
        ]
        assert (
            records[0]["payload"]["provider_request"]
            == records[1]["payload"]["provider_request"]
        )
        first_record = container.writes.index(records[0])
        assert all(
            item["recordType"] != CHUNK_TYPE for item in container.writes[first_record:]
        )


def test_tool_outcome_keeps_one_large_payload_and_explicit_correlation() -> None:
    store = _Store()
    structured = {"raw": LARGE}
    import json

    text = json.dumps(structured)

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs):
            return SessionMessage(
                "request",
                "outgoing",
                MessageContent(
                    "", "model", requests=(ToolRequest("call", "work", "{}"),)
                ),
            )

    session = _session(
        Provider(),
        store,
        lambda _: ToolExecution(
            ok=False,
            structured=structured,
            text=text,
            failure={"kind": "harness", "payload": structured},
        ),
    )
    session._max_tool_iterations = 1
    result = session.run(())
    outcome = result.turns[-1].message
    assert outcome.content.source == "tool"
    assert outcome.content.correlation is not None
    assert outcome.content.correlation.request_message_id == "request"
    payload = cast(dict[str, Any], outcome.content.as_dict()["result"])
    assert "structured" not in payload
    assert "payload" not in payload["failure"]
    assert payload["datums"][0]["structured"] == structured
    assert payload["datums"][0]["text"] == ""
    assert "payload" not in payload["datums"][0]["failure"]
    assert len(json.dumps(outcome.as_dict())) < len(text) + 2000


def test_compaction_preserves_distinct_tool_text_failure_evidence_and_pure_projection() -> (
    None
):
    from thesis.agent.runtime.datum import Datum
    from thesis.providers.session_adapter import project_native_messages

    store = _Store()
    text = "Distinct textual evidence " * 100
    requested = SessionMessage(
        "request",
        "outgoing",
        MessageContent("", "model", requests=(ToolRequest("call", "work", "{}"),)),
    )

    class Provider(_SnapshotProvider):
        def complete(self, **kwargs):
            return requested

    session = _session(
        Provider(),
        store,
        lambda _: ToolExecution(
            ok=False,
            text=text,
            failure={"kind": "harness", "payload": {"distinct": True}},
            datums=(Datum("first", structured={"receipt": "one"}),),
        ),
    )
    session._max_tool_iterations = 1
    result = session.run(())
    delivered = result.turns[-1].message
    assert delivered.content.result is not None
    failure = cast(Mapping[str, object], delivered.content.result["failure"])
    assert failure["payload"] == {"distinct": True}
    assert any(datum.text == text for datum in result.turns[-1].datums)
    replayed = SessionMessage.from_dict(delivered.as_dict())
    assert project_native_messages((requested, delivered)) == project_native_messages(
        (requested, replayed)
    )
    assert project_native_messages((requested, delivered))[-1].tool_call_id == "call"
