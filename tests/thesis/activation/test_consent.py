"""Consent, activation-request, and gatekeeper-boundary behaviour tests.

All timestamps are fixed and injected; the gatekeeper is an in-memory fake.
No test touches a network or any credential store.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from thesis.activation import (
    DEFAULT_ACTIONS_DISABLED,
    ActivationRequest,
    ActivationRequestError,
    ActivationState,
    ActivationStatus,
    ConsentConfirmation,
    ConsentError,
    ConsentGrant,
    ConsentProposal,
    GatekeeperClient,
    GatekeeperDecision,
    GatekeeperDecisionKind,
    GatekeeperProtocolError,
    GatekeeperRequest,
    GatekeeperRequestError,
    GrantAction,
    GrantEnvironment,
    GrantLedger,
    GrantScope,
    GrantScopeKind,
    build_activation_request,
    confirm_grant,
    interval_scope,
    is_active,
    one_shot_scope,
    propose_grant,
    revoke,
    validate_for_grant,
)

BUNDLE_HASH = "cd" * 32
CONFIRMATION_TOKEN = "confirm-20260908-abcd1234"

NAIVE_NOON = datetime(2026, 9, 8, 12, 0, 0)
PROPOSED_AT = datetime(2026, 9, 8, 11, 55, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 8, 12, 1, 0, tzinfo=UTC)
INTERVAL_END = datetime(2026, 9, 8, 12, 30, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 9, 8, 13, 0, 0, tzinfo=UTC)

_CREDENTIAL_PATTERN = re.compile(
    r"token|secret|password|credential|api[_-]?key|auth",
    re.IGNORECASE,
)


def _wrong(value: object) -> Any:
    """Pass a deliberately wrong value through the typed surface at runtime."""
    return cast(Any, value)


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _proposal(**overrides: Any) -> ConsentProposal:
    values: dict[str, Any] = {
        "bundle_hash": BUNDLE_HASH,
        "environment": GrantEnvironment.PROD,
        "action": GrantAction.PAPER,
        "instruments": ("AAPL", "MSFT"),
        "capital_bound": Decimal("100000"),
        "risk_bound": Decimal("2500"),
        "expiration": EXPIRES_AT,
        "scope": one_shot_scope(),
        "proposed_at": PROPOSED_AT,
    }
    values.update(overrides)
    return propose_grant(**values)


def _confirmation(**overrides: Any) -> ConsentConfirmation:
    values: dict[str, Any] = {
        "bundle_hash": BUNDLE_HASH,
        "environment": GrantEnvironment.PROD,
        "action": GrantAction.PAPER,
        "confirmation_token": CONFIRMATION_TOKEN,
    }
    values.update(overrides)
    return ConsentConfirmation(**values)


def _grant(**overrides: Any) -> ConsentGrant:
    proposal = _proposal(**overrides)
    confirmation = ConsentConfirmation(
        bundle_hash=proposal.bundle_hash,
        environment=proposal.environment,
        action=proposal.action,
        confirmation_token=CONFIRMATION_TOKEN,
    )
    return confirm_grant(proposal, confirmation, created_at=CREATED_AT)


def _request(grant: ConsentGrant, **overrides: Any) -> ActivationRequest:
    values: dict[str, Any] = {
        "request_ref": "req-0001",
        "grant_id": grant.grant_id,
        "bundle_hash": grant.bundle_hash,
        "environment": grant.environment,
        "action": grant.action,
        "instruments": grant.instruments,
        "requested_at": CREATED_AT,
    }
    values.update(overrides)
    return ActivationRequest(**values)


class FakeGatekeeperEndpoint:
    """In-memory gatekeeper endpoint: records requests, returns decisions."""

    def __init__(self, *, approved: bool = True, reason: str = "") -> None:
        self.submitted: list[GatekeeperRequest] = []
        self.polled: list[str] = []
        self.approved = approved
        self.reason = reason
        self.poll_decisions: dict[str, GatekeeperDecision] = {}

    def submit(self, request: GatekeeperRequest) -> GatekeeperDecision:
        self.submitted.append(request)
        if self.approved:
            return GatekeeperDecision(
                request_ref=request.request_ref,
                decision=GatekeeperDecisionKind.APPROVED,
            )
        return GatekeeperDecision(
            request_ref=request.request_ref,
            decision=GatekeeperDecisionKind.REJECTED,
            reason=self.reason or "policy violation",
        )

    def poll(self, request_ref: str) -> GatekeeperDecision:
        self.polled.append(request_ref)
        decision = self.poll_decisions.get(request_ref)
        if decision is not None:
            return decision
        return GatekeeperDecision(
            request_ref=request_ref,
            decision=GatekeeperDecisionKind.PENDING,
        )


def test_paper_shadow_live_default_disabled() -> None:
    """AC3: paper, shadow, and live are all disabled before consent exists."""
    assert isinstance(DEFAULT_ACTIONS_DISABLED, Mapping)
    assert set(DEFAULT_ACTIONS_DISABLED) == set(GrantAction)
    assert all(enabled is False for enabled in DEFAULT_ACTIONS_DISABLED.values())


def test_consent_surface_accepts_no_free_text() -> None:
    """AC1: there is structurally no free-text path into a grant."""
    assert set(inspect.signature(propose_grant).parameters) == {
        "bundle_hash",
        "environment",
        "action",
        "instruments",
        "capital_bound",
        "risk_bound",
        "expiration",
        "scope",
        "proposed_at",
    }
    assert set(inspect.signature(confirm_grant).parameters) == {
        "proposal",
        "confirmation",
        "created_at",
    }
    ambiguous = "yes please, go ahead and activate it"
    with pytest.raises(ConsentError):
        _proposal(environment=_wrong(ambiguous))
    with pytest.raises(ConsentError):
        _proposal(action=_wrong(ambiguous))
    with pytest.raises(ConsentError):
        _proposal(bundle_hash=_wrong(ambiguous))
    with pytest.raises(ConsentError):
        _confirmation(confirmation_token=_wrong(ambiguous))
    with pytest.raises(ConsentError):
        _confirmation(bundle_hash=_wrong(ambiguous))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bundle_hash", _wrong("CD" * 32)),
        ("bundle_hash", _wrong("cd" * 31)),
        ("bundle_hash", _wrong("not-a-digest")),
        ("environment", _wrong("production")),
        ("action", _wrong("live")),
        ("instruments", _wrong([])),
        ("instruments", _wrong(["AAPL", "DROP TABLE"])),
        ("capital_bound", _wrong(0)),
        ("capital_bound", _wrong(Decimal("0"))),
        ("capital_bound", _wrong(Decimal("-2500"))),
        ("capital_bound", _wrong(100000.0)),
        ("capital_bound", _wrong("100000")),
        ("capital_bound", _wrong(Decimal("NaN"))),
        ("risk_bound", _wrong(0)),
        ("risk_bound", _wrong(float("nan"))),
        ("expiration", _wrong(NAIVE_NOON)),
        ("expiration", _wrong("2026-09-08T13:00:00Z")),
        ("scope", _wrong("one shot")),
        ("proposed_at", _wrong(NAIVE_NOON)),
    ],
)
def test_grant_validation_matrix(field: str, value: object) -> None:
    with pytest.raises(ConsentError):
        _proposal(**{field: value})


def test_scope_and_time_boundaries_are_enforced() -> None:
    with pytest.raises(ConsentError):
        GrantScope(kind=GrantScopeKind.ONE_SHOT, interval_end=EXPIRES_AT)
    with pytest.raises(ConsentError):
        interval_scope(NAIVE_NOON)
    with pytest.raises(ConsentError):
        _proposal(scope=interval_scope(EXPIRES_AT + timedelta(minutes=1)))
    with pytest.raises(ConsentError):
        confirm_grant(_proposal(), _confirmation(), created_at=EXPIRES_AT)


def test_confirmation_must_restate_proposal_exactly() -> None:
    with pytest.raises(ConsentError):
        confirm_grant(
            _proposal(),
            _confirmation(bundle_hash="ab" * 32),
            created_at=CREATED_AT,
        )
    with pytest.raises(ConsentError):
        confirm_grant(
            _proposal(),
            _confirmation(environment=GrantEnvironment.DEV),
            created_at=CREATED_AT,
        )
    with pytest.raises(ConsentError):
        confirm_grant(
            _proposal(),
            _confirmation(action=GrantAction.LIVE),
            created_at=CREATED_AT,
        )


def test_revocation_enforcement() -> None:
    """AC3: a revoked grant fails activation checks."""
    grant = _grant()
    request = _request(grant)
    ledger = GrantLedger()
    revocation = revoke(grant, at=LATER, reason="operator paused the program")
    ledger.record_revocation(revocation)
    with pytest.raises(ActivationRequestError):
        validate_for_grant(
            request, grant, at=LATER + timedelta(minutes=1), ledger=ledger
        )
    assert is_active(grant, LATER, revocations=(revocation,)) is False
    assert is_active(grant, LATER) is True
    assert ledger.is_revoked(grant.grant_id) is True
    assert ledger.revocation(grant.grant_id) is revocation


def test_revocation_requires_reason_and_valid_time() -> None:
    grant = _grant()
    with pytest.raises(ConsentError):
        revoke(grant, at=LATER, reason="   ")
    with pytest.raises(ConsentError):
        revoke(grant, at=PROPOSED_AT, reason="before creation")
    revocation = revoke(grant, at=LATER, reason="  operator paused the program  ")
    assert revocation.reason == "operator paused the program"
    assert json.loads(revocation.canonical_json)["reason"] == (
        "operator paused the program"
    )


def test_one_shot_scope_consumes_grant_on_first_validated_request() -> None:
    grant = _grant()
    ledger = GrantLedger()
    first = validate_for_grant(
        _request(grant, request_ref="req-0001"), grant, at=LATER, ledger=ledger
    )
    assert first.consumed is True
    assert first.request_ref == "req-0001"
    assert ledger.receipt(grant.grant_id) is first
    with pytest.raises(ActivationRequestError):
        validate_for_grant(
            _request(grant, request_ref="req-0002"), grant, at=LATER, ledger=ledger
        )


def test_interval_scope_is_reusable_within_its_window() -> None:
    grant = _grant(scope=interval_scope(INTERVAL_END))
    ledger = GrantLedger()
    first = validate_for_grant(
        _request(grant, request_ref="req-0001"), grant, at=LATER, ledger=ledger
    )
    second = validate_for_grant(
        _request(grant, request_ref="req-0002"),
        grant,
        at=LATER + timedelta(minutes=1),
        ledger=ledger,
    )
    assert first.consumed is False
    assert second.consumed is False
    assert ledger.receipts() == ()
    assert is_active(grant, INTERVAL_END) is False


def test_expired_grant_fails_activation() -> None:
    grant = _grant()
    assert is_active(grant, EXPIRES_AT - timedelta(microseconds=1)) is True
    assert is_active(grant, EXPIRES_AT) is False
    with pytest.raises(ActivationRequestError):
        validate_for_grant(_request(grant), grant, at=EXPIRES_AT, ledger=GrantLedger())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_id", "f" * 64),
        ("bundle_hash", "e" * 64),
        ("environment", GrantEnvironment.DEV),
        ("action", GrantAction.LIVE),
        ("instruments", ("AAPL", "TSLA")),
        ("requested_at", EXPIRES_AT + timedelta(minutes=1)),
    ],
)
def test_request_mismatch_fails_closed(field: str, value: object) -> None:
    grant = _grant()
    with pytest.raises(ActivationRequestError):
        validate_for_grant(
            _request(grant, **{field: value}), grant, at=LATER, ledger=GrantLedger()
        )


def test_request_instruments_may_be_a_subset_of_the_grant() -> None:
    grant = _grant()
    request = build_activation_request(
        grant,
        bundle_hash=grant.bundle_hash,
        instruments=("MSFT",),
        request_ref="req-0001",
        requested_at=CREATED_AT,
    )
    validation = validate_for_grant(request, grant, at=LATER, ledger=GrantLedger())
    assert validation.consumed is True
    assert validation.validated_at == LATER
    assert json.loads(validation.canonical_json)["requestRef"] == "req-0001"


def test_submit_forwards_declarations_and_polls_status() -> None:
    grant = _grant()
    ledger = GrantLedger()
    endpoint = FakeGatekeeperEndpoint()
    client = GatekeeperClient(endpoint, ledger=ledger)
    status = client.submit(
        _request(grant),
        grant,
        at=LATER,
        harness_capability="paper-shell",
        tests_passed=True,
    )
    assert status.state is ActivationState.APPROVED
    assert status.request_ref == "req-0001"
    assert status.reason == ""
    assert len(endpoint.submitted) == 1
    sent = endpoint.submitted[0]
    assert sent.request_ref == "req-0001"
    assert sent.grant_id == grant.grant_id
    assert sent.bundle_hash == grant.bundle_hash
    assert sent.environment is GrantEnvironment.PROD
    assert sent.action is GrantAction.PAPER
    assert sent.instruments == grant.instruments
    assert sent.capital_bound == Decimal("100000")
    assert sent.risk_bound == Decimal("2500")
    assert sent.scope_kind is GrantScopeKind.ONE_SHOT
    assert sent.interval_end is None
    assert sent.harness_capability == "paper-shell"
    assert sent.tests_passed is True
    assert ledger.is_consumed(grant.grant_id) is True
    endpoint.poll_decisions["req-0001"] = GatekeeperDecision(
        request_ref="req-0001",
        decision=GatekeeperDecisionKind.APPROVED,
    )
    assert client.poll("req-0001").state is ActivationState.APPROVED
    assert endpoint.polled == ["req-0001"]


@pytest.mark.parametrize(
    "mode",
    ["revoked", "expired", "mismatched", "capability", "tests_passed"],
)
def test_submit_fails_closed_before_endpoint(mode: str) -> None:
    grant = _grant()
    endpoint = FakeGatekeeperEndpoint()
    ledger = GrantLedger()
    client = GatekeeperClient(endpoint, ledger=ledger)
    request = _request(grant)
    at = LATER
    capability = "paper-shell"
    tests_passed: bool = True
    if mode == "revoked":
        ledger.record_revocation(revoke(grant, at=LATER, reason="withdrawn"))
        at = LATER + timedelta(minutes=1)
    elif mode == "expired":
        at = EXPIRES_AT
    elif mode == "mismatched":
        request = _request(grant, bundle_hash="e" * 64)
    elif mode == "capability":
        capability = ""
    elif mode == "tests_passed":
        tests_passed = _wrong("definitely passed")
    with pytest.raises(GatekeeperRequestError):
        client.submit(
            request,
            grant,
            at=at,
            harness_capability=capability,
            tests_passed=tests_passed,
        )
    assert endpoint.submitted == []
    assert endpoint.polled == []


def test_rejected_outcomes_carry_reasons() -> None:
    with pytest.raises(GatekeeperProtocolError):
        GatekeeperDecision(
            request_ref="req-0001", decision=GatekeeperDecisionKind.REJECTED
        )
    with pytest.raises(GatekeeperProtocolError):
        ActivationStatus(request_ref="req-0001", state=ActivationState.REJECTED)
    endpoint = FakeGatekeeperEndpoint(approved=False, reason="risk policy violation")
    client = GatekeeperClient(endpoint, ledger=GrantLedger())
    grant = _grant()
    status = client.submit(
        _request(grant),
        grant,
        at=LATER,
        harness_capability="paper-shell",
        tests_passed=True,
    )
    assert status.state is ActivationState.REJECTED
    assert status.reason == "risk policy violation"


def test_client_and_gatekeeper_request_carry_no_credentials() -> None:
    """AC5: Thesis structurally holds no broker or gatekeeper credentials."""
    grant = _grant()
    endpoint = FakeGatekeeperEndpoint()
    client = GatekeeperClient(endpoint, ledger=GrantLedger())
    status = client.submit(
        _request(grant),
        grant,
        at=LATER,
        harness_capability="paper-shell",
        tests_passed=True,
    )
    records = (endpoint.submitted[0], _request(grant), status)
    for record in records:
        assert not any(
            _CREDENTIAL_PATTERN.search(str(key)) for key in record.canonical_document
        )
        assert _CREDENTIAL_PATTERN.search(record.canonical_json) is None
    assert set(vars(client)) == {"_endpoint", "_ledger"}
    for record_type in (
        GatekeeperRequest,
        ActivationRequest,
        ActivationStatus,
        GatekeeperDecision,
    ):
        for field in dataclasses.fields(record_type):
            assert _CREDENTIAL_PATTERN.search(field.name) is None


def test_audit_records_round_trip_through_canonical_json() -> None:
    proposal = _proposal()
    confirmation = _confirmation()
    grant = confirm_grant(proposal, confirmation, created_at=CREATED_AT)
    revocation = revoke(grant, at=LATER, reason="paused")
    request = _request(grant)
    gatekeeper_request = GatekeeperRequest(
        request_ref=request.request_ref,
        grant_id=grant.grant_id,
        bundle_hash=grant.bundle_hash,
        environment=grant.environment,
        action=grant.action,
        instruments=grant.instruments,
        capital_bound=grant.capital_bound,
        risk_bound=grant.risk_bound,
        scope_kind=grant.scope.kind,
        interval_end=None,
        harness_capability="paper-shell",
        tests_passed=True,
    )
    status = ActivationStatus(request_ref="req-0001", state=ActivationState.APPROVED)
    records = (
        proposal,
        confirmation,
        grant,
        revocation,
        request,
        gatekeeper_request,
        status,
    )
    for record in records:
        assert record.canonical_json == json.dumps(
            record.canonical_document,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        assert json.loads(record.canonical_json) == record.canonical_document
    assert re.fullmatch(r"[0-9a-f]{64}", grant.grant_id)
    twin = confirm_grant(_proposal(), _confirmation(), created_at=CREATED_AT)
    assert twin.grant_id == grant.grant_id
    document = json.loads(grant.canonical_json)
    rebuilt = confirm_grant(
        propose_grant(
            bundle_hash=document["bundleHash"],
            environment=GrantEnvironment(document["environment"]),
            action=GrantAction(document["action"]),
            instruments=document["instruments"],
            capital_bound=Decimal(document["capitalBound"]),
            risk_bound=Decimal(document["riskBound"]),
            expiration=_parse(document["expiration"]),
            scope=GrantScope(
                kind=GrantScopeKind(document["scope"]["kind"]),
                interval_end=document["scope"]["intervalEnd"],
            ),
            proposed_at=PROPOSED_AT,
        ),
        _confirmation(),
        created_at=_parse(document["createdAt"]),
    )
    assert rebuilt.canonical_json == grant.canonical_json
    assert rebuilt.grant_id == grant.grant_id
