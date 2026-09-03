"""Capability status values shared by Thesis integration adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

CapabilityReason = Literal[
    "available",
    "not_configured",
    "not_implemented",
    "not_returned",
    "unauthenticated",
    "unavailable",
    "transport_unavailable",
    "protocol_error",
]


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """Redacted status for one externally provided capability."""

    capability: str
    available: bool
    reason: CapabilityReason
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if not self.capability or self.capability != self.capability.strip():
            raise ValueError("capability must be non-empty and trimmed")
        if self.available != (self.reason == "available"):
            raise ValueError("available must agree with the capability reason")


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """A deterministic snapshot of the capabilities exposed by an adapter."""

    statuses: tuple[CapabilityStatus, ...]
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        statuses = tuple(self.statuses)
        if not all(isinstance(status, CapabilityStatus) for status in statuses):
            raise TypeError("statuses must contain CapabilityStatus values")
        names = tuple(status.capability for status in statuses)
        if names != tuple(sorted(names)):
            raise ValueError("capability statuses must be sorted by name")
        if len(names) != len(set(names)):
            raise ValueError("capability statuses must be unique")
        object.__setattr__(self, "statuses", statuses)

    @classmethod
    def from_statuses(cls, statuses: Iterable[CapabilityStatus]) -> CapabilityReport:
        """Build a report with stable ordering and duplicate protection."""
        return cls(tuple(sorted(statuses, key=lambda status: status.capability)))

    def status(self, capability: str) -> CapabilityStatus:
        """Return one status or an explicit unavailable status for an unknown name."""
        for status in self.statuses:
            if status.capability == capability:
                return status
        return CapabilityStatus(capability, False, "not_configured")

    def __getitem__(self, capability: str) -> CapabilityStatus:
        return self.status(capability)

    @property
    def available(self) -> bool:
        """Return whether every declared capability is available."""
        return bool(self.statuses) and all(status.available for status in self.statuses)

    def as_mapping(self) -> Mapping[str, CapabilityStatus]:
        """Return statuses keyed by capability for service-layer composition."""
        return {status.capability: status for status in self.statuses}


def unavailable_report(
    capabilities: Iterable[str],
    *,
    reason: CapabilityReason = "not_configured",
) -> CapabilityReport:
    """Create a fail-closed report without probing or emulating a service."""
    return CapabilityReport.from_statuses(
        CapabilityStatus(capability, False, reason) for capability in capabilities
    )


__all__ = [
    "CapabilityReason",
    "CapabilityReport",
    "CapabilityStatus",
    "unavailable_report",
]
