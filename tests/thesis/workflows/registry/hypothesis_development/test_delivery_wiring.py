"""Delivery wiring (Part A, Phase 2).

The engine's phase handoffs and its failure datum land through
``Conversations.deliver`` — the one delivery primitive — and therefore carry
delivery-key idempotency: a re-delivered handoff or failure resolves to the
durable message instead of duplicating the turn.
"""

from __future__ import annotations

from pathlib import Path

from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import MessageRole, TranscriptItemKind
from thesis.research.models import ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.research_models import ResearchModelRunResult

from ._support import WORKSPACE, _hypothesis_envelope, _kit, _request


class _CompletedStudyRunner:
    """A research-model port returning one completed study run."""

    def run(self, request: object) -> ResearchModelRunResult:
        del request
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            backtest=BacktestRunResult(
                job_id="study-1",
                state="completed",
                metrics={"sharpe": 1.0, "sample_valid": 1.0},
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


HANDOFF = "Falsification findings: the planned falsifier fired."


def _complete_creation(kit, request: object) -> str:
    """Drive the run into the verdict phase and return the verdict node id."""
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    result = kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )
    assert result["applied"] is True
    active = kit.workflow.active_orchestrators[request.execution_id]
    return active.sub_agent_id


def _handoff_messages(kit, request: object) -> list[object]:
    return [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.data is not None
        and message.data.get("handoffKind") == "falsification_findings"
    ]


def test_phase_handoff_lands_through_the_one_delivery_path(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    verdict_node = _complete_creation(kit, request)

    kit.workflow._post_phase_handoff(request, HANDOFF, kind="falsification_findings")
    first_wake_count = len(kit.woke)
    kit.workflow._post_phase_handoff(request, HANDOFF, kind="falsification_findings")

    messages = _handoff_messages(kit, request)
    assert len(messages) == 1
    message = messages[0]
    assert message.role is MessageRole.SYSTEM
    assert message.kind is TranscriptItemKind.SUB_AGENT
    assert message.body == HANDOFF
    assert message.data["executionId"] == request.execution_id
    assert message.data["handoffKind"] == "falsification_findings"
    assert message.data["phase"] == "verdict"
    assert message.data["subAgentId"] == verdict_node
    # The delivery carries its durable idempotency key.
    assert message.data["deliveryKey"]
    # The re-delivered handoff deduped: one message, one extra wake at most.
    assert len(kit.woke) == first_wake_count


def test_failure_datum_lands_through_the_one_delivery_path(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    kit.workflow.prepare_phase(request)

    kit.workflow.fail_execution(request, reason="provider down")
    kit.workflow.fail_execution(request, reason="provider down")

    failures = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.data is not None
        and message.data.get("datumType") == "workflow_outcome"
        and message.data.get("ok") is False
    ]
    assert len(failures) == 1
    message = failures[0]
    assert message.data["structured"]["executionId"] == request.execution_id
    assert message.data["structured"]["status"] == "failed"
    assert message.data["failure"] == {"kind": "workflow", "message": "provider down"}
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state.value == "failed"


def test_operator_answer_is_not_required_for_the_engine_handoff(
    tmp_path: Path,
) -> None:
    """The engine handoff is an engine delivery, not an operator answer."""
    kit = _kit(
        tmp_path, payloads=[], results=[], research_runner=_CompletedStudyRunner()
    )
    request = _request(kit)
    _complete_creation(kit, request)

    kit.workflow._post_phase_handoff(request, HANDOFF, kind="falsification_findings")

    messages = _handoff_messages(kit, request)
    assert len(messages) == 1
    assert messages[0].actor_id == "thesis-workflow"
    # The delivery is durable in the chat store; the durable node records are
    # untouched by a handoff.
    records = PersistenceSubAgentStore(kit.chat.persistence).list_sub_agents(WORKSPACE)
    assert all(record.status.value != "needs_input" for record in records)
