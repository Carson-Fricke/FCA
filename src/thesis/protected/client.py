"""Deterministic client for the external protected-test authority.

The authority service is external and sealed: Thesis is only its client.
All interaction goes through an injected :class:`AuthorityTransport`, so the
client itself performs no network, wall-clock, or randomness operations and
is fully deterministic in tests (an in-memory fake authority plus a fixed
clock).  A real HTTP transport adapter can implement the same one-method
protocol without changing this module.

Protocol shape, one POST per operation:

1. ``request_eligibility`` posts the immutable
   :class:`~thesis.protected.contracts.ProtectedTestSubmission` and receives
   signed eligibility facts.  A grant carries the preallocated hidden-window
   allocation (references only — window contents never cross the boundary),
   the allowed look budget, and a reuse flag the authority derived from
   exact bundle fingerprints and signed ancestry.
2. ``record_exposure`` posts an evaluation against one allocated window and
   receives signed exposure facts: ``window-exposed`` on a first sanctioned
   look, ``window-reused`` on any later look at the same window, and
   ``eligibility-exhausted`` once the budget is spent.
3. ``sealed_status`` queries the authority's signed record for a submission.

Every returned fact is signature-verified with the injected authority public
key before it is returned or stored in the read-only mirror; an unverified
or unexpected fact is an error, never a silent downgrade of trust.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from thesis.protected.contracts import (
    AuthorityProtocolError,
    ProtectedTestError,
    ProtectedTestSubmission,
    SealedStatusKind,
    _digest,
    _identifier,
    _timestamp,
)
from thesis.protected.facts import (
    AuthorityVerifier,
    SealedStatusFact,
    SealedStatusMirror,
    SignedSealedStatusFact,
)
from thesis.protected.windows import WindowAllocation

ELIGIBILITY_PATH = "/v1/protected/eligibility"
EVALUATION_PATH = "/v1/protected/evaluations"
SEALED_STATUS_PATH = "/v1/protected/sealed-status"

_EXPECTED_FACT_KEYS = ("authorityId", "fact", "scheme", "signature")


class Clock(Protocol):
    """Time source injected into the client; never the wall clock in tests."""

    def now(self) -> str:
        """Return the current timezone-aware ISO-8601 timestamp."""
        ...


class SystemClock:
    """Production UTC clock; tests inject a fixed clock instead."""

    def now(self) -> str:
        """Return the current UTC time as a canonical ISO-8601 timestamp."""
        return (
            datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )


@dataclass(frozen=True, slots=True)
class AuthorityRequest:
    """One outbound authority request."""

    method: str
    path: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AuthorityResponse:
    """One inbound authority response."""

    status: int
    payload: Mapping[str, object]


class AuthorityTransport(Protocol):
    """The injected transport boundary; the client never opens sockets."""

    def send(self, request: AuthorityRequest) -> AuthorityResponse:
        """Send one request and return the authority response."""
        ...


@dataclass(frozen=True, slots=True)
class EvaluationSubmission:
    """One evaluation contact against an allocated protected window."""

    evaluation_id: str
    submission_id: str
    window_id: str
    execution_id: str
    experiment_id: str
    strategy_bundle_sha256: str
    lineage_id: str
    actor: str
    performed_at: str
    schema: str = "fca-thesis-protected-evaluation-v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evaluation_id", _identifier(self.evaluation_id, "evaluation_id")
        )
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        object.__setattr__(self, "window_id", _identifier(self.window_id, "window_id"))
        object.__setattr__(
            self, "execution_id", _identifier(self.execution_id, "execution_id")
        )
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(
            self,
            "strategy_bundle_sha256",
            _digest(self.strategy_bundle_sha256, "strategy_bundle_sha256"),
        )
        object.__setattr__(
            self, "lineage_id", _identifier(self.lineage_id, "lineage_id")
        )
        object.__setattr__(self, "actor", _identifier(self.actor, "actor"))
        object.__setattr__(
            self, "performed_at", _timestamp(self.performed_at, "performed_at")
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this evaluation."""
        return {
            "actor": self.actor,
            "evaluationId": self.evaluation_id,
            "executionId": self.execution_id,
            "experimentId": self.experiment_id,
            "lineageId": self.lineage_id,
            "performedAt": self.performed_at,
            "schema": self.schema,
            "strategyBundleSha256": self.strategy_bundle_sha256,
            "submissionId": self.submission_id,
            "windowId": self.window_id,
        }


