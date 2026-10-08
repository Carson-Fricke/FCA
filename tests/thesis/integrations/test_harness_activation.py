"""Focused activation identity coverage for the Thesis-harness boundary.

The caller allocates one execution ``job_id`` before submission.  The
activation request carries it, the remote boundary fails closed on a
mismatched acknowledgement, and a transport failure never replaces the
preallocated identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from thesis.integrations.harness_activation import (
    ActivationAdapter,
    ActivationOutcome,
    HarnessActivationError,
    HarnessActivationProtocolError,
    HarnessActivationRequest,
    HarnessApiResponse,
    HarnessCapabilities,
    RemoteHarnessApi,
)

BUNDLE = "b" * 64
JOB_ID = "job-0123456789abcdef0123456789abcdef"


class RecordingTransport:
    """HarnessApiTransport double recording posted payloads."""

    def __init__(self, response: HarnessApiResponse | Exception) -> None:
        self._response = response
        self.payloads: list[Mapping[str, object]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse:
        raise AssertionError(f"unexpected GET {url}")

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HarnessApiResponse:
        self.payloads.append(dict(payload))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class RecordingBoundary:
    """HarnessBoundary double recording activation requests."""

    def __init__(self) -> None:
        self.requests: list[HarnessActivationRequest] = []

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(backtest=True, trader=True)

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        self.requests.append(request)
        return _outcome(request)

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        self.requests.append(request)
        return _outcome(request)


def _outcome(request: HarnessActivationRequest) -> ActivationOutcome:
    return ActivationOutcome(
        action=request.action,
        bundle_ref=request.bundle_ref,
        environment=request.environment,
        request_payload=request.to_payload(),
        exit_code=0,
        job_id=request.job_id,
        state="queued",
        raw={},
    )


def _request(job_id: str | None = JOB_ID) -> HarnessActivationRequest:
    return HarnessActivationRequest(
        bundle_ref=BUNDLE,
        config_path="cfg/bt.yaml",
        action="backtest",
        job_id=job_id,
    )


def test_activation_payload_carries_the_preallocated_job_id() -> None:
    payload = _request().to_payload()

    assert payload["job_id"] == JOB_ID
    assert payload["action"] == "backtest"


def test_activation_request_rejects_a_blank_job_id() -> None:
    with pytest.raises(ValueError, match="job_id"):
        _request(job_id="   ")


def test_trader_activation_payload_omits_absent_job_id() -> None:
    request = HarnessActivationRequest(
        bundle_ref=BUNDLE, config_path="cfg/trader.yaml", action="trader"
    )

    assert "job_id" not in request.to_payload()


def test_remote_activation_accepts_the_submitted_job_id() -> None:
    transport = RecordingTransport(
        HarnessApiResponse(200, {"job_id": JOB_ID, "state": "queued"})
    )
    boundary = RemoteHarnessApi("https://harness.test:9000", transport)

    outcome = boundary.run_backtest(_request(), bearer_token="secret")

    assert outcome.job_id == JOB_ID
    assert transport.payloads[-1]["job_id"] == JOB_ID


def test_remote_activation_fails_closed_on_a_mismatched_job_id() -> None:
    transport = RecordingTransport(
        HarnessApiResponse(200, {"job_id": "job-other", "state": "queued"})
    )
    boundary = RemoteHarnessApi("https://harness.test:9000", transport)

    with pytest.raises(HarnessActivationProtocolError, match="job-other"):
        boundary.run_backtest(_request(), bearer_token="secret")


def test_remote_activation_never_replaces_the_submitted_identity() -> None:
    transport = RecordingTransport(OSError("connection reset"))
    boundary = RemoteHarnessApi("https://harness.test:9000", transport)
    request = _request()

    with pytest.raises(HarnessActivationError):
        boundary.run_backtest(request, bearer_token="secret")

    assert request.job_id == JOB_ID
    assert transport.payloads[-1]["job_id"] == JOB_ID


def test_adapter_requires_job_id_for_backtest() -> None:
    boundary = RecordingBoundary()
    adapter = ActivationAdapter(boundary)

    with pytest.raises(TypeError):
        cast(Any, adapter.run_backtest)(BUNDLE, "cfg/bt.yaml")

    outcome = adapter.run_backtest(BUNDLE, "cfg/bt.yaml", job_id=JOB_ID)

    assert outcome.job_id == JOB_ID
    assert boundary.requests[-1].job_id == JOB_ID


def test_adapter_trader_activation_has_no_job_id_requirement() -> None:
    boundary = RecordingBoundary()
    adapter = ActivationAdapter(boundary)

    outcome = adapter.launch_trader(BUNDLE, "cfg/trader.yaml")

    assert outcome.job_id is None
    assert boundary.requests[-1].job_id is None
