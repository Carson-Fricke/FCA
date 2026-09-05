from __future__ import annotations

import dataclasses

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fake_authority import (
    BUNDLE,
    CONTRACT,
    FIXED_NOW,
    POLICY_VERSION,
    SNAPSHOT,
    FakeAuthority,
    FixedClock,
    build_submission,
)

from thesis.protected import (
    ELIGIBILITY_PATH,
    AuthorityProtocolError,
    AuthorityRequest,
    AuthorityResponse,
    AuthorityVerifier,
    EligibilityDeniedError,
    EvaluationSubmission,
    FactConflictError,
    ProtectedTestClient,
    SealedStatusKind,
    SealedStatusMirror,
    SignatureVerificationError,
)
from thesis.protected.windows import HiddenWindowRef

WINDOW_REF_KEYS = {
    "allocationPolicyVersion",
    "commitmentSha256",
    "schema",
    "windowId",
}


def _evaluation(
    evaluation_id: str,
    submission_id: str,
    window_id: str,
    *,
    lineage_id: str = "lineage-1",
    bundle: str = BUNDLE,
) -> EvaluationSubmission:
    return EvaluationSubmission(
        evaluation_id=evaluation_id,
        submission_id=submission_id,
        window_id=window_id,
        execution_id="exec-1",
        experiment_id="experiment-1",
        strategy_bundle_sha256=bundle,
        lineage_id=lineage_id,
        actor="protected-runner",
        performed_at=FIXED_NOW,
    )


def _client(
    authority: FakeAuthority,
) -> tuple[ProtectedTestClient, SealedStatusMirror]:
    verifier = AuthorityVerifier(authority.public_key)
    mirror = SealedStatusMirror(verifier)
    client = ProtectedTestClient(authority, verifier, FixedClock(), mirror)
    return client, mirror


def test_eligibility_grants_signed_preallocated_windows() -> None:
    authority = FakeAuthority(window_count=2, allowed_looks=1)
    client, mirror = _client(authority)

    grant = client.request_eligibility(build_submission("sub-1"))

    assert grant.fact.fact.kind is SealedStatusKind.ELIGIBILITY_GRANTED
    assert grant.allowed_looks == 1
    assert grant.reused is False
    assert grant.fact.content_sha256 == grant.fact.fact.content_sha256
    assert grant.fact.authority_id == "authority-fake"
    assert grant.allocation.policy_version == POLICY_VERSION
    assert [window.window_id for window in grant.allocation.windows] == [
        "win-0000",
        "win-0001",
    ]
    assert grant.fact.fact.payload["allowedLooks"] == 1
    assert mirror.kinds("sub-1") == (SealedStatusKind.ELIGIBILITY_GRANTED,)
    for window in grant.allocation.windows:
        assert isinstance(window, HiddenWindowRef)
        assert window.allocation_policy_version == POLICY_VERSION
        assert set(window.to_payload()) == WINDOW_REF_KEYS


def test_window_refs_carry_no_contents_or_time_bounds() -> None:
    authority = FakeAuthority(window_count=1)
    client, _ = _client(authority)

    grant = client.request_eligibility(build_submission("sub-1"))

    for window in grant.allocation.windows:
        payload = window.to_payload()
        assert set(payload) == WINDOW_REF_KEYS
        assert "startNs" not in payload
        assert "endNs" not in payload
        assert "assets" not in payload
        assert "payload" not in payload


def test_denied_submission_raises_with_verified_fact() -> None:
    authority = FakeAuthority()
    client, mirror = _client(authority)

    with pytest.raises(EligibilityDeniedError) as denied:
        client.request_eligibility(build_submission("sub-1", with_registry=False))

    assert denied.value.fact.fact.kind is SealedStatusKind.ELIGIBILITY_DENIED
    assert "generator registry identity" in denied.value.reason
    assert mirror.kinds("sub-1") == (SealedStatusKind.ELIGIBILITY_DENIED,)


def test_evaluation_records_exposure_reuse_then_exhaustion() -> None:
    authority = FakeAuthority(window_count=1, allowed_looks=1)
    client, mirror = _client(authority)

    first = client.request_eligibility(build_submission("sub-1"))
    window_id = first.allocation.windows[0].window_id
    exposed = client.record_exposure(_evaluation("eval-1", "sub-1", window_id))
    assert [fact.fact.kind for fact in exposed] == [SealedStatusKind.WINDOW_EXPOSED]
    assert exposed[0].fact.window_id == window_id

    client.request_eligibility(build_submission("sub-2"))
    reused = client.record_exposure(_evaluation("eval-2", "sub-2", window_id))
    assert [fact.fact.kind for fact in reused] == [SealedStatusKind.WINDOW_REUSED]

    exhausted = client.record_exposure(_evaluation("eval-3", "sub-2", window_id))
    assert [fact.fact.kind for fact in exhausted] == [
        SealedStatusKind.ELIGIBILITY_EXHAUSTED
    ]
    assert exhausted[0].fact.payload == {"allowedLooks": 1, "observedLooks": 1}
    assert mirror.kinds("sub-2") == (
        SealedStatusKind.ELIGIBILITY_GRANTED,
        SealedStatusKind.WINDOW_REUSED,
        SealedStatusKind.ELIGIBILITY_EXHAUSTED,
    )