@dataclass(frozen=True, slots=True)
class EligibilityGrant:
    """A verified eligibility grant with its hidden-window allocation."""

    fact: SignedSealedStatusFact
    facts: tuple[SignedSealedStatusFact, ...]
    allocation: WindowAllocation
    allowed_looks: int
    reused: bool


class EligibilityDeniedError(ProtectedTestError):
    """The authority denied eligibility; the decision fact is verified."""

    def __init__(self, fact: SignedSealedStatusFact, reason: str) -> None:
        self.fact = fact
        self.reason = reason
        super().__init__(f"protected-test eligibility denied: {reason}")


class ProtectedTestClient:
    """The authority client: eligibility, exposure, and sealed status."""

    def __init__(
        self,
        transport: AuthorityTransport,
        verifier: AuthorityVerifier,
        clock: Clock,
        mirror: SealedStatusMirror | None = None,
    ) -> None:
        if not isinstance(verifier, AuthorityVerifier):
            raise ProtectedTestError("verifier must be an AuthorityVerifier")
        self._transport = transport
        self._verifier = verifier
        self._clock = clock
        self._mirror = mirror

    def request_eligibility(
        self, submission: ProtectedTestSubmission
    ) -> EligibilityGrant:
        """Request eligibility and return the verified grant.

        Raises :class:`EligibilityDeniedError` with the verified decision fact
        when the authority denies the submission.
        """
        if not isinstance(submission, ProtectedTestSubmission):
            raise ProtectedTestError("submission must be a ProtectedTestSubmission")
        facts = self._exchange(
            AuthorityRequest(
                method="POST",
                path=ELIGIBILITY_PATH,
                payload={"submission": submission.canonical_document},
            )
        )
        grant = self._single_fact(facts, SealedStatusKind.ELIGIBILITY_GRANTED)
        if grant is None:
            denied = self._single_fact(facts, SealedStatusKind.ELIGIBILITY_DENIED)
            if denied is None:
                raise AuthorityProtocolError(
                    "authority returned no eligibility decision"
                )
            reason = denied.fact.payload.get("reason")
            raise EligibilityDeniedError(
                denied,
                reason if isinstance(reason, str) else "unspecified",
            )
        allocation = WindowAllocation.from_payload(grant.fact.payload.get("allocation"))
        allowed_looks = grant.fact.payload.get("allowedLooks")
        if isinstance(allowed_looks, bool) or not isinstance(allowed_looks, int):
            raise AuthorityProtocolError("eligibility allowedLooks must be an integer")
        if allowed_looks < 0:
            raise AuthorityProtocolError(
                "eligibility allowedLooks must not be negative"
            )
        return EligibilityGrant(
            fact=grant,
            facts=facts,
            allocation=allocation,
            allowed_looks=allowed_looks,
            reused=grant.fact.payload.get("reused") is True,
        )

    def record_exposure(
        self, evaluation: EvaluationSubmission
    ) -> tuple[SignedSealedStatusFact, ...]:
        """Submit one evaluation contact and return verified exposure facts.

        The authority decides between first exposure, reuse, and budget
        exhaustion; this client records and returns whichever signed facts it
        issues without ever interpreting window contents.
        """
        if not isinstance(evaluation, EvaluationSubmission):
            raise ProtectedTestError("evaluation must be an EvaluationSubmission")
        return self._exchange(
            AuthorityRequest(
                method="POST",
                path=EVALUATION_PATH,
                payload={"evaluation": evaluation.canonical_document},
            )
        )

    def sealed_status(self, submission_id: str) -> tuple[SignedSealedStatusFact, ...]:
        """Query the authority's signed sealed-status record for a submission."""
        return self._exchange(
            AuthorityRequest(
                method="POST",
                path=SEALED_STATUS_PATH,
                payload={"submissionId": _identifier(submission_id, "submission_id")},
            ),
            require_facts=False,
        )

    def _exchange(
        self, request: AuthorityRequest, *, require_facts: bool = True
    ) -> tuple[SignedSealedStatusFact, ...]:
        response = self._transport.send(request)
        if response.status != 200:
            raise AuthorityProtocolError(
                f"authority returned status {response.status} for {request.path}"
            )
        envelopes = response.payload.get("facts")
        if not isinstance(envelopes, list):
            raise AuthorityProtocolError("authority response is missing facts")
        if not envelopes and require_facts:
            raise AuthorityProtocolError("authority returned no facts")
        signed = tuple(_signed_fact_from_envelope(envelope) for envelope in envelopes)
        for item in signed:
            self._verifier.verify(item)
        if self._mirror is not None:
            self._mirror.record_all(signed)
        return signed

    def _single_fact(
        self,
        facts: tuple[SignedSealedStatusFact, ...],
        kind: SealedStatusKind,
    ) -> SignedSealedStatusFact | None:
        matches = [item for item in facts if item.fact.kind is kind]
        if len(matches) > 1:
            raise AuthorityProtocolError(f"authority returned duplicate {kind} facts")
        return matches[0] if matches else None


