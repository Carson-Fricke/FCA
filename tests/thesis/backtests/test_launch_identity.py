"""Backtest launch allocates one execution job_id before harness submission."""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from thesis.http.backtests import (
    BacktestDataService,
    InMemoryBacktestStore,
    LaunchBacktestRequest,
)
from thesis.integrations.harness_activation import (
    ActivationOutcome,
    HarnessActivationError,
    HarnessActivationProtocolError,
    HarnessActivationRequest,
    HarnessCapabilities,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{32}$")


@dataclass
class RecordingBoundary:
    """HarnessBoundary double returning an optionally divergent job_id.

    ``state`` and ``metrics`` shape a terminal receipt that carries a published
    metrics document; the default is the queued receipt a local harness returns.
    """

    acknowledged_job_id: str | None = None
    omit_job_id: bool = False
    failure: Exception | None = None
    state: str = "queued"
    metrics: Mapping[str, object] | None = None
    requests: list[HarnessActivationRequest] = field(default_factory=list)

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(backtest=True)

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        job_id: str | None = None if self.omit_job_id else request.job_id
        if self.acknowledged_job_id is not None:
            job_id = self.acknowledged_job_id
        raw: dict[str, object] = {"state": self.state}
        if job_id is not None:
            raw["job_id"] = job_id
        if self.metrics is not None:
            raw["metrics"] = dict(self.metrics)
        return ActivationOutcome(
            action=request.action,
            bundle_ref=request.bundle_ref,
            environment=request.environment,
            request_payload=request.to_payload(),
            job_id=job_id,
            state=self.state,
            raw=raw,
        )

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        raise AssertionError("trader activation is not part of backtest launch")


def _request() -> LaunchBacktestRequest:
    return LaunchBacktestRequest(
        bundle_ref="a" * 64, config_path="config/harness/backtest.yaml"
    )


def _service(boundary: RecordingBoundary) -> BacktestDataService:
    return BacktestDataService(store=InMemoryBacktestStore(), harness=boundary)


def test_launch_allocates_and_reuses_one_job_id() -> None:
    boundary = RecordingBoundary()
    service = _service(boundary)

    document = service.launch("workspace-one", _request())

    submitted = boundary.requests[0].job_id
    assert submitted is not None and JOB_ID_PATTERN.fullmatch(submitted)
    assert boundary.requests[0].to_payload()["job_id"] == submitted
    assert document["job_id"] == submitted
    assert service.store.list("workspace-one") == ()
    backtest = document["backtest"]
    assert isinstance(backtest, dict)
    assert backtest["job_id"] == submitted


def test_launch_allocates_a_fresh_job_id_per_execution() -> None:
    boundary = RecordingBoundary()
    service = _service(boundary)

    first = service.launch("workspace-one", _request())
    second = service.launch("workspace-one", _request())

    assert first["job_id"] != second["job_id"]
    assert service.store.list("workspace-one") == ()


def test_launch_rejects_a_mismatched_acknowledged_job_id() -> None:
    boundary = RecordingBoundary(acknowledged_job_id="job-other")
    service = _service(boundary)

    with pytest.raises(HarnessActivationProtocolError, match="job-other") as caught:
        service.launch("workspace-one", _request())

    submitted = boundary.requests[0].job_id
    assert caught.value.job_id == submitted
    assert len(boundary.requests) == 1
    assert service.store.list("workspace-one") == ()


def test_launch_rejects_an_absent_acknowledged_job_id() -> None:
    boundary = RecordingBoundary(omit_job_id=True)
    service = _service(boundary)

    with pytest.raises(HarnessActivationProtocolError) as caught:
        service.launch("workspace-one", _request())

    assert caught.value.job_id == boundary.requests[0].job_id
    assert len(boundary.requests) == 1
    assert service.store.list("workspace-one") == ()


def test_unknown_outcome_retains_the_submitted_job_id_without_replay() -> None:
    boundary = RecordingBoundary(failure=HarnessActivationError("transport lost"))
    service = _service(boundary)

    with pytest.raises(HarnessActivationError) as caught:
        service.launch("workspace-one", _request())

    submitted = boundary.requests[0].job_id
    assert submitted is not None and JOB_ID_PATTERN.fullmatch(submitted)
    assert caught.value.job_id == submitted
    assert len(boundary.requests) == 1
    assert service.store.list("workspace-one") == ()


def test_launch_ignores_result_aliases_for_identity() -> None:
    boundary = RecordingBoundary()
    service = _service(boundary)

    document = service.launch("workspace-one", _request())

    mapping: Mapping[str, object] = document
    assert "run_id" not in mapping
    assert "backtest_id" not in mapping


def test_terminal_launch_reads_the_canonical_equity_and_trades_blocks() -> None:
    metrics = json.loads(
        (FIXTURES / "closed_trades.metrics.json").read_text(encoding="utf-8")
    )
    service = _service(RecordingBoundary(state="succeeded", metrics=metrics))

    service.launch("workspace-one", _request())

    [row] = service.list_backtests("workspace-one")
    assert row["total_return"] == -0.0024
    assert row["sharpe_per_sample"] == -0.41
    assert row["max_drawdown"] == 0.02
    assert row["win_rate"] == pytest.approx(1 / 3)
    assert row["trades"] == 3
    # Canonical metrics publish no capital, so the launch record carries none.
    assert row["capital"] is None