def test_sealed_status_returns_the_authority_record() -> None:
    authority = FakeAuthority(window_count=1)
    client, _ = _client(authority)
    grant = client.request_eligibility(build_submission("sub-1"))
    client.record_exposure(
        _evaluation("eval-1", "sub-1", grant.allocation.windows[0].window_id)
    )

    status = client.sealed_status("sub-1")

    assert [fact.fact.kind for fact in status] == [
        SealedStatusKind.ELIGIBILITY_GRANTED,
        SealedStatusKind.WINDOW_EXPOSED,
    ]
    assert client.sealed_status("unknown-sub") == ()


def test_unknown_prior_exposure_denies_a_fresh_budget() -> None:
    authority = FakeAuthority(allowed_looks=1)
    client, mirror = _client(authority)

    grant = client.request_eligibility(
        build_submission("sub-1", ancestry_signature_valid=False)
    )

    assert grant.allowed_looks == 0
    assert [fact.fact.kind for fact in grant.facts] == [
        SealedStatusKind.ELIGIBILITY_GRANTED,
        SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE,
    ]
    assert grant.fact.fact.payload["unknownPriorExposure"] is True
    assert mirror.kinds("sub-1") == (
        SealedStatusKind.ELIGIBILITY_GRANTED,
        SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE,
    )
    window_id = grant.allocation.windows[0].window_id
    exhausted = client.record_exposure(_evaluation("eval-1", "sub-1", window_id))
    assert [fact.fact.kind for fact in exhausted] == [
        SealedStatusKind.ELIGIBILITY_EXHAUSTED
    ]


def test_bundle_fingerprint_prevents_lineage_laundering() -> None:
    authority = FakeAuthority(window_count=2, allowed_looks=1)
    client, _ = _client(authority)

    fresh = client.request_eligibility(
        build_submission("sub-1", bundle="e" * 64, with_ancestry=False)
    )
    assert fresh.reused is False
    client.record_exposure(
        _evaluation(
            "eval-1",
            "sub-1",
            fresh.allocation.windows[0].window_id,
            bundle="e" * 64,
        )
    )

    laundered = client.request_eligibility(
        build_submission(
            "sub-2",
            lineage_id="lineage-2",
            bundle="e" * 64,
            with_ancestry=False,
        )
    )
    assert laundered.reused is True

    other = client.request_eligibility(build_submission("sub-3", with_ancestry=False))
    assert other.reused is False


def test_client_rejects_facts_signed_by_another_authority() -> None:
    authority = FakeAuthority()
    other = FakeAuthority(
        private_key=Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    )
    verifier = AuthorityVerifier(other.public_key)
    mirror = SealedStatusMirror(verifier)
    client = ProtectedTestClient(authority, verifier, FixedClock(), mirror)

    with pytest.raises(SignatureVerificationError):
        client.request_eligibility(build_submission("sub-1"))
    assert mirror.facts() == ()


def test_tampered_fact_fails_verification_everywhere() -> None:
    authority = FakeAuthority()
    verifier = AuthorityVerifier(authority.public_key)
    mirror = SealedStatusMirror(verifier)
    client = ProtectedTestClient(authority, verifier, FixedClock(), mirror)
    grant = client.request_eligibility(build_submission("sub-1"))
    signed = grant.fact
    tampered_fact = dataclasses.replace(
        signed.fact, payload={**signed.fact.payload, "allowedLooks": 99}
    )
    tampered = dataclasses.replace(signed, fact=tampered_fact)

    with pytest.raises(SignatureVerificationError):
        verifier.verify(tampered)
    with pytest.raises(SignatureVerificationError):
        mirror.record(tampered)
    assert mirror.get(signed.fact.fact_id) == signed


def test_duplicate_fact_id_with_different_content_conflicts() -> None:
    authority = FakeAuthority()
    verifier = AuthorityVerifier(authority.public_key)
    mirror = SealedStatusMirror(verifier)
    grant = ProtectedTestClient(
        authority, verifier, FixedClock(), mirror
    ).request_eligibility(build_submission("sub-1"))
    original = grant.fact
    conflicting = authority.sign_fact(
        dataclasses.replace(original.fact, payload={"allowedLooks": 7})
    )

    mirror.record(original)
    with pytest.raises(FactConflictError):
        mirror.record(conflicting)


def test_transport_errors_surface_as_protocol_errors() -> None:
    authority = FakeAuthority()

    class BrokenTransport:
        def send(self, request: AuthorityRequest) -> AuthorityResponse:
            if request.path == ELIGIBILITY_PATH:
                return AuthorityResponse(status=404, payload={"error": "unknown path"})
            return authority.send(request)

    verifier = AuthorityVerifier(authority.public_key)
    client = ProtectedTestClient(
        BrokenTransport(), verifier, FixedClock(), SealedStatusMirror(verifier)
    )

    with pytest.raises(AuthorityProtocolError):
        client.request_eligibility(build_submission("sub-1"))


def test_submission_content_address_is_stable() -> None:
    first = build_submission("sub-1")
    second = build_submission("sub-1")
    changed = build_submission("sub-1", configuration="9" * 64)

    assert first.content_sha256 == second.content_sha256
    assert first.content_sha256 != changed.content_sha256
    assert first.contract_sha256 == CONTRACT
    assert first.model_registry[0].snapshot_sha256 == SNAPSHOT
