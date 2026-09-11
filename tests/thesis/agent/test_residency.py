from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.operations import (
    EditRecord,
    InvestigationSession,
    OperationOutcome,
    OperationReceipt,
    WorkspaceOperations,
)
from thesis.agent.residency import (
    AgentResidency,
    AgentSession,
    OperationAuditRecord,
    OperationKind,
    ResidencyError,
    resume,
)
from thesis.agent.router import (
    ModelProfile,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry, ThesisSkill, content_hash
from thesis.chat.contracts import ChatThread
from thesis.chat.service import ChatService

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
WORKSPACE = "equity-momentum"
ROOT = "/workspaces/equity-momentum"
REFS = ("experiments/e-1/contract.json", "backtests/b-1/manifest.json")


def _constant_clock() -> datetime:
    return NOW


class TickingClock:
    """Deterministic clock advancing one minute per call."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        current = self._now
        self._now = current + timedelta(minutes=1)
        return current


def _ids() -> Callable[[], str]:
    return iter(("s-1", "1", "2", "3", "4", "5", "6", "7", "8")).__next__


class FakeResidencyStore:
    """In-memory durable store keyed by workspace and agent identity."""

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str], AgentSession] = {}
        self.audit: list[OperationAuditRecord] = []

    def load(self, workspace_id: str, agent_id: str) -> AgentSession | None:
        return self.sessions.get((workspace_id, agent_id))

    def save(self, session: AgentSession) -> AgentSession:
        self.sessions[(session.workspace_id, session.agent_id)] = session
        return session

    def append_audit(self, record: OperationAuditRecord) -> OperationAuditRecord:
        self.audit.append(record)
        return record

    def list_audit(self, workspace_id: str) -> tuple[OperationAuditRecord, ...]:
        return tuple(
            record for record in self.audit if record.workspace_id == workspace_id
        )


def _residency(
    store: FakeResidencyStore | None = None,
    *,
    workspace_id: str = WORKSPACE,
    agent_id: str = "thesis-agent",
    root: str = ROOT,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> AgentResidency:
    return AgentResidency.bind(
        FakeResidencyStore() if store is None else store,
        workspace_id=workspace_id,
        agent_id=agent_id,
        workspace_root=root,
        clock=_constant_clock if clock is None else clock,
        id_factory=_ids() if id_factory is None else id_factory,
    )


def _chat_service(clock: Callable[[], datetime] | None = None) -> ChatService:
    return ChatService(
        clock=_constant_clock if clock is None else clock,
        id_factory=lambda prefix: f"{prefix}-1",
    )


def _router() -> ModelRouter:
    profiles = {
        f"{role}-profile": ModelProfile(
            profile_id=f"{role}-profile",
            version="1.0.0",
            provider="fake",
            model=f"{role}-model",
        )
        for role in ("primary", "coding")
    }
    policies: dict[Role, RolePolicy] = {
        role: RolePolicy(
            role=role,
            version="1.0.0",
            model_profile=f"{role}-profile",
            skills=("shared",),
            allowed_tools=("read_workspace",),
        )
        for role in ("primary", "coding")
    }

    class FakeProvider:
        def invoke(self, **_: object) -> str:
            return "unused"

    return ModelRouter(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": FakeProvider()},
        skills=SkillRegistry(
            (
                ThesisSkill(
                    "shared",
                    "1.0.0",
                    "Use falsifiable reasoning.",
                    ("read_workspace",),
                ),
            )
        ),
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        clock=_constant_clock,
        id_factory=iter(("inv-1",)).__next__,
    )


def _chat_thread(workspace_id: str) -> ChatThread:
    return ChatThread(
        workspace_id=workspace_id,
        created_at=NOW,
        created_by="operator",
        chat_id="chat-1",
        title="Ops thread",
        owner_actor_id="operator",
    )


def test_binding_freezes_the_workspace_identity() -> None:
    residency = _residency()

    assert residency.workspace_id == WORKSPACE
    assert residency.agent_id == "thesis-agent"
    assert residency.context_root == ROOT
    assert residency.working_state_dir == ".thesis/agent-state/thesis-agent"
    assert residency.audit_scope == f"workspace:{WORKSPACE}"
    assert residency.session.transcript_cursor == 0
    assert residency.session.working_state_refs == ()
    assert residency.session.created_at == NOW
    assert residency.session.last_active_at == NOW

    with pytest.raises(ResidencyError, match="frozen at construction"):
        residency.__setattr__("workspace_id", "beta-lab")
    with pytest.raises(ResidencyError, match="frozen at construction"):
        del residency.workspace_id  # type: ignore[attr-defined]


def test_boundary_methods_derive_every_scope_from_the_binding() -> None:
    residency = _residency()

    assert residency.working_state_path() == Path(
        f"{ROOT}/.thesis/agent-state/thesis-agent"
    )
    assert residency.resolve_path("strategies/momentum.py") == Path(
        f"{ROOT}/strategies/momentum.py"
    )
    assert residency.resolve_path(Path("backtests/run-1/report.json")) == Path(
        f"{ROOT}/backtests/run-1/report.json"
    )

    with pytest.raises(ResidencyError, match="traversal"):
        residency.resolve_path("../sibling-workspace/strategies/steal.py")
    with pytest.raises(ResidencyError, match="relative to the context root"):
        residency.resolve_path("C:/escape/strategies/steal.py")
    with pytest.raises(ResidencyError, match="traversal"):
        residency.resolve_path("strategies/../../escape.py")

    with pytest.raises(ResidencyError, match="outside residency"):
        residency.assert_same_workspace("beta-lab")
    residency.assert_same_workspace(WORKSPACE)


def test_session_persistence_round_trip_with_canonical_json_v1_document() -> None:
    store = FakeResidencyStore()
    residency = _residency(store, clock=TickingClock(NOW))

    session = residency.checkpoint(
        transcript_cursor=3,
        working_state_refs=REFS,
    )

    assert session is store.sessions[(WORKSPACE, "thesis-agent")]
    assert store.load(WORKSPACE, "thesis-agent") == session
    assert session.schema_version == 1
    assert session.as_dict() == {
        "agentId": "thesis-agent",
        "createdAt": "2026-09-09T12:00:00Z",
        "lastActiveAt": "2026-09-09T12:01:00Z",
        "parentSessionId": None,
        "schemaVersion": 1,
        "sessionId": "s-1",
        "transcriptCursor": 3,
        "workingStateRefs": list(REFS),
        "workspaceId": WORKSPACE,
    }
    assert session.document_json == json.dumps(
        session.as_dict(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    assert session.document_hash == content_hash(session.document_json)
    rebuilt = AgentSession(
        workspace_id=WORKSPACE,
        agent_id="thesis-agent",
        session_id="s-1",
        created_at=NOW,
        last_active_at=NOW + timedelta(minutes=1),
        transcript_cursor=3,
        working_state_refs=REFS,
    )
    assert session.document_json == rebuilt.document_json

    with pytest.raises(ResidencyError, match="non-decreasing"):
        residency.checkpoint(transcript_cursor=2)


def test_resume_restores_last_session_state_across_a_restart() -> None:
    store = FakeResidencyStore()
    clock = TickingClock(NOW)
    first = resume(
        store,
        WORKSPACE,
        "thesis-agent",
        workspace_root=ROOT,
        clock=clock,
        id_factory=iter(("s-1", "1")).__next__,
    )
    first.checkpoint(transcript_cursor=7, working_state_refs=REFS)

    restored = resume(
        store,
        WORKSPACE,
        "thesis-agent",
        workspace_root=ROOT,
        clock=clock,
        id_factory=iter(("s-2", "2")).__next__,
    )

    assert restored is not first
    assert restored.workspace_id == WORKSPACE
    assert restored.agent_id == "thesis-agent"
    assert restored.session.session_id == "s-1"
    assert restored.session.created_at == NOW
    assert restored.session.last_active_at == NOW + timedelta(minutes=1)
    assert restored.session.transcript_cursor == 7
    assert restored.session.working_state_refs == REFS

    advanced = restored.checkpoint(transcript_cursor=9)
    assert advanced.transcript_cursor == 9
    assert advanced.last_active_at == NOW + timedelta(minutes=2)
    with pytest.raises(ResidencyError, match="non-decreasing"):
        restored.checkpoint(transcript_cursor=8)


def test_resume_creates_fresh_session_and_rejects_corrupt_state() -> None:
    store = FakeResidencyStore()
    fresh = resume(
        store,
        "beta-lab",
        "thesis-agent",
        clock=_constant_clock,
        id_factory=iter(("s-1",)).__next__,
    )

    assert fresh.session.session_id == "s-1"
    assert fresh.session.transcript_cursor == 0
    assert fresh.session.created_at == NOW
    assert fresh.context_root == "workspace://beta-lab"

    store.sessions[(WORKSPACE, "thesis-agent")] = AgentSession(
        workspace_id="beta-lab",
        agent_id="thesis-agent",
        session_id="s-9",
        created_at=NOW,
        last_active_at=NOW,
    )
    with pytest.raises(ResidencyError, match="does not match the binding"):
        resume(store, WORKSPACE, "thesis-agent", clock=_constant_clock)


def test_every_operation_emits_an_audit_record_with_exact_fields() -> None:
    residency = _residency()
    operations = WorkspaceOperations(residency, chat=_chat_service(), router=_router())

    opened = operations.open_investigation("Test a short momentum claim.")
    record = residency.audit_trail()[-1]
    assert record.operation_id == "op-1"
    assert record.workspace_id == WORKSPACE
    assert record.operation == "open_investigation"
    assert record.status == "recorded"
    assert record.inputs_digest == content_hash(
        {"topic": "Test a short momentum claim."}
    )
    assert record.created_at == NOW
    assert record.recorded_at == NOW
    assert record.session_id == "s-1"
    assert record.reason is None
    assert record.route_ref == "primary@1.0.0"
    assert record.detail_ref == opened.investigation_id

    edited = operations.edit_strategy_code("strategies/momentum.py", "def run(): ...")
    record = residency.audit_trail()[-1]
    assert record.operation_id == "op-2"
    assert record.workspace_id == WORKSPACE
    assert record.operation == "edit_strategy_code"
    assert record.status == "unavailable"
    assert record.inputs_digest == content_hash(
        {
            "sourceSha256": content_hash("def run(): ..."),
            "strategyRef": "strategies/momentum.py",
        }
    )
    assert record.reason == "strategy_editing_service_unavailable"
    assert record.route_ref == "coding@1.0.0"
    assert record.detail_ref is None

    assert [item.operation_id for item in residency.audit_trail()] == ["op-1", "op-2"]
    assert opened.state == "recorded"
    assert isinstance(edited, EditRecord)


def test_unavailable_operations_fail_closed_without_fabricated_results() -> None:
    residency = _residency()
    operations = WorkspaceOperations(residency)

    edited = operations.edit_strategy_code("strategies/momentum.py", "def run(): ...")
    assert edited.state == "unavailable"
    assert edited.reason == "strategy_editing_service_unavailable"
    assert edited.source_sha256 == content_hash("def run(): ...")
    assert edited.workspace_id == WORKSPACE

    receipt = operations.request_backtest("backtests/config-1.json")
    assert isinstance(receipt, OperationReceipt)
    assert receipt.state == "unavailable"
    assert receipt.reason == "backtest_service_unavailable"
    assert receipt.config_ref == "backtests/config-1.json"

    outcome = operations.deploy_paper("consent: confirm paper deployment")
    assert isinstance(outcome, OperationOutcome)
    assert outcome.state == "unavailable"
    assert outcome.reason == "deployment_gatekeeper_unavailable"
    assert outcome.consent_recorded is True

    opened = operations.open_investigation("Test a mean-reversion claim.")
    assert isinstance(opened, InvestigationSession)
    assert opened.state == "unavailable"
    assert opened.reason == "chat_service_unavailable"
    assert opened.investigation_id is None

    assert [record.status for record in residency.audit_trail()] == [
        "unavailable",
        "unavailable",
        "unavailable",
        "unavailable",
    ]
    with pytest.raises(ResidencyError, match="consent_confirmation"):
        operations.deploy_paper("   ")
    with pytest.raises(ResidencyError, match="new_source"):
        operations.edit_strategy_code("strategies/momentum.py", "")
    with pytest.raises(ResidencyError, match="safe"):
        operations.request_backtest("../escape/config.json")


def test_open_investigation_composes_the_chat_service_within_the_binding() -> None:
    chat = _chat_service()
    residency = _residency()
    operations = WorkspaceOperations(residency, chat=chat)

    opened = operations.open_investigation("Test a short momentum claim.")

    stored = chat.list_investigations(WORKSPACE)
    assert len(stored) == 1
    investigation = stored[0]
    assert opened.investigation_id == investigation.investigation_id
    assert investigation.workspace_id == WORKSPACE
    assert investigation.objective == "Test a short momentum claim."
    assert investigation.created_by == "thesis-agent"
    assert opened.state == "recorded"

    with pytest.raises(ResidencyError, match="outside residency"):
        operations.bind_chat(_chat_thread("beta-lab"))
    assert operations.bind_chat(_chat_thread(WORKSPACE)).workspace_id == WORKSPACE


def test_all_records_serialize_deterministically() -> None:
    def run() -> dict[str, str]:
        store = FakeResidencyStore()
        residency = _residency(store)
        operations = WorkspaceOperations(
            residency, chat=_chat_service(), router=_router()
        )
        opened = operations.open_investigation("Test a short momentum claim.")
        edited = operations.edit_strategy_code(
            "strategies/momentum.py", "def run(): ..."
        )
        receipt = operations.request_backtest("backtests/config-1.json")
        outcome = operations.deploy_paper("consent: confirm paper deployment")
        checkpoint = residency.checkpoint(
            transcript_cursor=3,
            working_state_refs=REFS,
        )
        return {
            "checkpoint": checkpoint.document_json,
            "session": residency.session.document_json,
            "audit": json.dumps(
                [record.as_dict() for record in residency.audit_trail()],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "opened": json.dumps(opened.as_dict(), sort_keys=True),
            "edited": json.dumps(edited.as_dict(), sort_keys=True),
            "receipt": json.dumps(receipt.as_dict(), sort_keys=True),
            "outcome": json.dumps(outcome.as_dict(), sort_keys=True),
        }

    first = run()
    second = run()
    assert first == second
    assert json.loads(first["checkpoint"]) == json.loads(first["session"])


def test_corrupt_audit_inputs_are_rejected_by_the_store_boundary() -> None:
    residency = _residency()

    with pytest.raises(ResidencyError, match="unsupported operation kind"):
        residency.record_audit(
            operation_id="op-9",
            operation=cast("OperationKind", "close_investigation"),
            status="recorded",
            inputs_digest="0" * 64,
            created_at=NOW,
        )
    with pytest.raises(ResidencyError, match="requires a reason"):
        residency.record_audit(
            operation_id="op-9",
            operation="request_backtest",
            status="unavailable",
            inputs_digest="0" * 64,
            created_at=NOW,
        )
    with pytest.raises(ResidencyError, match="carries no reason"):
        residency.record_audit(
            operation_id="op-9",
            operation="request_backtest",
            status="recorded",
            inputs_digest="0" * 64,
            created_at=NOW,
            reason="recorded",
        )
    with pytest.raises(ResidencyError, match="cannot precede"):
        residency.record_audit(
            operation_id="op-9",
            operation="request_backtest",
            status="recorded",
            inputs_digest="0" * 64,
            created_at=NOW + timedelta(minutes=1),
        )
