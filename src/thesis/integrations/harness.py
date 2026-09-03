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

    run_id: str
    configuration: object
    strategy_bundle: object | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        object.__setattr__(self, "run_id", self.run_id.strip())
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_payload(self) -> dict[str, object]:
        """Serialize only published request fields, preserving configuration data."""
        return {
            "run_id": self.run_id,
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


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Minimal result surface returned by an injected local process transport."""

    returncode: int
    stdout: str
    stderr: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.returncode, int) or isinstance(self.returncode, bool):
            raise TypeError("returncode must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("process output must be text")


class ProcessTransport(Protocol):
    """Injected local process boundary; no subprocess is created by this module."""

    def run(
        self,
        command: str,
        payload: Mapping[str, object],
        *,
        timeout: float,
    ) -> ProcessResult: ...


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


class LocalHarnessProcessAdapter:
    """Adapter for a local ``api``/``backtest-job`` process boundary."""

    def __init__(
        self,
        transport: ProcessTransport,
        *,
        api_command: str = "api",
        backtest_command: str = "backtest-job",
        timeout: float = 60.0,
    ) -> None:
        if not callable(getattr(transport, "run", None)):
            raise TypeError("transport must implement ProcessTransport")
        if timeout <= 0 or isinstance(timeout, bool):
            raise ValueError("timeout must be positive")
        self._transport = transport
        self._api_command = _command(api_command, "api_command")
        self._backtest_command = _command(backtest_command, "backtest_command")
        self._timeout = timeout

    def capabilities(self) -> CapabilityReport:
        """Read capabilities from the local API process, fail-closed on absence."""
        try:
            result = self._run(self._api_command, {"operation": "capabilities"})
        except HarnessError:
            return unavailable_report(
                _HARNESS_CAPABILITIES, reason="transport_unavailable"
            )
        if result.returncode != 0:
            return unavailable_report(
                _HARNESS_CAPABILITIES,
                reason=(
                    "not_implemented"
                    if _looks_unimplemented(result.stderr)
                    else "transport_unavailable"
                ),
            )
        try:
            return _capability_report(_json_object(json.loads(result.stdout)))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise HarnessProtocolError(
                "local harness capabilities are invalid"
            ) from error

    def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult:
        """Submit a local job without emulating an unavailable runner."""
        report = self.capabilities()
        if not report.status("backtest").available:
            raise HarnessCapabilityUnavailable("backtest", report)
        result = self._run(
            self._backtest_command,
            {"operation": "launch_backtest", "request": request.to_payload()},
        )
        return _process_result(result, "backtest", fallback=report)

    def query_backtest(self, job_id: str) -> HarnessBacktestResult:
        """Query a local API job result, retaining all published result fields."""
        report = self.capabilities()
        if not report.status("result_query").available:
            raise HarnessCapabilityUnavailable("result_query", report)
        result = self._run(
            self._api_command,
            {"operation": "query_backtest", "job_id": _job_id(job_id)},
        )
        return _process_result(result, "result_query", fallback=report)

    def _run(self, command: str, payload: Mapping[str, object]) -> ProcessResult:
        try:
            result = self._transport.run(command, payload, timeout=self._timeout)
        except Exception as error:
            raise HarnessError(
                f"local harness process {command!r} is unavailable"
            ) from error
        if not isinstance(result, ProcessResult):
            raise HarnessProtocolError("process transport returned an invalid result")
        return result


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
        """Return declared capabilities, translating absent services to unavailable."""
        try:
            response = self._http.get("v1/capabilities")
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
        try:
            return _capability_report(_response_object(response))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise HarnessProtocolError(
                "harness capabilities response is invalid"
            ) from error

    def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult:
        """Launch a published backtest request over HTTP."""
        report = self.capabilities()
        if not report.status("backtest").available:
            raise HarnessCapabilityUnavailable("backtest", report)
        response = self._http.post("v1/backtests", json=request.to_payload())
        return _api_result(response, "backtest", fallback=report)

    def query_backtest(self, job_id: str) -> HarnessBacktestResult:
        """Retrieve a published backtest result over HTTP."""
        report = self.capabilities()
        if not report.status("result_query").available:
            raise HarnessCapabilityUnavailable("result_query", report)
        response = self._http.get(f"v1/backtests/{_job_id(job_id)}")
        return _api_result(response, "result_query", fallback=report)


class LocalHarnessApiAdapter(HarnessApiAdapter):
    """Unauthenticated local HTTP adapter for a loopback harness API."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        super().__init__(base_url, transport=transport, timeout=timeout)


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


def _process_result(
    result: ProcessResult,
    capability: str,
    *,
    fallback: CapabilityReport,
) -> HarnessBacktestResult:
    if result.returncode != 0:
        raise HarnessCapabilityUnavailable(
            capability,
            unavailable_report(
                _HARNESS_CAPABILITIES,
                reason=(
                    "not_implemented"
                    if _looks_unimplemented(result.stderr)
                    else "unavailable"
                ),
            ),
        )
    try:
        return _backtest_result(
            _json_object(json.loads(result.stdout)), capability, fallback=fallback
        )
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise HarnessProtocolError("local harness result is invalid") from error


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


LocalHarnessClient = LocalHarnessProcessAdapter
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
    "LocalHarnessApiAdapter",
    "LocalHarnessClient",
    "LocalHarnessProcessAdapter",
    "ProcessResult",
    "ProcessTransport",
    "RemoteHarnessApiAdapter",
    "RemoteHarnessClient",
    "ThesisHarnessClient",
]
