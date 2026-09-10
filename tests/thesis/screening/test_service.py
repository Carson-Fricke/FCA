"""Screener service and HTTP route tests against the committed T224 service
facade: auth, response shapes, provenance, idempotency, and error mapping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.screening import (
    MarketBar,
    UniverseEntry,
)
from thesis.screening.service import (
    ScreenerDataUnavailableError,
    ScreenerService,
    ScreenerServiceError,
    UnknownScreenerError,
)

AS_OF = date(2026, 9, 9)
BEARER_TOKEN = "thesis-test-token"


class StubContext:
    """Minimal extended-protocol context over canned data."""

    def universe(self, as_of: date) -> tuple[UniverseEntry, ...]:
        return tuple(
            UniverseEntry(symbol=symbol, listing_date=date(2025, 1, 2))
            for symbol in ("AAA", "BBB")
        )

    def market_data(self, symbol: str, window_end: date) -> tuple[MarketBar, ...]:
        return tuple(
            MarketBar(
                bar_date=date(2026, 9, 1 + offset),
                volume=1_000_000,
                close="12.00",
                dollar_volume="12000000.00",
            )
            for offset in range(9)
        )

    def classification(self, symbol: str, as_of: date) -> dict[str, str]:
        return {"security_type": "stock", "exchange": "NASDAQ"}

    def indicator_value(
        self,
        name: str,
        params: dict[str, object],
        symbol: str,
        *,
        window_start: date,
        window_end: date,
    ) -> str:
        return "11.00"


class MemoryStore:
    """In-memory ScreenerStore with fail-closed reads, like the adapters."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], dict[str, Any]] = {}

    def put(self, workspace_id: str, key: str, *, document: Mapping[str, Any]) -> None:
        self._docs[(workspace_id, key)] = dict(document)

    def get(self, workspace_id: str, key: str) -> Mapping[str, Any]:
        document = self._docs.get((workspace_id, key))
        if document is None:
            raise UnknownScreenerError(f"unknown screener document {key!r}")
        return dict(document)

    def list_prefix(
        self, workspace_id: str, prefix: str
    ) -> Sequence[Mapping[str, Any]]:
        return tuple(
            dict(document)
            for (workspace, key), document in sorted(self._docs.items())
            if workspace == workspace_id and key.startswith(prefix)
        )

    def head_revision(self, workspace_id: str) -> str:
        del workspace_id
        return "test-revision"


def _service(
    data_context: Any = None, *, store: MemoryStore | None = None
) -> ScreenerService:
    return ScreenerService(
        data_context=(lambda workspace_id, as_of: StubContext())
        if data_context is None
        else data_context,
        store=store if store is not None else MemoryStore(),
        clock=lambda: "2026-09-09T12:00:00Z",
    )


def _client(service: ScreenerService) -> TestClient:
    return TestClient(create_app(bearer_token=BEARER_TOKEN, screener_service=service))


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {BEARER_TOKEN}"}


def _run_body(**overrides: object) -> dict[str, object]:
    return {
        "name": "liquid-stocks",
        "as_of": AS_OF.isoformat(),
        "lookback_days": 20,
        "filters": [{"kind": "avg_dollar_volume_min", "params": {"min": 1000000}}],
        **overrides,
    }


def test_run_returns_provenance_and_deterministic_ordering() -> None:
    client = _client(_service())

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == "workspace-a"
    assert body["result_id"].startswith("scr-")
    assert body["definition_hash"]
    assert body["git_revision"] == "test-revision"
    result = body["result"]
    assert result["definition_name"] == "liquid-stocks"
    assert result["as_of"] == AS_OF.isoformat()
    assert result["data_window_start"] == "2026-08-21"
    assert result["data_window_end"] == AS_OF.isoformat()
    assert [hit["rank"] for hit in result["hits"]] == [1, 2]
    assert [hit["symbol"] for hit in result["hits"]] == ["AAA", "BBB"]
    assert all("avg_dollar_volume" in hit["metrics"] for hit in result["hits"])

    again = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    ).json()
    assert again["result_id"] == body["result_id"]


def test_run_is_idempotent_and_results_are_fetchable_by_id() -> None:
    client = _client(_service())

    body = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    ).json()
    fetched = client.get(
        f"/v1/workspaces/workspace-a/screening/results/{body['result_id']}",
        headers=_headers(),
    )

    assert fetched.status_code == 200
    assert fetched.json()["definition_hash"] == body["definition_hash"]
    assert fetched.json()["result"]["hits"] == body["result"]["hits"]


def test_run_defaults_the_universe_revision_to_the_as_of_date() -> None:
    client = _client(_service())

    body = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    ).json()

    assert body["definition"]["universe_revision"] == f"asof-{AS_OF.isoformat()}"


