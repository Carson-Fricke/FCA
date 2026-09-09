"""Activation and runtime-status adapter tests over injected fake boundaries.

No network and no real harness processes are started: the local runner and
the HTTP transport are fakes, and every timestamp is fixed fixture text.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

import pytest

from thesis.integrations.harness import ProcessResult
from thesis.integrations.harness_activation import (
    ActivationAdapter,
    ActivationOutcome,
    CapabilityUnavailableError,
    HarnessActivationError,
    HarnessActivationProtocolError,
    HarnessActivationRequest,
    HarnessApiResponse,
    HarnessCapabilities,
    LocalHarnessProcess,
    RemoteHarnessApi,
)
from thesis.integrations.runtime_status import (
    ArtifactEntry,
    BookRecord,
    HarnessStatusError,
    ManifestSummary,
    PortfolioSummary,
    PositionRecord,
    RiskDecisionRecord,
    RuntimeStatus,
    RuntimeStatusClient,
    StatusSurface,
)

BUNDLE_REF = "bundle-sha256-abc123"
BACKTEST_CONFIG = "config/harness/backtest.yaml"
TRADER_CONFIG = "config/harness/trader.yaml"
BEARER = "session-bearer-secret"


class FakeRunner:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> ProcessResult:
        self.calls.append(tuple(argv))
        return ProcessResult(self.exit_code, "stdout text", "stderr text")


class FailingRunner:
    def __call__(self, argv: Sequence[str]) -> ProcessResult:
        del argv
        raise OSError("spawn failed")


class FakeTransport:
    """Records get/post calls and replays canned responses by route prefix."""

    def __init__(
        self, responses: Mapping[str, HarnessApiResponse] | None = None
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[
            tuple[str, str, Mapping[str, str], Mapping[str, object] | None]
        ] = []

    def _respond(self, url: str) -> HarnessApiResponse:
        for route, response in self.responses.items():
            if route in url:
                return response
        return HarnessApiResponse(404)

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse:
        self.calls.append(("GET", url, dict(headers), None))
        return self._respond(url)

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HarnessApiResponse:
        self.calls.append(("POST", url, dict(headers), dict(payload)))
        return self._respond(url)


def _expected_payload(
    action: str, config_path: str, environment: str = "paper"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_ref": BUNDLE_REF,
        "environment": environment,
        "action": action,
        "config_path": config_path,
    }


def test_run_backtest_builds_one_request_contract_for_local_and_remote() -> None:
    runner = FakeRunner()
    transport = FakeTransport(
        {
            "/v1/backtests/jobs": HarnessApiResponse(
                202, {"job_id": "momentum-spy-2024-01", "state": "queued"}
            )
        }
    )
    local = ActivationAdapter(LocalHarnessProcess(runner))
    remote = ActivationAdapter(RemoteHarnessApi("https://harness.test", transport))

    local_outcome = local.run_backtest(BUNDLE_REF, BACKTEST_CONFIG)
    remote_outcome = remote.run_backtest(
        BUNDLE_REF, BACKTEST_CONFIG, bearer_token=BEARER
    )

    expected = _expected_payload("backtest", BACKTEST_CONFIG)
    assert local_outcome.request_payload == expected
    assert remote_outcome.request_payload == expected
    assert transport.calls[0][1] == "https://harness.test/v1/backtests/jobs"
    assert transport.calls[0][3] == expected
    assert runner.calls == [("harness", "backtest-job", "--config", BACKTEST_CONFIG)]
    assert remote_outcome.job_id == "momentum-spy-2024-01"
    assert remote_outcome.state == "queued"
    assert local_outcome.exit_code == 0


def test_launch_trader_builds_one_request_contract_for_local_and_remote() -> None:
    runner = FakeRunner()
    transport = FakeTransport(
        {
            "/v1/activation/trader": HarnessApiResponse(
                202, {"job_id": "trader-1", "state": "queued"}
            )
        }
    )
    local = ActivationAdapter(LocalHarnessProcess(runner))
    remote = ActivationAdapter(
        RemoteHarnessApi(
            "https://harness.test",
            transport,
            capabilities=HarnessCapabilities(),
            routes={
                "backtest": "/v1/backtests/jobs",
                "trader": "/v1/activation/trader",
            },
        )
    )

    local_outcome = local.launch_trader(BUNDLE_REF, TRADER_CONFIG)
    remote_outcome = remote.launch_trader(
        BUNDLE_REF, TRADER_CONFIG, bearer_token=BEARER
    )

    expected = _expected_payload("trader", TRADER_CONFIG)
    assert local_outcome.request_payload == expected
    assert remote_outcome.request_payload == expected
    assert runner.calls == [("harness", "trader", "--config", TRADER_CONFIG)]
    assert transport.calls[0][1] == "https://harness.test/v1/activation/trader"
    assert transport.calls[0][3] == expected
    assert remote_outcome.job_id == "trader-1"


def test_capability_gating_raises_before_any_invocation() -> None:
    runner = FakeRunner()
    local = ActivationAdapter(LocalHarnessProcess(runner))

    with pytest.raises(CapabilityUnavailableError) as live_error:
        local.launch_trader(BUNDLE_REF, TRADER_CONFIG, environment="live")
    with pytest.raises(CapabilityUnavailableError) as shadow_error:
        local.launch_trader(BUNDLE_REF, TRADER_CONFIG, environment="shadow")

    assert live_error.value.capability == "live"
    assert shadow_error.value.capability == "shadow"
    assert runner.calls == []
    assert local.capabilities.as_mapping() == {
        "backtest": True,
        "trader": True,
        "shadow": False,
        "live": False,
    }
    with pytest.raises(ValueError, match="unknown harness capability"):
        local.capabilities.require_capability("sweep")


def test_remote_trader_is_unavailable_until_the_harness_publishes_it() -> None:
    transport = FakeTransport()
    remote = ActivationAdapter(RemoteHarnessApi("https://harness.test", transport))

    with pytest.raises(CapabilityUnavailableError) as error:
        remote.launch_trader(BUNDLE_REF, TRADER_CONFIG, bearer_token=BEARER)

    assert error.value.capability == "trader"
    assert transport.calls == []


def test_remote_bearer_is_injected_per_call_and_never_stored() -> None:
    transport = FakeTransport(
        {
            "/v1/backtests/jobs": HarnessApiResponse(
                202, {"job_id": "run-17", "state": "queued"}
            )
        }
    )
    boundary = RemoteHarnessApi("https://harness.test", transport)
    remote = ActivationAdapter(boundary)

    with pytest.raises(HarnessActivationError, match="bearer credential is required"):
        remote.run_backtest(BUNDLE_REF, BACKTEST_CONFIG)
    assert transport.calls == []

    remote.run_backtest(BUNDLE_REF, BACKTEST_CONFIG, bearer_token=f"  {BEARER}  ")

    assert transport.calls[0][2] == {"Authorization": f"Bearer {BEARER}"}
    serialized = repr(boundary) + repr(remote) + str(vars(boundary))
    assert BEARER not in serialized


def test_local_process_outcomes_are_reported_as_data() -> None:
    runner = FakeRunner(exit_code=3)
    local = ActivationAdapter(LocalHarnessProcess(runner))

    outcome = local.launch_trader(BUNDLE_REF, TRADER_CONFIG)

    assert outcome == ActivationOutcome(
        action="trader",
        bundle_ref=BUNDLE_REF,
        environment="paper",
        request_payload=_expected_payload("trader", TRADER_CONFIG),
        exit_code=3,
        stdout="stdout text",
        stderr="stderr text",
    )
    with pytest.raises(HarnessActivationError, match="operator environment"):
        local.launch_trader(BUNDLE_REF, TRADER_CONFIG, bearer_token=BEARER)
    with pytest.raises(HarnessActivationError, match="could not be invoked"):
        ActivationAdapter(LocalHarnessProcess(FailingRunner())).run_backtest(
            BUNDLE_REF, BACKTEST_CONFIG
        )


def test_remote_error_responses_fail_closed() -> None:
    unauthenticated = RemoteHarnessApi(
        "https://harness.test",
        FakeTransport({"/v1/backtests/jobs": HarnessApiResponse(401, {})}),
    )
    unpublished = RemoteHarnessApi(
        "https://harness.test",
        FakeTransport({"/v1/backtests/jobs": HarnessApiResponse(404, {})}),
    )
    malformed = RemoteHarnessApi(
        "https://harness.test",
        FakeTransport({"/v1/backtests/jobs": HarnessApiResponse(202, {"other": 1})}),
    )

    with pytest.raises(
        HarnessActivationError, match="rejected the activation credential"
    ):
        ActivationAdapter(unauthenticated).run_backtest(
            BUNDLE_REF, BACKTEST_CONFIG, bearer_token=BEARER
        )
    with pytest.raises(CapabilityUnavailableError) as error:
        ActivationAdapter(unpublished).run_backtest(
            BUNDLE_REF, BACKTEST_CONFIG, bearer_token=BEARER
        )
    with pytest.raises(HarnessActivationProtocolError, match="job_id"):
        ActivationAdapter(malformed).run_backtest(
            BUNDLE_REF, BACKTEST_CONFIG, bearer_token=BEARER
        )

    assert error.value.capability == "backtest"


def test_activation_requests_reject_undefined_contract_values() -> None:
    with pytest.raises(ValueError, match="bundle_ref"):
        HarnessActivationRequest("", BACKTEST_CONFIG, "backtest")
    with pytest.raises(ValueError, match="action"):
        HarnessActivationRequest(BUNDLE_REF, BACKTEST_CONFIG, "sweep")
    with pytest.raises(ValueError, match="environment"):
        HarnessActivationRequest(BUNDLE_REF, BACKTEST_CONFIG, "trader", "staging")
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteHarnessApi("http://harness.test", FakeTransport())
    with pytest.raises(ValueError, match="base_url"):
        RuntimeStatusClient("  ", FakeTransport())


def test_status_maps_the_published_job_surface() -> None:
    transport = FakeTransport(
        {
            "/v1/backtests/jobs/run-17": HarnessApiResponse(
                200,
                {
                    "job_id": "run-17",
                    "state": "succeeded",
                    "created_at": "2026-09-08T15:04:05Z",
                    "updated_at": "2026-09-08T15:06:09Z",
                    "error_message": None,
                },
            )
        }
    )

    surface = RuntimeStatusClient("https://harness.test", transport).status(
        "run-17", bearer_token=BEARER
    )

    assert surface.state == "available"
    assert surface.value == RuntimeStatus(
        request_ref="run-17",
        state="succeeded",
        created_at="2026-09-08T15:04:05Z",
        updated_at="2026-09-08T15:06:09Z",
        error_message=None,
    )
    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1] == "https://harness.test/v1/backtests/jobs/run-17"
    assert transport.calls[0][2] == {"Authorization": f"Bearer {BEARER}"}


def test_manifests_map_the_published_run_row() -> None:
    transport = FakeTransport(
        {
            "/v1/backtests/runs/run-17": HarnessApiResponse(
                200,
                {
                    "run_id": "run-17",
                    "status": "complete",
                    "strategy_id": "spy-momentum",
                    "data_start": "2024-01-02",
                    "data_end": "2024-01-19",
                    "run_digest": "run-digest",
                    "configuration_digest": "config-digest",
                    "artifact_count": 1,
                    "total_bytes": 512,
                    "metrics": {"total_return": "0.12"},
                },
            )
        }
    )

    surface = RuntimeStatusClient("https://harness.test", transport).manifests(
        "run-17", bearer_token=BEARER
    )

    assert surface.value == ManifestSummary(
        run_id="run-17",
        status="complete",
        strategy_id="spy-momentum",
        data_start="2024-01-02",
        data_end="2024-01-19",
        run_digest="run-digest",
        configuration_digest="config-digest",
        artifact_count=1,
        total_bytes=512,
        metrics={"total_return": "0.12"},
    )


def test_artifacts_map_the_published_artifact_summaries() -> None:
    transport = FakeTransport(
        {
            "/v1/backtests/runs/run-17/artifacts": HarnessApiResponse(
                200,
                {
                    "run_id": "run-17",
                    "artifacts": [
                        {
                            "name": "backtest.json",
                            "size": 512,
                            "object_path": "backtests/run-17/backtest.json",
                            "sas_url": "https://sas.test/read/run-17",
                        },
                        {
                            "name": "events.parquet",
                            "size": 2048,
                            "object_path": "backtests/run-17/events.parquet",
                            "sas_url": None,
                        },
                    ],
                },
            )
        }
    )

    surface = RuntimeStatusClient("https://harness.test", transport).artifacts(
        "run-17", bearer_token=BEARER
    )

    assert surface.value == (
        ArtifactEntry(
            "backtest.json",
            512,
            "backtests/run-17/backtest.json",
            "https://sas.test/read/run-17",
        ),
        ArtifactEntry("events.parquet", 2048, "backtests/run-17/events.parquet", None),
    )


def test_portfolio_summary_maps_inert_books_and_positions() -> None:
    transport = FakeTransport(
        {
            "/v1/runtime/portfolio": HarnessApiResponse(
                200,
                {
                    "cash": "100000.00",
                    "equity": "101250.00",
                    "books": [
                        {
                            "name": "strategy-book",
                            "cash": "95000.00",
                            "equity": "101250.00",
                            "positions": [
                                {
                                    "symbol": "SPY",
                                    "quantity": "100",
                                    "average_price": "600.00",
                                }
                            ],
                        }
                    ],
                },
            )
        }
    )

    surface = RuntimeStatusClient("https://harness.test", transport).portfolio_summary(
        bearer_token=BEARER
    )

    assert surface.value == PortfolioSummary(
        books=(
            BookRecord(
                "strategy-book",
                "95000.00",
                "101250.00",
                (PositionRecord("SPY", "100", "600.00"),),
            ),
        ),
        cash="100000.00",
        equity="101250.00",
    )


def test_risk_decisions_map_inert_records() -> None:
    transport = FakeTransport(
        {
            "/v1/runtime/risk-decisions/run-17": HarnessApiResponse(
                200,
                {
                    "request_ref": "run-17",
                    "decisions": [
                        {
                            "rule": "max_gross_exposure",
                            "decision": "submit",
                            "reason": None,
                        },
                        {
                            "rule": "max_order_size",
                            "decision": "reduce",
                            "reason": "capped",
                        },
                    ],
                },
            )
        }
    )

    surface = RuntimeStatusClient("https://harness.test", transport).risk_decisions(
        "run-17", bearer_token=BEARER
    )

    assert surface.value == (
        RiskDecisionRecord("max_gross_exposure", "submit", None),
        RiskDecisionRecord("max_order_size", "reduce", "capped"),
    )


def test_unavailable_surfaces_never_fabricate_records() -> None:
    transport = FakeTransport({"/v1/runtime/portfolio": HarnessApiResponse(501, {})})
    client = RuntimeStatusClient("https://harness.test", transport)

    surfaces = [
        client.status("run-17"),
        client.manifests("run-17"),
        client.artifacts("run-17"),
        client.portfolio_summary(),
        client.risk_decisions("run-17"),
    ]

    for surface in surfaces[:3] + surfaces[4:]:
        assert surface.state == "unavailable"
        assert surface.value is None
        assert surface.reason == "not_found"
    assert client.portfolio_summary().reason == "not_implemented"
    assert all(call[2] == {} for call in transport.calls)


def test_status_clients_reject_credential_and_protocol_violations() -> None:
    denied = FakeTransport({"/v1/backtests/jobs/run-17": HarnessApiResponse(401, {})})
    faulted = FakeTransport({"/v1/backtests/jobs/run-17": HarnessApiResponse(500, {})})
    malformed = FakeTransport(
        {
            "/v1/backtests/jobs/run-17": HarnessApiResponse(
                200,
                {
                    "job_id": 17,
                    "state": "succeeded",
                    "created_at": "t",
                    "updated_at": "t",
                },
            )
        }
    )
    with pytest.raises(HarnessStatusError, match="rejected the status credential"):
        RuntimeStatusClient("https://harness.test", denied).status("run-17")
    with pytest.raises(HarnessStatusError, match="unexpected harness status"):
        RuntimeStatusClient("https://harness.test", faulted).status("run-17")
    with pytest.raises(HarnessStatusError, match="job_id"):
        RuntimeStatusClient("https://harness.test", malformed).status("run-17")
    with pytest.raises(HarnessStatusError, match="bearer_token"):
        RuntimeStatusClient("https://harness.test", FakeTransport()).status(
            "run-17", bearer_token="   "
        )


def _inert_instances() -> dict[type, object]:
    return {
        RuntimeStatus: RuntimeStatus(
            "run-17", "succeeded", "2026-09-08T15:04:05Z", "2026-09-08T15:06:09Z"
        ),
        ManifestSummary: ManifestSummary("run-17", "complete", "spy-momentum"),
        ArtifactEntry: ArtifactEntry("backtest.json", 512, "backtests/run-17"),
        PositionRecord: PositionRecord("SPY", "100"),
        BookRecord: BookRecord("strategy-book", "95000.00", "101250.00", ()),
        PortfolioSummary: PortfolioSummary((), "100000.00", "101250.00"),
        RiskDecisionRecord: RiskDecisionRecord("max_order_size", "submit"),
        ActivationOutcome: ActivationOutcome(
            "backtest", BUNDLE_REF, "paper", {"schema_version": 1}
        ),
    }


def test_records_are_inert_data_without_harness_behavior() -> None:
    for record_type, instance in _inert_instances().items():
        fields = [field.name for field in dataclasses.fields(record_type)]
        public = [name for name in dir(record_type) if not name.startswith("_")]
        assert sorted(public) == sorted(fields), record_type
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, fields[0], None)

    with pytest.raises(ValueError):
        StatusSurface.available(None)
    with pytest.raises(ValueError):
        StatusSurface(
            state="unavailable",
            value=RuntimeStatus("run-17", "succeeded", "t", "t"),
            reason="gone",
        )
    with pytest.raises(ValueError):
        StatusSurface(state="unavailable")
