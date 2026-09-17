from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from lake.app import create_app
from lake.catalog import (
    ContractRecord,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryContractRepository,
    InMemoryJobRepository,
    JobRecord,
    JobState,
    OptionType,
)
from lake.contracts import AssetClass, DataType
from lake.http.models import EnsureRequest
from lake.http.routes.ensure import EnsureJob, EnsureJobLauncher, ensure_job_id

TOKEN = "test-secret"
AS_OF = datetime(2024, 1, 22, 12, tzinfo=UTC)


def _request(
    *,
    start_date: str = "2024-01-18",
    end_date: str = "2024-01-19",
    symbol: str = "AAPL",
    asset_class: str = "equity",
    data_type: str = "trade",
) -> dict[str, str]:
    return {
        "asset_class": asset_class,
        "data_type": data_type,
        "symbol": symbol,
        "start_date": start_date,
        "end_date": end_date,
    }


def _client(
    catalog: InMemoryCatalogRepository,
    jobs: InMemoryJobRepository | None = None,
    *,
    launcher: EnsureJobLauncher | None = None,
    max_range_days: int = 366,
    wait_deadline_seconds: int | float = 1,
) -> TestClient:
    return TestClient(
        create_app(
            bearer_token=TOKEN,
            catalog_repository=catalog,
            job_repository=jobs,
            job_launcher=launcher,
            catalog_max_range_days=max_range_days,
            ensure_wait_deadline_seconds=wait_deadline_seconds,
            coverage_clock=lambda: AS_OF,
        )
    )


def _post(client: TestClient, body: Mapping[str, object]):
    return client.post(
        "/v1/catalog/ensure",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def _get(client: TestClient, job_id: str):
    return client.get(
        f"/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_satisfied_ensure_returns_coverage_without_launching() -> None:
    catalog = InMemoryCatalogRepository(
        [
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.COMPLETE,
            ),
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="AAPL",
                session_date=date(2024, 1, 19),
                status=CoverageStatus.COMPLETE,
            ),
        ]
    )
    launched: list[EnsureJob] = []
    client = _client(catalog, launcher=launched.append)

    response = _post(client, _request())

    assert response.status_code == 200
    assert [entry["status"] for entry in response.json()["entries"]] == [
        "complete",
        "complete",
    ]
    assert launched == []


def test_missing_ensure_returns_synchronous_completion_when_launcher_fills_catalog() -> (
    None
):
    catalog = InMemoryCatalogRepository()
    launched: list[EnsureJob] = []

    def launcher(job: EnsureJob) -> None:
        launched.append(job)
        for gap in job.gaps:
            catalog.upsert(
                CoverageRecord(
                    asset_class=job.request.asset_class,
                    data_type=job.request.data_type,
                    symbol=job.request.symbol,
                    session_date=gap.session_date,
                    status=CoverageStatus.COMPLETE,
                )
            )

    response = _post(
        _client(catalog, launcher=launcher),
        _request(start_date="2024-01-18", end_date="2024-01-18"),
    )

    assert response.status_code == 200
    assert [entry["status"] for entry in response.json()["entries"]] == ["complete"]
    assert len(launched) == 1
    assert response.headers.get("location") is None


