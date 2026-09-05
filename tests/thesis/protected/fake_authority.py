"""In-memory fake protected-test authority for offline deterministic tests.

The real authority is an external sealed service.  This fake implements the
same transport-facing protocol in memory so protocol tests never touch the
network: it preallocates a hidden-window pool with a fixed versioned policy,
verifies signed ancestry and exact bundle fingerprints, signs sealed-status
facts with a deterministic Ed25519 test key, and owns exposure, reuse, and
budget exhaustion.  Window contents exist only inside the fake as opaque
commitment digests; nothing content-bearing is ever placed in a response.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from thesis.protected.client import (
    ELIGIBILITY_PATH,
    EVALUATION_PATH,
    SEALED_STATUS_PATH,
    AuthorityRequest,
    AuthorityResponse,
)
from thesis.protected.contracts import (
    LineageAncestry,
    ModelRegistryIdentity,
    ProtectedTestSubmission,
    RegistryRole,
    SealedStatusKind,
)
from thesis.protected.facts import (
    SealedStatusFact,
    SignedSealedStatusFact,
    fact_signing_bytes,
)
from thesis.protected.windows import HiddenWindowRef, WindowAllocation

FIXED_NOW = "2026-03-01T12:00:00.000000Z"
POLICY_VERSION = "hidden-window-policy-v1"
AUTHORITY_ID = "authority-fake"
BUNDLE = "b" * 64
CONTRACT = "c" * 64
CONFIGURATION = "d" * 64
SNAPSHOT = "a" * 64


class FixedClock:
    """Deterministic clock; the production SystemClock is never used in tests."""

    def __init__(self, now: str = FIXED_NOW) -> None:
        self.now_value = now

    def now(self) -> str:
        """Return the fixed timestamp."""
        return self.now_value


def ancestry_signature(parent_lineage_id: str, bundle_sha256: str) -> str:
    """Return the valid test signature for one ancestry link."""
    material = f"fca-test-ancestry:{parent_lineage_id}:{bundle_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_submission(
    submission_id: str = "sub-1",
    *,
    lineage_id: str = "lineage-1",
    bundle: str = BUNDLE,
    contract: str = CONTRACT,
    configuration: str = CONFIGURATION,
    with_ancestry: bool = True,
    ancestry_signature_valid: bool = True,
    with_registry: bool = True,
) -> ProtectedTestSubmission:
    """Build a well-formed submission for protocol tests."""
    links = ()
    if with_ancestry:
        parent = "lineage-0"
        signature = (
            ancestry_signature(parent, bundle)
            if ancestry_signature_valid
            else "00" * 32
        )
        links = (
            LineageAncestry(
                parent_lineage_id=parent, bundle_sha256=bundle, signature=signature
            ),
        )
    registry = ()
    if with_registry:
        registry = (
            ModelRegistryIdentity(
                role=RegistryRole.GENERATOR,
                registry_id="registry-1",
                provider="provider-x",
                model_id="model-x",
                snapshot_sha256=SNAPSHOT,
            ),
        )
    return ProtectedTestSubmission(
        submission_id=submission_id,
        workspace_id="ws-test",
        experiment_id="experiment-1",
        lineage_id=lineage_id,
        contract_sha256=contract,
        strategy_bundle_sha256=bundle,
        configuration_sha256=configuration,
        submitted_at=FIXED_NOW,
        lineage_ancestry=links,
        model_registry=registry,
    )


class FakeAuthority:
    """The sealed authority stand-in; also the client transport."""

    def __init__(
        self,
        *,
        window_count: int = 2,
        allowed_looks: int = 1,
        private_key: Ed25519PrivateKey | None = None,
        clock: FixedClock | None = None,
    ) -> None:
        self._key = private_key or Ed25519PrivateKey.from_private_bytes(
            bytes(range(32))
        )
        self._clock = clock or FixedClock()
        self._windows = tuple(
            HiddenWindowRef(
                window_id=f"win-{index:04d}",
                allocation_policy_version=POLICY_VERSION,
                commitment_sha256=hashlib.sha256(
                    f"hidden-window:{index}".encode()
                ).hexdigest(),
            )
            for index in range(window_count)
        )
        self._default_allowed_looks = allowed_looks
        self._workspaces: dict[str, str] = {}
        self._submissions: dict[str, dict[str, object]] = {}
        self._allocations: dict[str, tuple[str, ...]] = {}
        self._allowed: dict[str, int] = {}
        self._looks: dict[str, int] = {}
        self._exposed_windows: dict[str, list[str]] = {}
        self._exposed_bundles: dict[str, set[str]] = {}
        self._facts: dict[str, list[SignedSealedStatusFact]] = {}
        self._fact_counter = 0

    @property
    def public_key(self) -> Ed25519PublicKey:
        """Return the verifying public key for clients."""
        return self._key.public_key()

    def sign_fact(self, fact: SealedStatusFact) -> SignedSealedStatusFact:
        """Sign one fact with the authority test key."""
        return SignedSealedStatusFact(
            fact=fact,
            authority_id=AUTHORITY_ID,
            signature=self._key.sign(fact_signing_bytes(fact)).hex(),
        )

    def send(self, request: AuthorityRequest) -> AuthorityResponse:
        """Handle one client request entirely in memory."""
        if request.method != "POST":
            return AuthorityResponse(
                status=405, payload={"error": "method not allowed"}
            )
        handlers = {
            ELIGIBILITY_PATH: self._eligibility,
            EVALUATION_PATH: self._evaluation,
            SEALED_STATUS_PATH: self._status,
        }
        handler = handlers.get(request.path)
        if handler is None:
            return AuthorityResponse(status=404, payload={"error": "unknown path"})
        return handler(request.payload)

    def _emit(
        self,
        submission_id: str,
        *,
        kind: SealedStatusKind,
        window_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> SignedSealedStatusFact:
        self._fact_counter += 1
        fact = SealedStatusFact(
            fact_id=f"fact-{self._fact_counter:04d}",
            kind=kind,
            workspace_id=self._workspaces[submission_id],
            submission_id=submission_id,
            window_id=window_id,
            issued_at=self._clock.now(),
            payload=payload if payload is not None else {},
        )
        signed = self.sign_fact(fact)
        self._facts.setdefault(submission_id, []).append(signed)
        return signed

    def _eligibility(self, payload: Mapping[str, object]) -> AuthorityResponse:
        document = payload.get("submission")
        if not isinstance(document, dict):
            return AuthorityResponse(
                status=422, payload={"error": "missing submission"}
            )
        submission_id = document.get("submissionId")
        if not isinstance(submission_id, str):
            return AuthorityResponse(status=422, payload={"error": "missing id"})
        stored = self._submissions.get(submission_id)
        if stored is not None and stored != document:
            return AuthorityResponse(
                status=409, payload={"error": "submission id reused"}
            )
        workspace = document.get("workspaceId")
        if not isinstance(workspace, str):
            return AuthorityResponse(status=422, payload={"error": "missing workspace"})
        if stored is None:
            self._submissions[submission_id] = document
            self._workspaces[submission_id] = workspace
        unknown_prior = False
        ancestry = document.get("lineageAncestry", [])
        if isinstance(ancestry, list):
            for link in ancestry:
                if not isinstance(link, dict):
                    unknown_prior = True
                    continue
                parent = link.get("parentLineageId")
                bundle = link.get("bundleSha256")
                signature = link.get("signature")
                if (
                    not isinstance(parent, str)
                    or not isinstance(bundle, str)
                    or not isinstance(signature, str)
                    or signature != ancestry_signature(parent, bundle)
                ):
                    unknown_prior = True
        registry = document.get("modelRegistry", [])
        roles = {
            identity.get("role") for identity in registry if isinstance(identity, dict)
        }
        facts: list[SignedSealedStatusFact] = []
        if "generator" not in roles:
            facts.append(
                self._emit(
                    submission_id,
                    kind=SealedStatusKind.ELIGIBILITY_DENIED,
                    payload={
                        "reason": "submission declares no generator registry identity"
                    },
                )
            )
            return AuthorityResponse(
                status=200, payload={"facts": [self._envelope(f) for f in facts]}
            )
        bundle_sha256 = document.get("strategyBundleSha256")
        reused = (
            isinstance(bundle_sha256, str) and bundle_sha256 in self._exposed_bundles
        )
        allowed = 0 if unknown_prior else self._default_allowed_looks
        allocation = WindowAllocation(
            submission_id=submission_id,
            policy_version=POLICY_VERSION,
            windows=self._windows,
        )
        facts.append(
            self._emit(
                submission_id,
                kind=SealedStatusKind.ELIGIBILITY_GRANTED,
                payload={
                    "allocation": allocation.to_payload(),
                    "allowedLooks": allowed,
                    "reused": reused,
                    "unknownPriorExposure": unknown_prior,
                },
            )
        )
        if unknown_prior:
            facts.append(
                self._emit(
                    submission_id,
                    kind=SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE,
                    payload={
                        "reason": "signed ancestry could not be verified",
                    },
                )
            )
        self._allocations[submission_id] = tuple(
            window.window_id for window in self._windows
        )
        self._allowed[submission_id] = allowed
        self._looks[submission_id] = 0
        return AuthorityResponse(
            status=200, payload={"facts": [self._envelope(fact) for fact in facts]}
        )

    def _evaluation(self, payload: Mapping[str, object]) -> AuthorityResponse:
        document = payload.get("evaluation")
        if not isinstance(document, dict):
            return AuthorityResponse(
                status=422, payload={"error": "missing evaluation"}
            )
        submission_id = document.get("submissionId")
        if not isinstance(submission_id, str) or submission_id not in self._submissions:
            return AuthorityResponse(
                status=404, payload={"error": "unknown submission"}
            )
        window_id = document.get("windowId")
        allocated = self._allocations.get(submission_id, ())
        if not isinstance(window_id, str) or window_id not in allocated:
            return AuthorityResponse(
                status=422, payload={"error": "window not allocated to submission"}
            )
        observed = self._looks[submission_id] + 1
        allowed = self._allowed[submission_id]
        if observed > allowed:
            fact = self._emit(
                submission_id,
                kind=SealedStatusKind.ELIGIBILITY_EXHAUSTED,
                payload={"observedLooks": observed - 1, "allowedLooks": allowed},
            )
            return AuthorityResponse(
                status=200, payload={"facts": [self._envelope(fact)]}
            )
        bundle_sha256 = document.get("strategyBundleSha256")
        lineage_id = document.get("lineageId")
        execution_id = document.get("executionId")
        if not isinstance(bundle_sha256, str) or not isinstance(lineage_id, str):
            return AuthorityResponse(
                status=422, payload={"error": "missing identities"}
            )
        reused = window_id in self._exposed_windows
        fact = self._emit(
            submission_id,
            kind=(
                SealedStatusKind.WINDOW_REUSED
                if reused
                else SealedStatusKind.WINDOW_EXPOSED
            ),
            window_id=window_id,
            payload={
                "executionId": execution_id if isinstance(execution_id, str) else "",
                "lineageId": lineage_id,
                "strategyBundleSha256": bundle_sha256,
                "windowId": window_id,
            },
        )
        self._looks[submission_id] = observed
        self._exposed_windows.setdefault(window_id, []).append(submission_id)
        self._exposed_bundles.setdefault(bundle_sha256, set()).add(window_id)
        return AuthorityResponse(status=200, payload={"facts": [self._envelope(fact)]})

    def _status(self, payload: Mapping[str, object]) -> AuthorityResponse:
        submission_id = payload.get("submissionId")
        if not isinstance(submission_id, str):
            return AuthorityResponse(status=422, payload={"error": "missing id"})
        facts = self._facts.get(submission_id, ())
        return AuthorityResponse(
            status=200, payload={"facts": [self._envelope(fact) for fact in facts]}
        )

    def _envelope(self, signed: SignedSealedStatusFact) -> dict[str, object]:
        return {
            "authorityId": signed.authority_id,
            "fact": signed.fact.canonical_document,
            "scheme": signed.scheme,
            "signature": signed.signature,
        }
