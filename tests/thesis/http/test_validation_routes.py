"""Focused tests for the hypothesis-validation HTTP surface and chat trigger.

A deterministic fake runtime is installed on ``app.state.workflow_runtime`` so
the launch route can exercise the full transport path without a live model or
harness process.  The fake still launches a real workflow execution through
:class:`~thesis.chat.service.ChatService`, so the read routes and the
chat-triggered execution cover the durable execution record and its events.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from thesis.chat.contracts import ChatMessage, MessageRole
from thesis.chat.responder import ChatReplyService
from thesis.chat.service import ChatService
from thesis.chat.streaming import (
    CHAT_MESSAGE,
    PROVIDER_DELTA,
    InMemoryChatStreamBroker,
)
from thesis.execution.events import ExecutionEventType
from thesis.http.application import create_app
from thesis.strategies.bundles import StrategyBundle
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.hypothesis_validation import (
    DATA_READINESS_CHECKS,
    HYPOTHESIS_VALIDATION_WORKFLOW,
    VERDICT_SUPPORTED,
    Criterion,
    CriterionEvaluation,
    DataReadinessRecord,
    DataWindow,
    HypothesisSpecification,
    HypothesisValidationOutcome,
    HypothesisValidationPhase,
    HypothesisValidationRequest,
    HypothesisVerdict,
    build_hypothesis_evidence_report,
)

WORKSPACE = "workspace-a"
BEARER_TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
CLAIM = "Momentum predicts positive forward returns."


def _bundle() -> StrategyBundle:
    return StrategyBundle.create(
        strategy_id="momentum-probe",
        version="1.0.0",
        source={"strategy.py": "class Strategy:\n    pass\n"},
        tests={"test_strategy.py": "def test_strategy():\n    assert True\n"},
        harness_configuration={"seed": 7, "symbols": ["SPY"]},
    )


def _outcome(workspace_id: str, execution_id: str) -> HypothesisValidationOutcome:
    bundle = _bundle()
    criterion = Criterion(
        metric="sharpe", operator="gte", threshold=1.0, label="Sharpe at least 1.0"
    )
    specification = HypothesisSpecification(
        claim=CLAIM,
        claim_type="predictive",
        variables=("momentum_5d",),
        null_hypothesis="Momentum has no effect.",
        alternative_hypothesis="Momentum increases forward returns.",
        estimand="mean forward return",
        endpoints=("forward_return_5d",),
        data_requirements={"symbols": ["SPY"]},
        criteria=(criterion,),
        bundle=bundle,
    )
    window = DataWindow(
        symbols=("SPY",), start=NOW - timedelta(days=30), end=NOW, as_of=NOW
    )
    readiness = DataReadinessRecord(
        checks={name: True for name in DATA_READINESS_CHECKS}
    )
    backtest = BacktestRunResult(
        job_id="job-1",
        state="completed",
        metrics={"sharpe": 1.4},
        manifest={"manifestId": "manifest-1", "sha256": "c" * 64},
        result_artifacts=[
            {
                "uri": "runs/run-1/fills.csv",
                "sha256": "d" * 64,
                "mediaType": "text/csv",
            }
        ],
    )
    verdict = HypothesisVerdict(
        outcome=VERDICT_SUPPORTED,
        reason="all pre-registered criteria passed",
        evaluations=(
            CriterionEvaluation(
                metric="sharpe",
                operator="gte",
                threshold=1.0,
                observed=1.4,
                passed=True,
            ),
        ),
    )
    report = build_hypothesis_evidence_report(
        workspace_id=workspace_id,
        execution_id=execution_id,
        specification=specification,
        window=window,
        readiness=readiness,
        verdict=verdict,
        backtest=backtest,
    )
    return HypothesisValidationOutcome(
        workspace_id=workspace_id,
        execution_id=execution_id,
        phase=HypothesisValidationPhase.COMPLETED,
        fenced=False,
        transitions=(),
        specification=specification,
        window=window,
        readiness=readiness,
        backtest=backtest,
        verdict=verdict,
        report=report,
    )


class FakeWorkflow:
    """Launch surface that records a real execution through ChatService."""

    def __init__(self, chat: ChatService) -> None:
        self._chat = chat

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        claim: str,
        investigation_id: str | None = None,
    ) -> HypothesisValidationRequest:
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=HYPOTHESIS_VALIDATION_WORKFLOW,
            actor_id=actor_id,
            body=claim,
            investigation_id=investigation_id,
        )
        return HypothesisValidationRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            claim=claim,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )


class FakeRuntime:
    """Deterministic validation runtime returning a canned completed verdict."""

    def __init__(self) -> None:
        self.requests: list[HypothesisValidationRequest] = []

    def build_hypothesis_validation(
        self, *, chat: ChatService, experiment_id: str | None = None
    ) -> FakeWorkflow:
        del experiment_id
        return FakeWorkflow(chat)

    def execute_hypothesis_validation(
        self,
        request: HypothesisValidationRequest,
        *,
        chat: ChatService,
        experiment_id: str | None = None,
    ) -> HypothesisValidationOutcome:
        del chat, experiment_id
        self.requests.append(request)
        return _outcome(request.workspace_id, request.execution_id)


def make_client(*, runtime: FakeRuntime | None) -> TestClient:
    app = create_app(bearer_token=BEARER_TOKEN, workflow_runtime=runtime)
    return TestClient(app)


def test_launch_and_read_validation_record() -> None:
    runtime = FakeRuntime()
    client = make_client(runtime=runtime)

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/validation",
        headers=HEADERS,
        json={"claim": CLAIM},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == WORKSPACE
    assert body["status"] == "completed"
    assert body["verdict"] == "supported"
    assert body["claim"] == CLAIM
    assert body["contract"]["hypothesis"] == CLAIM
    assert body["contract"]["metric"] == "sharpe"
    assert body["contract"]["threshold"] == 1.0
    assert body["contract"]["direction"] == "increase"
    assert body["evidence"][0]["kind"] == "backtest"
    assert body["evidence"][0]["run_id"] == "job-1"
    assert body["report"]["verdict"]["outcome"] == "supported"
    assert len(runtime.requests) == 1

    validation_id = body["id"]
    listed = client.get(f"/v1/workspaces/{WORKSPACE}/validation", headers=HEADERS)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [validation_id]

    detail = client.get(
        f"/v1/workspaces/{WORKSPACE}/validation/{validation_id}", headers=HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["id"] == validation_id
    assert detail.json()["verdict_detail"]["evaluations"][0]["observed"] == 1.4

    missing = client.get(
        f"/v1/workspaces/{WORKSPACE}/validation/val-missing", headers=HEADERS
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "validation_not_found"


def test_validation_routes_fail_closed_without_runtime() -> None:
    client = make_client(runtime=None)

    launched = client.post(
        f"/v1/workspaces/{WORKSPACE}/validation",
        headers=HEADERS,
        json={"claim": CLAIM},
    )
    assert launched.status_code == 503
    assert launched.json()["error"]["code"] == "validation_unavailable"

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/validation", headers=HEADERS)
    assert listed.status_code == 503


def test_chat_message_triggers_execution() -> None:
    runtime = FakeRuntime()
    chat = ChatService(clock=lambda: NOW)
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
        workflow_runtime=runtime,
    )
    thread = chat.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    trigger = json.dumps({"workflow": "hypothesis-validation", "claim": CLAIM})
    chat.create_message(WORKSPACE, thread.chat_id, actor_id="operator", body=trigger)

    message = reply.respond(WORKSPACE, thread.chat_id)

    assert message is not None
    assert message.role is MessageRole.ASSISTANT
    assert "supported" in message.body
    assert len(runtime.requests) == 1
    executions = chat.list_executions(WORKSPACE)
    assert len(executions) == 1
    replay = chat.replay_events(WORKSPACE, executions[0].execution_id)
    assert ExecutionEventType.WORKFLOW_LAUNCHED in [
        event.event_type for event in replay.events
    ]
    frames = broker.read(WORKSPACE, thread.chat_id)
    assert frames[0].event == PROVIDER_DELTA
    assert frames[-2].event == CHAT_MESSAGE
    assert frames[-1].data["messageIdFinal"] == message.message_id


def test_chat_route_message_triggers_execution() -> None:
    runtime = FakeRuntime()
    chat = ChatService(clock=lambda: NOW)
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(()),
        workflow_runtime=runtime,
    )
    app = create_app(
        bearer_token=BEARER_TOKEN,
        chat_service=chat,
        chat_stream_broker=broker,
        chat_reply_service=reply,
    )
    client = TestClient(app)
    created = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats",
        headers=HEADERS,
        json={"title": "Research", "owner_actor_id": "operator"},
    )
    assert created.status_code == 200
    chat_id = created.json()["chat_id"]
    trigger = json.dumps({"workflow": "hypothesis-validation", "claim": CLAIM})

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/chats/{chat_id}/messages",
        headers=HEADERS,
        json={"actor_id": "operator", "body": trigger},
    )

    assert response.status_code == 200
    assert len(runtime.requests) == 1
    messages = chat.list_messages(WORKSPACE, chat_id)
    assert messages[-1].role is MessageRole.ASSISTANT
    assert "supported" in messages[-1].body


def test_ordinary_message_does_not_trigger_execution() -> None:
    runtime = FakeRuntime()
    chat = ChatService(clock=lambda: NOW)
    broker = InMemoryChatStreamBroker()
    reply = ChatReplyService(
        chat_service=chat,
        broker=broker,
        provider=lambda history: iter(("plain reply",)),
        workflow_runtime=runtime,
    )
    thread = chat.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    chat.create_message(WORKSPACE, thread.chat_id, actor_id="operator", body="hello")

    message = reply.respond(WORKSPACE, thread.chat_id)

    assert message is not None
    assert message.body == "plain reply"
    assert runtime.requests == []
    assert chat.list_executions(WORKSPACE) == ()


def test_provider_reply_still_streams_without_runtime() -> None:
    chat = ChatService(clock=lambda: NOW)
    broker = InMemoryChatStreamBroker()

    def stream(history: Sequence[ChatMessage]) -> Iterator[str]:
        del history
        yield "ok"

    reply = ChatReplyService(chat_service=chat, broker=broker, provider=stream)
    thread = chat.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    chat.create_message(WORKSPACE, thread.chat_id, actor_id="operator", body="hello")

    message = reply.respond(WORKSPACE, thread.chat_id)

    assert message is not None
    assert [item.role for item in chat.list_messages(WORKSPACE, thread.chat_id)] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