def test_option_ensure_uses_selected_osi_contracts_and_stable_authenticated_job() -> (
    None
):
    symbol = "SPY240119C00500000"
    contract = ContractRecord(
        symbol=symbol,
        underlying="SPY",
        expiration_date=date(2024, 1, 19),
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
    )
    catalog = InMemoryCatalogRepository()
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []
    client = _client(catalog, jobs, launcher=launched.append)
    client.app.state.contract_repository = InMemoryContractRepository([contract])
    body = {
        "asset_class": "option",
        "data_type": "trade",
        "contract_symbols": [symbol],
        "start_date": "2024-01-19",
        "end_date": "2024-01-19",
    }

    first = _post(client, body)
    second = _post(client, body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    # The first option ensure completed without satisfying coverage, so the
    # second supersedes it and relaunches the launcher.
    assert len(launched) == 2
    assert launched[0].request.symbol == symbol
    assert [gap.symbol for gap in launched[0].gaps] == [symbol]
    assert launched[0].job_id == first.json()["job_id"] + f"-{symbol}"


def test_missing_ensure_persists_stable_job_invokes_launcher_and_polls() -> None:
    catalog = InMemoryCatalogRepository()
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []

    def launcher(job: EnsureJob) -> None:
        launched.append(job)

    client = _client(catalog, jobs, launcher=launcher)
    body = _request()

    first = _post(client, body)
    second = _post(client, body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    # The launcher finished but did not satisfy coverage, so the record is
    # terminal and the next ensure supersedes it to repair the period.
    assert first.json()["state"] == "complete"
    assert first.headers["location"] == f"/v1/jobs/{first.json()['job_id']}"
    assert len(launched) == 2
    assert launched[0].job_id == ensure_job_id(EnsureRequest.model_validate(body))
    assert len(launched[0].gaps) == 2

    polled = _get(client, first.json()["job_id"])
    assert polled.status_code == 200
    assert polled.json() == {
        "job_id": first.json()["job_id"],
        "state": "complete",
        "created_at": "2024-01-22T12:00:00Z",
        "updated_at": "2024-01-22T12:00:00Z",
        "error_message": None,
    }


def test_crypto_ensure_uses_utc_days_and_the_equity_job_contract() -> None:
    catalog = InMemoryCatalogRepository()
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []
    client = _client(catalog, jobs, launcher=launched.append)
    body = _request(
        asset_class="crypto",
        data_type="quote",
        symbol="BTC/USD",
        start_date="2024-01-19",
        end_date="2024-01-22",
    )

    unauthenticated = client.post("/v1/catalog/ensure", json=body)
    first = _post(client, body)
    second = _post(client, body)

    assert unauthenticated.status_code == 401
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["state"] == "complete"
    assert first.headers["location"] == f"/v1/jobs/{first.json()['job_id']}"
    assert len(launched) == 2
    assert launched[0].request.asset_class is AssetClass.CRYPTO
    assert launched[0].request.symbol == "BTC/USD"
    assert [gap.session_date.isoformat() for gap in launched[0].gaps] == [
        "2024-01-19",
        "2024-01-20",
        "2024-01-21",
        "2024-01-22",
    ]
    assert [gap.state for gap in launched[0].gaps] == [
        "missing",
        "missing",
        "missing",
        "partial",
    ]
    assert _get(client, first.json()["job_id"]).json()["state"] == "complete"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (
            {
                **_request(),
                "asset_class": "option",
            },
            "unsupported_asset_class",
        ),
        ({**_request(), "unexpected": "value"}, "validation_error"),
    ],
)
def test_ensure_rejects_unsupported_requests(
    body: dict[str, str], expected_code: str
) -> None:
    response = _post(_client(InMemoryCatalogRepository()), body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


def test_ensure_accepts_a_range_beyond_the_catalog_cap() -> None:
    catalog = InMemoryCatalogRepository()
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []
    client = _client(catalog, jobs, launcher=launched.append, max_range_days=366)

    response = _post(
        client,
        _request(start_date="2022-01-03", end_date="2024-01-19"),
    )

    assert response.status_code == 202
    assert response.json()["state"] == "complete"
    assert len(launched) == 1
    assert launched[0].request.start_date == date(2022, 1, 3)
    assert launched[0].request.end_date == date(2024, 1, 19)
    # The full multi-year window is planned in one job despite the configured
    # catalog cap: every XNYS session in the range is a gap.
    assert len(launched[0].gaps) > 366
    assert launched[0].gaps[0].session_date == date(2022, 1, 3)
    assert launched[0].gaps[-1].session_date == date(2024, 1, 19)


def test_ensure_and_poll_require_authentication() -> None:
    client = _client(InMemoryCatalogRepository())

    ensure_response = client.post("/v1/catalog/ensure", json=_request())
    poll_response = client.get("/v1/jobs/unknown")

    assert ensure_response.status_code == 401
    assert poll_response.status_code == 401


def test_terminal_job_with_unsatisfied_coverage_is_superseded_and_relaunched() -> None:
    catalog = InMemoryCatalogRepository(
        [
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="AAPL",
                session_date=date(2024, 1, 18),
                status=CoverageStatus.ERROR,
            ),
        ]
    )
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []
    spec = EnsureRequest.model_validate(
        _request(start_date="2024-01-18", end_date="2024-01-18")
    )
    jobs.upsert(
        JobRecord(
            job_id=ensure_job_id(spec),
            state=JobState.SUCCEEDED,
            created_at=AS_OF,
            updated_at=AS_OF,
            error_message="stale attempt",
        )
    )
    client = _client(catalog, jobs, launcher=launched.append)

    response = _post(client, _request(start_date="2024-01-18", end_date="2024-01-18"))

    assert response.status_code == 202
    assert response.json()["job_id"] == ensure_job_id(spec)
    assert response.json()["state"] == "complete"
    assert len(launched) == 1
    assert [gap.state for gap in launched[0].gaps] == ["error"]
    polled = _get(client, ensure_job_id(spec))
    assert polled.json()["state"] == "complete"
    assert polled.json()["error_message"] is None


