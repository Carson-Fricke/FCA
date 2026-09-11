"""Thesis-side inert status records over harness-published surfaces.

The harness owns portfolio tracking, OMS behavior, risk decisions, matching,
and supervision; Thesis only consumes the published status, manifest, and
artifact surfaces.  Every record in this module is plain data mapped from a
harness payload, and a surface the harness does not publish maps to
:class:`StatusSurface.unavailable` instead of fabricated values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from thesis.integrations.harness_activation import HarnessApiTransport

T = TypeVar("T")

StatusState = Literal["available", "unavailable"]

_SURFACE_UNAVAILABLE: Mapping[int, str] = {
    404: "not_found",
    501: "not_implemented",
    503: "unavailable",
}


class HarnessStatusError(RuntimeError):
    """A harness status surface violated the published contract."""


@dataclass(frozen=True, slots=True)
class StatusSurface[T]:
    """One consumed surface: either a mapped record or an explicit absence."""

    state: StatusState
    value: T | None = None
    reason: str | None = None

    @classmethod
    def available(cls, value: T) -> StatusSurface[T]:
        """Wrap one mapped record."""
        return cls(state="available", value=value)

    @classmethod
    def unavailable(cls, reason: str) -> StatusSurface[T]:
        """Wrap an explicit absence; no substitute data is fabricated."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("unavailable reason must be a non-empty string")
        return cls(state="unavailable", value=None, reason=reason.strip())

    def __post_init__(self) -> None:
        if self.state == "available":
            if self.value is None:
                raise ValueError("an available surface requires a value")
            if self.reason is not None:
                raise ValueError("an available surface carries no reason")
        elif self.state == "unavailable":
            if self.value is not None:
                raise ValueError("an unavailable surface carries no value")
            if self.reason is None:
                raise ValueError("an unavailable surface requires a reason")
        else:
            raise ValueError("state must be 'available' or 'unavailable'")


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """One harness job status row, with published timestamp text preserved."""

    request_ref: str
    state: str
    created_at: str
    updated_at: str
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestSummary:
    """One harness run row: opaque digests, metrics, and artifact totals."""

    run_id: str
    status: str
    strategy_id: str
    data_start: str | None = None
    data_end: str | None = None
    run_digest: str | None = None
    configuration_digest: str | None = None
    artifact_count: int = 0
    total_bytes: int = 0
    metrics: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """One stored run artifact in the harness reproducibility-manifest shape."""

    name: str
    uri: str
    sha256: str
    size_bytes: int
    sas_url: str | None = None


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """One published position as exact-decimal text; no arithmetic here."""

    symbol: str
    quantity: str
    average_price: str | None = None


@dataclass(frozen=True, slots=True)
class BookRecord:
    """One published book with its cash, equity, and positions."""

    name: str
    cash: str
    equity: str
    positions: tuple[PositionRecord, ...]


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    """The published portfolio snapshot: books, cash, and equity."""

    books: tuple[BookRecord, ...]
    cash: str
    equity: str


@dataclass(frozen=True, slots=True)
class RiskDecisionRecord:
    """One published pre-trade risk decision, audited by the harness."""

    rule: str
    decision: str
    reason: str | None = None


class RuntimeStatusClient:
    """Consumer of the harness-published status surfaces over a transport.

    The client maps payloads into inert Thesis records and never emulates a
    surface the harness does not publish.  The portfolio and risk-decision
    routes belong to the planned harness runtime status reporting; until a
    deployment publishes them, they return an explicit unavailable surface.
    """

    def __init__(self, base_url: str, transport: HarnessApiTransport) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not callable(getattr(transport, "get", None)):
            raise TypeError("transport must implement HarnessApiTransport (get)")
        self._base_url = base_url.strip().rstrip("/")
        self._transport = transport

    def status(
        self, request_ref: str, *, bearer_token: str | None = None
    ) -> StatusSurface[RuntimeStatus]:
        """Consume one harness job status row."""
        return self._fetch(
            f"/v1/backtests/jobs/{_reference(request_ref, 'request_ref')}",
            bearer_token,
            _runtime_status,
        )

    def manifests(
        self, bundle_ref: str, *, bearer_token: str | None = None
    ) -> StatusSurface[ManifestSummary]:
        """Consume one harness run row as the bundle's manifest summary."""
        return self._fetch(
            f"/v1/backtests/runs/{_reference(bundle_ref, 'bundle_ref')}",
            bearer_token,
            _manifest_summary,
        )

    def artifacts(
        self, bundle_ref: str, *, bearer_token: str | None = None
    ) -> StatusSurface[tuple[ArtifactEntry, ...]]:
        """Consume the artifact summaries stored below one run prefix."""
        return self._fetch(
            f"/v1/backtests/runs/{_reference(bundle_ref, 'bundle_ref')}/artifacts",
            bearer_token,
            _artifact_entries,
        )

    def portfolio_summary(
        self, *, bearer_token: str | None = None
    ) -> StatusSurface[PortfolioSummary]:
        """Consume the published portfolio snapshot when the harness has one."""
        return self._fetch("/v1/runtime/portfolio", bearer_token, _portfolio_summary)

    def risk_decisions(
        self, request_ref: str, *, bearer_token: str | None = None
    ) -> StatusSurface[tuple[RiskDecisionRecord, ...]]:
        """Consume the audited risk decisions recorded for one request."""
        return self._fetch(
            f"/v1/runtime/risk-decisions/{_reference(request_ref, 'request_ref')}",
            bearer_token,
            _risk_decisions,
        )

    def _fetch(
        self,
        route: str,
        bearer_token: str | None,
        mapper: Callable[[Mapping[str, object]], T],
    ) -> StatusSurface[T]:
        try:
            response = self._transport.get(
                f"{self._base_url}{route}", _headers(bearer_token)
            )
        except HarnessStatusError:
            raise
        except Exception as error:
            raise HarnessStatusError(
                "the harness status surface could not be reached"
            ) from error
        status_code = response.status_code
        unavailable_reason = _SURFACE_UNAVAILABLE.get(status_code)
        if unavailable_reason is not None:
            return StatusSurface.unavailable(unavailable_reason)
        if status_code in (401, 403):
            raise HarnessStatusError(
                f"the harness rejected the status credential (HTTP {status_code})"
            )
        if status_code < 200 or status_code >= 300:
            raise HarnessStatusError(
                f"unexpected harness status response (HTTP {status_code})"
            )
        return StatusSurface.available(mapper(response.body))


