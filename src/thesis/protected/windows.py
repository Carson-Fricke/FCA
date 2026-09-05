"""Preallocated hidden window records visible to the Thesis client.

The authority preallocates a pool of protected windows with a versioned,
strategy-independent policy and never sends window contents to Thesis.  The
client only ever receives :class:`HiddenWindowRef` records: a stable window
identifier, the allocation-policy version that selected it, and an opaque
commitment digest.  There is intentionally no time range, asset, or payload
field on this record because Thesis must not be able to reconstruct or
guess the hidden allocation from anything it stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from thesis.protected.contracts import AuthorityProtocolError

_WINDOW_REF_SCHEMA = "fca-thesis-hidden-window-ref-v1"
_ALLOCATION_SCHEMA = "fca-thesis-window-allocation-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise AuthorityProtocolError(f"{label} must be a safe identifier")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise AuthorityProtocolError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _freeze(value: dict[str, object], schema: str) -> dict[str, object]:
    document = dict(value)
    document["schema"] = schema
    return document


@dataclass(frozen=True, slots=True)
class HiddenWindowRef:
    """A client-visible reference to one preallocated hidden window.

    The commitment digest is the authority's opaque commitment to the hidden
    window contents.  The contents themselves never cross this boundary.
    """

    window_id: str
    allocation_policy_version: str
    commitment_sha256: str
    schema: str = _WINDOW_REF_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", _identifier(self.window_id, "window_id"))
        object.__setattr__(
            self,
            "allocation_policy_version",
            _identifier(self.allocation_policy_version, "allocation_policy_version"),
        )
        object.__setattr__(
            self,
            "commitment_sha256",
            _digest(self.commitment_sha256, "commitment_sha256"),
        )

    def to_payload(self) -> dict[str, object]:
        """Return the JSON payload shape used inside authority fact payloads."""
        return {
            "allocationPolicyVersion": self.allocation_policy_version,
            "commitmentSha256": self.commitment_sha256,
            "schema": self.schema,
            "windowId": self.window_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> HiddenWindowRef:
        """Build a window reference from an authority fact payload entry."""
        if not isinstance(value, dict):
            raise AuthorityProtocolError("window reference must be a mapping")
        schema = value.get("schema", _WINDOW_REF_SCHEMA)
        if not isinstance(schema, str):
            raise AuthorityProtocolError("window reference schema must be a string")
        return cls(
            window_id=_require(value, "windowId"),
            allocation_policy_version=_require(value, "allocationPolicyVersion"),
            commitment_sha256=_require(value, "commitmentSha256"),
            schema=schema,
        )


@dataclass(frozen=True, slots=True)
class WindowAllocation:
    """The client-visible record of one preallocated hidden-window grant."""

    submission_id: str
    policy_version: str
    windows: tuple[HiddenWindowRef, ...]
    schema: str = _ALLOCATION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        object.__setattr__(
            self, "policy_version", _identifier(self.policy_version, "policy_version")
        )
        windows = tuple(self.windows)
        for window in windows:
            if not isinstance(window, HiddenWindowRef):
                raise AuthorityProtocolError(
                    "windows must contain HiddenWindowRef values"
                )
        seen: set[str] = set()
        for window in windows:
            if window.window_id in seen:
                raise AuthorityProtocolError(
                    f"window {window.window_id!r} is allocated twice"
                )
            seen.add(window.window_id)
        object.__setattr__(self, "windows", windows)

    def to_payload(self) -> dict[str, object]:
        """Return the JSON payload shape embedded in eligibility facts."""
        return _freeze(
            {
                "policyVersion": self.policy_version,
                "submissionId": self.submission_id,
                "windows": [window.to_payload() for window in self.windows],
            },
            self.schema,
        )

    @classmethod
    def from_payload(cls, value: object) -> WindowAllocation:
        """Build an allocation from an authority fact payload mapping."""
        if not isinstance(value, dict):
            raise AuthorityProtocolError("window allocation must be a mapping")
        entries = value.get("windows")
        if not isinstance(entries, list):
            raise AuthorityProtocolError("window allocation must list windows")
        windows = tuple(HiddenWindowRef.from_payload(entry) for entry in entries)
        schema = value.get("schema", _ALLOCATION_SCHEMA)
        if not isinstance(schema, str):
            raise AuthorityProtocolError("window allocation schema must be a string")
        return cls(
            submission_id=_require(value, "submissionId"),
            policy_version=_require(value, "policyVersion"),
            windows=windows,
            schema=schema,
        )


def _require(value: dict[str, object], key: str) -> Any:
    if key not in value:
        raise AuthorityProtocolError(f"payload is missing {key!r}")
    return value[key]


__all__ = ["HiddenWindowRef", "WindowAllocation"]