def _signed_fact_from_envelope(envelope: object) -> SignedSealedStatusFact:
    if not isinstance(envelope, dict):
        raise AuthorityProtocolError("fact envelope must be a mapping")
    for key in _EXPECTED_FACT_KEYS:
        if key not in envelope:
            raise AuthorityProtocolError(f"fact envelope is missing {key!r}")
    document = envelope["fact"]
    if not isinstance(document, dict):
        raise AuthorityProtocolError("fact envelope fact must be a mapping")
    kind_value = document.get("kind")
    try:
        kind = SealedStatusKind(kind_value)
    except ValueError as error:
        raise AuthorityProtocolError(
            f"unknown sealed-status kind {kind_value!r}"
        ) from error
    payload = document.get("payload", {})
    if not isinstance(payload, dict):
        raise AuthorityProtocolError("fact payload must be a mapping")
    fact = SealedStatusFact(
        fact_id=_envelope_field(document, "factId"),
        kind=kind,
        workspace_id=_envelope_field(document, "workspaceId"),
        submission_id=_envelope_field(document, "submissionId"),
        issued_at=_envelope_field(document, "issuedAt"),
        payload=payload,
        window_id=document.get("windowId"),
    )
    scheme = envelope["scheme"]
    signature = envelope["signature"]
    authority_id = envelope["authorityId"]
    if not isinstance(scheme, str) or not isinstance(signature, str):
        raise AuthorityProtocolError("fact envelope fields must be strings")
    if not isinstance(authority_id, str):
        raise AuthorityProtocolError("fact envelope authorityId must be a string")
    return SignedSealedStatusFact(
        fact=fact,
        authority_id=authority_id,
        signature=signature,
        scheme=scheme,
    )


def _envelope_field(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise AuthorityProtocolError(f"fact field {key!r} must be a string")
    return value


__all__ = [
    "AuthorityRequest",
    "AuthorityResponse",
    "AuthorityTransport",
    "Clock",
    "ELIGIBILITY_PATH",
    "EVALUATION_PATH",
    "EligibilityDeniedError",
    "EligibilityGrant",
    "EvaluationSubmission",
    "ProtectedTestClient",
    "SEALED_STATUS_PATH",
    "SystemClock",
]