def _headers(bearer_token: str | None) -> Mapping[str, str]:
    if bearer_token is None:
        return {}
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise HarnessStatusError(
            "bearer_token must be a non-empty string when supplied"
        )
    return {"Authorization": f"Bearer {bearer_token.strip()}"}


def _reference(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessStatusError(f"{name} must be a non-empty string")
    return value.strip()


def _require_object(body: object) -> Mapping[str, object]:
    if not isinstance(body, Mapping) or any(not isinstance(key, str) for key in body):
        raise HarnessStatusError("harness status payloads must be JSON objects")
    return body


def _required_text(body: Mapping[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessStatusError(
            f"harness status field {key!r} must be a non-empty string"
        )
    return value.strip()


def _optional_text(body: Mapping[str, object], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarnessStatusError(
            f"harness status field {key!r} must be a string when present"
        )
    return value


def _required_int(body: Mapping[str, object], key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HarnessStatusError(
            f"harness status field {key!r} must be a non-negative integer"
        )
    return value


def _text_mapping(body: Mapping[str, object], key: str) -> dict[str, str]:
    value = body.get(key, {})
    if not isinstance(value, Mapping):
        raise HarnessStatusError(f"harness status field {key!r} must be an object")
    for _entry_key, entry_value in value.items():
        if not isinstance(entry_value, str):
            raise HarnessStatusError(
                f"harness status field {key!r} values must be strings"
            )
    return {str(entry_key): entry_value for entry_key, entry_value in value.items()}


def _entries(body: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = body.get(key)
    if not isinstance(value, (list, tuple)):
        raise HarnessStatusError(f"harness status field {key!r} must be an array")
    for entry in value:
        if not isinstance(entry, Mapping):
            raise HarnessStatusError(
                f"harness status field {key!r} entries must be objects"
            )
    return tuple(value)


def _runtime_status(body: Mapping[str, object]) -> RuntimeStatus:
    return RuntimeStatus(
        request_ref=_required_text(body, "job_id"),
        state=_required_text(body, "state"),
        created_at=_required_text(body, "created_at"),
        updated_at=_required_text(body, "updated_at"),
        error_message=_optional_text(body, "error_message"),
    )


def _manifest_summary(body: Mapping[str, object]) -> ManifestSummary:
    return ManifestSummary(
        run_id=_required_text(body, "run_id"),
        status=_required_text(body, "status"),
        strategy_id=_required_text(body, "strategy_id"),
        data_start=_optional_text(body, "data_start"),
        data_end=_optional_text(body, "data_end"),
        run_digest=_optional_text(body, "run_digest"),
        configuration_digest=_optional_text(body, "configuration_digest"),
        artifact_count=_required_int(body, "artifact_count"),
        total_bytes=_required_int(body, "total_bytes"),
        metrics=_text_mapping(body, "metrics"),
    )


def _artifact_entries(
    body: Mapping[str, object],
) -> tuple[ArtifactEntry, ...]:
    return tuple(
        ArtifactEntry(
            name=_required_text(entry, "name"),
            uri=_required_text(entry, "uri"),
            sha256=_required_text(entry, "sha256"),
            size_bytes=_required_int(entry, "sizeBytes"),
            sas_url=_optional_text(entry, "sas_url"),
        )
        for entry in _entries(body, "artifacts")
    )


def _portfolio_summary(body: Mapping[str, object]) -> PortfolioSummary:
    books = tuple(
        BookRecord(
            name=_required_text(entry, "name"),
            cash=_required_text(entry, "cash"),
            equity=_required_text(entry, "equity"),
            positions=tuple(
                PositionRecord(
                    symbol=_required_text(position, "symbol"),
                    quantity=_required_text(position, "quantity"),
                    average_price=_optional_text(position, "average_price"),
                )
                for position in _entries(entry, "positions")
            ),
        )
        for entry in _entries(body, "books")
    )
    return PortfolioSummary(
        books=books,
        cash=_required_text(body, "cash"),
        equity=_required_text(body, "equity"),
    )


def _risk_decisions(
    body: Mapping[str, object],
) -> tuple[RiskDecisionRecord, ...]:
    return tuple(
        RiskDecisionRecord(
            rule=_required_text(entry, "rule"),
            decision=_required_text(entry, "decision"),
            reason=_optional_text(entry, "reason"),
        )
        for entry in _entries(body, "decisions")
    )


__all__ = [
    "ArtifactEntry",
    "BookRecord",
    "HarnessStatusError",
    "ManifestSummary",
    "PortfolioSummary",
    "PositionRecord",
    "RiskDecisionRecord",
    "RuntimeStatus",
    "RuntimeStatusClient",
    "StatusState",
    "StatusSurface",
]
