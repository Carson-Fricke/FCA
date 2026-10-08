"""Thesis adapters for the published quant-harness service boundary.

The harness is intentionally treated as an external service here.  Requests and
results are JSON-shaped values, not engine objects.  This keeps manifests,
metrics, and artifact descriptors opaque and prevents Thesis from accidentally
coupling itself to a planned or unavailable engine capability.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, Self, cast

import httpx

from thesis.integrations.capabilities import (
    CapabilityReason,
    CapabilityReport,
    CapabilityStatus,
    unavailable_report,
)

_HARNESS_CAPABILITIES = (
    "backtest",
    "result_query",
    "manifests",
    "metrics",
    "result_artifacts",
)

# Canonical harness HTTP routes.  The launch route decodes a backtest
# configuration document, jobs expose lifecycle state, and the job and its
# artifacts expose the manifest, metrics, and artifact references.
_HEALTH_ROUTE = "healthz"
_BACKTEST_JOB_ROUTE = "v1/backtests/jobs"
_JOB_ROUTE = "v1/jobs"


class HarnessError(RuntimeError):
    """Base error for an invalid or unavailable harness operation."""


class HarnessProtocolError(HarnessError):
    """The injected process or API response violates the harness contract."""


class HarnessCapabilityUnavailable(HarnessError):
    """Raised when a requested harness capability is not actually available."""

    def __init__(self, capability: str, report: CapabilityReport) -> None:
        self.capability = capability
        self.report = report
        super().__init__(f"quant-harness capability {capability!r} is unavailable")


class HarnessApiError(HarnessError):
    """An authenticated harness API error response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: object | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.details = details
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HarnessCredentials:
    """Process-local bearer credentials for a remote harness API."""

    bearer_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or not self.bearer_token.strip():
            raise ValueError("bearer_token must be a non-empty string")
        object.__setattr__(self, "bearer_token", self.bearer_token.strip())

    def __repr__(self) -> str:
        return "HarnessCredentials(bearer_token='<redacted>')"


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """Opaque, deterministic input accepted by the published backtest API."""

    job_id: str
    configuration: object
    strategy_bundle: object | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        object.__setattr__(self, "job_id", self.job_id.strip())
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_payload(self) -> dict[str, object]:
        """Serialize only published request fields, preserving configuration data."""
        return {
            "job_id": self.job_id,
            "configuration": _json_value(self.configuration),
            "strategy_bundle": _json_value(self.strategy_bundle),
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class HarnessBacktestResult:
    """Inspectable backtest response with opaque result values preserved."""

    job_id: str
    state: str
    manifest: object | None
    metrics: object | None
    result_artifacts: object | None
    capability_status: CapabilityReport
    raw: Mapping[str, object]

    @property
    def artifacts(self) -> object | None:
        """Alias for the published result-artifact field."""
        return self.result_artifacts

    @property
    def capabilities(self) -> CapabilityReport:
        """Return the capability status carried by the service response."""
        return self.capability_status


class HarnessHttpTransport(Protocol):
    """HTTP request surface implemented by ``httpx.Client`` or a test fake."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: object | None = None,
        timeout: float,
    ) -> object: ...


class HarnessClientPort(Protocol):
    """Operations Thesis needs from either local or remote harness service."""

    def capabilities(self) -> CapabilityReport: ...

    def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult: ...

    def query_backtest(self, job_id: str) -> HarnessBacktestResult: ...


class ThesisHarnessClient:
    """Common Thesis-facing client over an injected harness adapter."""

    def __init__(self, adapter: HarnessClientPort) -> None:
        _require_harness_port(adapter)
        self._adapter = adapter

    def capabilities(self) -> CapabilityReport:
        """Return service-declared capability status without fallback behavior."""
        return self._adapter.capabilities()

    def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult:
        """Launch one backtest only when the service declares it available."""
        report = self.capabilities()
        if not report.status("backtest").available:
            raise HarnessCapabilityUnavailable("backtest", report)
        return self._adapter.launch_backtest(request)

    def query_backtest(self, job_id: str) -> HarnessBacktestResult:
        """Query one result only when the service declares result querying."""
        report = self.capabilities()
        if not report.status("result_query").available:
            raise HarnessCapabilityUnavailable("result_query", report)
        return self._adapter.query_backtest(job_id)

    launch = launch_backtest
    query = query_backtest


class HarnessApiAdapter:
    """JSON HTTP adapter shared by local and authenticated remote services."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if bearer_token is not None and not bearer_token.strip():
            raise ValueError("bearer_token must be non-empty when supplied")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=(
                {}
                if bearer_token is None
                else {"Authorization": f"Bearer {bearer_token.strip()}"}
            ),
            transport=transport,
            timeout=timeout,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def capabilities(self) -> CapabilityReport:
        """Declare the capabilities a healthy canonical harness service publishes.

        The canonical harness API exposes public liveness at ``healthz`` and
        serves the launch, job, run, and artifact routes those capabilities
        name.  A service that is absent, unauthenticated, or unpublished maps
        to an explicit fail-closed report instead of a fabricated capability
        set.
        """
        try:
            response = self._http.get(_HEALTH_ROUTE)
        except httpx.HTTPError:
            return unavailable_report(
                _HARNESS_CAPABILITIES, reason="transport_unavailable"
            )
        if response.status_code in {401, 403}:
            return unavailable_report(_HARNESS_CAPABILITIES, reason="unauthenticated")
        if response.status_code == 404:
            return unavailable_report(_HARNESS_CAPABILITIES, reason="not_implemented")
        if response.is_error:
            raise _api_error(response)
        return _available_report()

    def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult:
        """Launch one backtest as a canonical ``POST /v1/backtests/jobs`` job."""
        report = self.capabilities()
        if not report.status("backtest").available:
            raise HarnessCapabilityUnavailable("backtest", report)
        response = self._http.post(_BACKTEST_JOB_ROUTE, json=_launch_document(request))
        result = _api_result(response, "backtest", fallback=report)
        if result.job_id != request.job_id:
            raise HarnessProtocolError(
                f"harness acknowledged job_id {result.job_id!r} for submitted "
                f"job_id {request.job_id!r}"
            )
        return result

    def query_backtest(self, job_id: str) -> HarnessBacktestResult:
        """Retrieve one canonical job and enrich it from its artifacts."""
        report = self.capabilities()
        if not report.status("result_query").available:
            raise HarnessCapabilityUnavailable("result_query", report)
        identifier = _job_id(job_id)
        response = self._http.get(f"{_JOB_ROUTE}/{identifier}")
        job = _api_result(response, "result_query", fallback=report)
        if job.job_id != identifier:
            raise HarnessProtocolError(
                f"harness returned job_id {job.job_id!r} for requested "
                f"job_id {identifier!r}"
            )
        artifacts = self._optional_object(f"{_JOB_ROUTE}/{identifier}/artifacts")
        manifest, metrics, result_artifacts = _result_documents(job, artifacts)
        return HarnessBacktestResult(
            job_id=job.job_id,
            state=job.state,
            manifest=manifest,
            metrics=metrics,
            result_artifacts=result_artifacts,
            capability_status=report,
            raw=job.raw,
        )

    def _optional_object(self, route: str) -> dict[str, object] | None:
        """Fetch one companion document, or ``None`` when it is unpublished."""
        response = self._http.get(route)
        if response.status_code in {404, 501}:
            return None
        if response.is_error:
            raise _api_error(response)
        return _response_object(response)


class RemoteHarnessApiAdapter(HarnessApiAdapter):
    """Bearer-authenticated remote harness API adapter."""

    def __init__(
        self,
        base_url: str,
        credentials: HarnessCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(credentials, HarnessCredentials):
            raise TypeError("credentials must be HarnessCredentials")
        if not base_url.lower().startswith("https://"):
            raise ValueError("remote harness base_url must use HTTPS")
        super().__init__(
            base_url,
            bearer_token=credentials.bearer_token,
            transport=transport,
            timeout=timeout,
        )


def _api_result(
    response: httpx.Response,
    capability: str,
    *,
    fallback: CapabilityReport,
) -> HarnessBacktestResult:
    if response.status_code in {401, 403, 404, 501}:
        report = unavailable_report(
            _HARNESS_CAPABILITIES,
            reason=(
                "unauthenticated"
                if response.status_code in {401, 403}
                else "not_implemented"
            ),
        )
        raise HarnessCapabilityUnavailable(capability, report)
    if response.is_error:
        raise _api_error(response)
    try:
        return _backtest_result(
            _response_object(response), capability, fallback=fallback
        )
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise HarnessProtocolError("harness backtest response is invalid") from error


def _backtest_result(
    body: dict[str, object],
    capability: str,
    *,
    fallback: CapabilityReport,
) -> HarnessBacktestResult:
    job_id = body.get("job_id")
    state = body.get("state")
    if not isinstance(job_id, str) or not job_id.strip():
        raise HarnessProtocolError("harness result job_id must be a non-empty string")
    if not isinstance(state, str) or not state.strip():
        raise HarnessProtocolError("harness result state must be a non-empty string")
    raw_capabilities = body.get("capabilities", body.get("capability_status"))
    report = (
        _capability_report(raw_capabilities)
        if isinstance(raw_capabilities, (dict, list))
        else fallback
    )
    if not report.status(capability).available:
        raise HarnessCapabilityUnavailable(capability, report)
    return HarnessBacktestResult(
        job_id=job_id.strip(),
        state=state.strip(),
        manifest=body.get("manifest"),
        metrics=body.get("metrics"),
        result_artifacts=body.get("result_artifacts", body.get("artifacts")),
        capability_status=report,
        raw=dict(body),
    )


def _available_report() -> CapabilityReport:
    """Declare the fixed capability set served by a healthy canonical harness."""
    return CapabilityReport.from_statuses(
        CapabilityStatus(capability, True, "available")
        for capability in _HARNESS_CAPABILITIES
    )


def _launch_document(request: BacktestRequest) -> dict[str, object]:
    """Render the canonical backtest configuration document for the launch route.

    The harness launch route decodes a ``shell: backtest`` configuration
    document, not a client envelope, so the submitted ``configuration`` is
    forwarded as-is with the request's ``job_id`` applied.
    """
    configuration = request.configuration
    if not isinstance(configuration, Mapping):
        raise HarnessProtocolError("backtest configuration must be a JSON object")
    document = {str(key): _json_value(value) for key, value in configuration.items()}
    document["job_id"] = request.job_id
    return document


def _result_documents(
    job: HarnessBacktestResult,
    artifacts: Mapping[str, object] | None,
) -> tuple[object | None, object | None, object | None]:
    """Assemble manifest, metrics, and artifact references from one job row."""
    metrics = job.raw.get("metrics") if isinstance(job.raw, Mapping) else None
    entries: object | None = None
    if isinstance(artifacts, Mapping) and isinstance(artifacts.get("artifacts"), list):
        entries = artifacts["artifacts"]
    manifest: object | None = None
    if isinstance(entries, list):
        manifest = {"manifestId": job.job_id, "artifacts": entries}
    return manifest, metrics, entries


def _capability_report(body: Mapping[str, object] | list[object]) -> CapabilityReport:
    """Parse either the published mapping or list representation."""
    raw = (
        body.get("capabilities", body.get("statuses", body))
        if isinstance(body, Mapping)
        else body
    )
    statuses: list[CapabilityStatus] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                raise TypeError("capability entries must be objects")
            name = item.get("capability")
            if not isinstance(name, str):
                raise TypeError("capability entries require a capability name")
            statuses.append(_status(name, item.get("available"), item.get("reason")))
    elif isinstance(raw, Mapping):
        for name, value in raw.items():
            if isinstance(value, Mapping):
                statuses.append(
                    _status(name, value.get("available"), value.get("reason"))
                )
            elif isinstance(value, bool):
                statuses.append(_status(name, value, None))
            else:
                raise TypeError("capability values must be objects or booleans")
    else:
        raise TypeError("capabilities must be an object or array")
    return CapabilityReport.from_statuses(statuses)


def _status(name: object, available: object, reason: object) -> CapabilityStatus:
    if not isinstance(name, str) or not name.strip():
        raise TypeError("capability name must be a non-empty string")
    if not isinstance(available, bool):
        raise TypeError(f"capability {name!r} available must be a boolean")
    if available:
        normalized: CapabilityReason = "available"
    elif reason in {
        "not_configured",
        "not_implemented",
        "not_returned",
        "unauthenticated",
        "unavailable",
        "transport_unavailable",
        "protocol_error",
    }:
        normalized = cast(CapabilityReason, reason)
    else:
        normalized = "unavailable"
    return CapabilityStatus(name.strip(), available, normalized)


def _response_object(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as error:
        raise HarnessProtocolError("harness response is not valid JSON") from error
    return _json_object(body)


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("harness response must be a JSON object")
    return value


def _api_error(response: httpx.Response) -> HarnessApiError:
    code = "http_error"
    message = response.reason_phrase or "quant-harness request failed"
    details: object | None = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            if isinstance(error.get("code"), str):
                code = error["code"]
            if isinstance(error.get("message"), str):
                message = error["message"]
            details = error.get("details")
    return HarnessApiError(response.status_code, code, message, details=details)


def _json_value(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return cast(object, model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    return value


def _require_harness_port(adapter: object) -> None:
    for name in ("capabilities", "launch_backtest", "query_backtest"):
        if not callable(getattr(adapter, name, None)):
            raise TypeError(
                f"adapter must implement HarnessClientPort ({name} is missing)"
            )


def _command(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("job_id must be a non-empty string")
    return value.strip()


def _looks_unimplemented(value: str) -> bool:
    lowered = value.casefold()
    return "not implemented" in lowered or "scaffolded" in lowered


RemoteHarnessClient = RemoteHarnessApiAdapter
HarnessBacktest = HarnessBacktestResult


__all__ = [
    "BacktestRequest",
    "HarnessApiAdapter",
    "HarnessApiError",
    "HarnessBacktest",
    "HarnessBacktestResult",
    "HarnessCapabilityUnavailable",
    "HarnessClientPort",
    "HarnessCredentials",
    "HarnessError",
    "HarnessHttpTransport",
    "HarnessProtocolError",
    "RemoteHarnessApiAdapter",
    "RemoteHarnessClient",
    "ThesisHarnessClient",
]