def test_saved_definitions_round_trip_with_definition_hash() -> None:
    client = _client(_service())

    saved = client.post(
        "/v1/workspaces/workspace-a/screening",
        headers=_headers(),
        json={
            "name": "saved-one",
            "lookback_days": 20,
            "filters": [{"kind": "min_price", "params": {"min": 1.0}}],
        },
    )

    assert saved.status_code == 200
    document = saved.json()
    assert document["definition_id"] == "saved-one"

    listed = client.get(
        "/v1/workspaces/workspace-a/screening", headers=_headers()
    ).json()
    assert [item["definition_id"] for item in listed["items"]] == ["saved-one"]

    fetched = client.get(
        "/v1/workspaces/workspace-a/screening/saved-one", headers=_headers()
    )
    assert fetched.status_code == 200
    assert fetched.json()["definition_id"] == "saved-one"


def test_unknown_definition_and_result_ids_return_404() -> None:
    client = _client(_service())

    assert (
        client.get(
            "/v1/workspaces/workspace-a/screening/does-not-exist", headers=_headers()
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/workspaces/workspace-a/screening/results/does-not-exist",
            headers=_headers(),
        ).status_code
        == 404
    )


def test_invalid_filter_kind_returns_422() -> None:
    client = _client(_service())

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run",
        headers=_headers(),
        json=_run_body(filters=[{"kind": "moon_phase", "params": {}}]),
    )

    assert response.status_code == 422


def test_invalid_payload_shape_returns_422() -> None:
    client = _client(_service())

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run",
        headers=_headers(),
        json={"name": "x", "as_of": "2026-09-09", "filters": [], "extra": True},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_screening_routes_require_the_bearer_token() -> None:
    client = _client(_service())

    assert (
        client.post(
            "/v1/workspaces/workspace-a/screening/run", json=_run_body()
        ).status_code
        == 401
    )
    assert client.get("/v1/workspaces/workspace-a/screening").status_code == 401
    assert client.get("/v1/workspaces/workspace-a/screening/xyz").status_code == 401
    rejected = client.post(
        "/v1/workspaces/workspace-a/screening/run",
        headers={"Authorization": "Bearer wrong"},
        json=_run_body(),
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "unauthorized"


def test_service_without_data_ports_answers_503() -> None:
    service = ScreenerService(
        data_context=None, store=MemoryStore(), clock=lambda: "2026-09-09T00:00:00Z"
    )
    client = _client(service)

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "screener_unavailable"


def test_app_without_a_screener_service_answers_503() -> None:
    client = TestClient(create_app(bearer_token=BEARER_TOKEN))

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "screener_unavailable"


def test_run_wraps_port_failures_as_422() -> None:
    class Broken:
        """Protocol-conformant context whose universe port fails closed."""

        def universe(self, as_of: date) -> tuple[UniverseEntry, ...]:
            raise OSError("backend down")

        def market_data(self, symbol: str, window_end: date) -> tuple[MarketBar, ...]:
            raise AssertionError("universe must fail before market_data is reached")

    service = ScreenerService(
        data_context=lambda workspace_id, as_of: Broken(),
        store=MemoryStore(),
        clock=lambda: "2026-09-09T00:00:00Z",
    )
    client = _client(service)

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run", headers=_headers(), json=_run_body()
    )

    assert response.status_code == 422


def test_unknown_result_error_maps_to_404_at_service_level() -> None:
    service = _service()

    with pytest.raises(UnknownScreenerError):
        service.get_result("ws", "missing")


def test_definitions_are_ordered_deterministically() -> None:
    service = _service()
    for name in ("zulu", "alpha", "mike"):
        service.save_definition(
            "ws",
            {
                "name": name,
                "filters": [{"kind": "min_price", "params": {"min": 1.0}}],
            },
        )

    ids = [document["definition_id"] for document in service.list_definitions("ws")]

    assert ids == sorted(ids)


def test_indicator_filters_run_through_the_api() -> None:
    client = _client(_service())

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run",
        headers=_headers(),
        json=_run_body(filters=[{"kind": "price_above_sma", "params": {"period": 5}}]),
    )

    assert response.status_code == 200
    hits = response.json()["result"]["hits"]
    assert all("ind:sma(period=5)" in hit["metrics"] for hit in hits)


def test_run_rejects_an_empty_definition_name() -> None:
    client = _client(_service())

    response = client.post(
        "/v1/workspaces/workspace-a/screening/run",
        headers=_headers(),
        json=_run_body(name=""),
    )

    assert response.status_code == 422


def test_error_hierarchy_is_stable() -> None:
    assert issubclass(ScreenerDataUnavailableError, ScreenerServiceError)
    assert issubclass(UnknownScreenerError, ScreenerServiceError)
