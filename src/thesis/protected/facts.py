"""Signed sealed-status facts and the read-only Thesis mirror.

The authority is the canonical owner of hidden allocation, protected
exposure, eligibility, reuse, and sealed-status decisions.  Every decision
crosses the boundary as a :class:`SealedStatusFact` wrapped in a
:class:`SignedSealedStatusFact` envelope signed with the authority's Ed25519
private key.  Thesis verifies each signature with the injected authority
public key and stores only verified facts in a read-only mirror; the mirror
refuses any fact that fails verification and keeps no authority-independent
copy of the underlying decisions.

Key material follows the repository secret conventions: private keys never
enter this package.  The verifying public key is injected by the caller
(loaded from the configured secret store at composition time); nothing here
reads files, the environment, or the network.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from thesis.protected.contracts import (
    AuthorityProtocolError,
    FactConflictError,
    ProtectedTestError,
    SealedStatusKind,
    SignatureVerificationError,
)

FACT_SIGNING_DOMAIN = b"fca:thesis-sealed-status-fact:v1\0"
FACT_SCHEMA = "fca-thesis-sealed-status-fact-v1"
SIGNATURE_SCHEME = "ed25519"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{128}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AuthorityProtocolError(f"{label} must be a safe identifier")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityProtocolError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise AuthorityProtocolError(f"{label} contains unsupported characters")
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AuthorityProtocolError(f"{label} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AuthorityProtocolError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorityProtocolError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        )
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _jsonable(value: object) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise AuthorityProtocolError("fact payloads must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise AuthorityProtocolError("fact payloads must be JSON-shaped")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AuthorityProtocolError("fact payloads must be canonical JSON") from error


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AuthorityProtocolError(f"{label} must be a JSON-shaped mapping")
    frozen = MappingProxyType(dict(value))
    _canonical_json(_jsonable(dict(frozen)))
    return frozen


@dataclass(frozen=True, slots=True)
class SealedStatusFact:
    """One authority-owned sealed-status decision."""

    fact_id: str
    kind: SealedStatusKind
    workspace_id: str
    submission_id: str
    issued_at: str
    payload: Mapping[str, object] = MappingProxyType({})
    window_id: str | None = None
    schema: str = FACT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", _identifier(self.fact_id, "fact_id"))
        if not isinstance(self.kind, SealedStatusKind):
            raise AuthorityProtocolError("kind must be a SealedStatusKind")
        object.__setattr__(
            self, "workspace_id", _identifier(self.workspace_id, "workspace_id")
        )
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        if self.window_id is not None:
            object.__setattr__(
                self, "window_id", _identifier(self.window_id, "window_id")
            )
        object.__setattr__(self, "issued_at", _timestamp(self.issued_at, "issued_at"))
        object.__setattr__(
            self, "payload", _freeze_mapping(self.payload, "fact payload")
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the signature covers."""
        return {
            "factId": self.fact_id,
            "issuedAt": self.issued_at,
            "kind": self.kind.value,
            "payload": _jsonable(dict(self.payload)),
            "schema": self.schema,
            "submissionId": self.submission_id,
            "windowId": self.window_id,
            "workspaceId": self.workspace_id,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this fact."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


def fact_signing_bytes(fact: SealedStatusFact) -> bytes:
    """Return the exact byte string the authority signs for one fact."""
    if not isinstance(fact, SealedStatusFact):
        raise SignatureVerificationError("fact must be a SealedStatusFact")
    return FACT_SIGNING_DOMAIN + _canonical_json(fact.canonical_document).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class SignedSealedStatusFact:
    """The signed envelope carrying one sealed-status fact."""

    fact: SealedStatusFact
    authority_id: str
    signature: str
    scheme: str = SIGNATURE_SCHEME

    def __post_init__(self) -> None:
        if not isinstance(self.fact, SealedStatusFact):
            raise SignatureVerificationError("fact must be a SealedStatusFact")
        object.__setattr__(
            self, "authority_id", _identifier(self.authority_id, "authority_id")
        )
        if self.scheme != SIGNATURE_SCHEME:
            raise SignatureVerificationError(
                f"unsupported signature scheme {self.scheme!r}"
            )
        if (
            not isinstance(self.signature, str)
            or _SIGNATURE.fullmatch(self.signature) is None
        ):
            raise SignatureVerificationError(
                "signature must be a hex-encoded Ed25519 signature"
            )

    @property
    def signed_bytes(self) -> bytes:
        """Return the exact byte string the authority signature covers."""
        return fact_signing_bytes(self.fact)

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the wrapped fact."""
        return self.fact.content_sha256


class AuthorityVerifier:
    """Verify signed sealed-status facts against the authority public key."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        if not isinstance(public_key, Ed25519PublicKey):
            raise SignatureVerificationError("public_key must be an Ed25519 public key")
        self._public_key = public_key

    def verify_bytes(self, payload: bytes, signature: str) -> None:
        """Verify raw signing bytes against a hex-encoded signature."""
        if not isinstance(payload, bytes):
            raise SignatureVerificationError("payload must be bytes")
        if not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
            raise SignatureVerificationError(
                "signature must be a hex-encoded Ed25519 signature"
            )
        try:
            self._public_key.verify(bytes.fromhex(signature), payload)
        except Exception as error:
            raise SignatureVerificationError(
                "authority signature verification failed"
            ) from error

    def verify(self, signed: SignedSealedStatusFact) -> None:
        """Verify one signed fact, raising on any mismatch."""
        if not isinstance(signed, SignedSealedStatusFact):
            raise SignatureVerificationError("signed must be a SignedSealedStatusFact")
        self.verify_bytes(signed.signed_bytes, signed.signature)


class SealedStatusMirror:
    """Read-only workspace mirror of verified authority decisions.

    Only signature-verified facts enter the mirror; identical facts are
    idempotent, and a fact ID reused for different content is rejected.  The
    mirror never derives or repairs authority decisions on its own.
    """

    def __init__(self, verifier: AuthorityVerifier) -> None:
        if not isinstance(verifier, AuthorityVerifier):
            raise ProtectedTestError("verifier must be an AuthorityVerifier")
        self._verifier = verifier
        self._facts: dict[str, SignedSealedStatusFact] = {}

    def record(self, signed: SignedSealedStatusFact) -> None:
        """Verify and store one signed fact, or reject a conflicting replay."""
        self._verifier.verify(signed)
        existing = self._facts.get(signed.fact.fact_id)
        if existing is not None:
            if existing.fact.content_sha256 == signed.fact.content_sha256:
                return
            raise FactConflictError(
                f"fact ID {signed.fact.fact_id!r} is already recorded"
            )
        self._facts[signed.fact.fact_id] = signed

    def record_all(self, signed_facts: tuple[SignedSealedStatusFact, ...]) -> None:
        """Verify and store a sequence of signed facts."""
        for signed in signed_facts:
            self.record(signed)

    def facts(self) -> tuple[SignedSealedStatusFact, ...]:
        """Return every stored fact in first-recorded order."""
        return tuple(self._facts.values())

    def get(self, fact_id: str) -> SignedSealedStatusFact | None:
        """Return the stored fact for a fact ID, or ``None``."""
        return self._facts.get(fact_id)

    def kinds(self, submission_id: str) -> tuple[SealedStatusKind, ...]:
        """Return the recorded fact kinds for one submission, in order."""
        return tuple(
            signed.fact.kind
            for signed in self._facts.values()
            if signed.fact.submission_id == submission_id
        )


__all__ = [
    "FACT_SIGNING_DOMAIN",
    "FACT_SCHEMA",
    "AuthorityVerifier",
    "SIGNATURE_SCHEME",
    "SealedStatusFact",
    "SealedStatusMirror",
    "SignedSealedStatusFact",
    "fact_signing_bytes",
]