def test_in_flight_job_record_short_circuits_without_relaunching() -> None:
    catalog = InMemoryCatalogRepository()
    jobs = InMemoryJobRepository()
    launched: list[EnsureJob] = []
    spec = EnsureRequest.model_validate(_request())
    jobs.upsert(
        JobRecord(
            job_id=ensure_job_id(spec),
            state=JobState.RUNNING,
            created_at=AS_OF,
            updated_at=AS_OF,
        )
    )
    client = _client(catalog, jobs, launcher=launched.append)

    first = _post(client, _request())
    second = _post(client, _request())

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["state"] == "running"
    assert launched == []


def test_launcher_failure_is_persisted_as_failed_job() -> None:
    def launcher(job: EnsureJob) -> None:
        raise RuntimeError(f"launcher failed for {job.job_id}")

    client = _client(InMemoryCatalogRepository(), launcher=launcher)

    response = _post(client, _request())

    assert response.status_code == 202
    assert response.json()["state"] == "failed"
    polled = _get(client, response.json()["job_id"])
    assert polled.json()["state"] == "failed"
    assert "launcher failed for ensure-" in polled.json()["error_message"]


def test_ensure_returns_after_the_configured_wait_deadline() -> None:
    started = Event()
    release = Event()

    def launcher(job: EnsureJob) -> None:
        del job
        started.set()
        release.wait()

    client = _client(
        InMemoryCatalogRepository(),
        launcher=launcher,
        wait_deadline_seconds=0.01,
    )

    request_started = monotonic()
    response = _post(client, _request())
    assert started.wait(0.1)
    release.set()

    assert response.status_code == 202
    assert monotonic() - request_started < 0.5


def test_poll_returns_not_found_with_consistent_error_shape() -> None:
    response = _get(_client(InMemoryCatalogRepository()), "missing-job")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "job_not_found",
            "message": "the requested job does not exist",
        }
    }


def test_poll_exposes_injected_terminal_job_state() -> None:
    job = JobRecord(
        job_id="ensure-existing",
        state=JobState.COMPLETE,
        created_at=AS_OF,
        updated_at=AS_OF,
    )
    client = _client(InMemoryCatalogRepository(), InMemoryJobRepository([job]))

    response = _get(client, job.job_id)

    assert response.status_code == 200
    assert response.json()["state"] == "complete"
