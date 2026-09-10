"""HTTP tests for the workspace DATA routes (T231 / TH6.8).

Every route group is exercised through :func:`create_app` with in-memory
local fakes for the stores, a fake harness process runner behind the
:class:`LocalHarnessProcess` adapter, a fake status transport behind the
``RuntimeStatusClient`` seam, and a fake screener market port.  Timestamps
are fixed and no network or real harness process is involved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from thesis.chat import ChatService
from thesis.http.application import create_app
from thesis.http.backtests import (
    BacktestDataService,
    BacktestRecord,
    InMemoryBacktestStore,
)
from thesis.http.dashboards import DashboardDataService
from thesis.http.deployments import DeploymentDataService, InMemoryDeploymentStore
from thesis.http.screeners import InMemoryScreenerStore, ScreenerDataService
from thesis.http.state import InMemoryWorkspaceStateStore, WorkspaceStateService
from thesis.http.workspaces import (
    WorkspaceCatalogStore,
    WorkspaceDataBundle,
    WorkspaceDataService,
    build_local_workspace_data,
)
from thesis.integrations.harness import ProcessResult
from thesis.integrations.harness_activation import (
    HarnessApiResponse,
    LocalHarnessProcess,
)
from thesis.integrations.runtime_status import RuntimeStatusClient
from thesis.screening.engine import MarketBar, UniverseEntry

BEARER_TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
WORKSPACE = "alpha-lab"

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
FIXED_CREATED_AT = "2026-09-09T12:00:00Z"


def clock() -> datetime:
    return NOW


def counting_ids() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"

    return factory


class FakeHarnessRunner:
    """Injected local process runner; records argv, returns success."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> ProcessResult:
        self.argvs.append(tuple(argv))
        return ProcessResult(returncode=0, stdout='{"state": "queued"}', stderr="")


class FakeStatusTransport:
    """Fake ``HarnessApiTransport`` serving canned harness status payloads."""

    def __init__(self, responses: Mapping[str, HarnessApiResponse]) -> None:
        self.responses = dict(responses)
        self.urls: list[str] = []

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse:
        self.urls.append(url)
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        return HarnessApiResponse(status_code=404)

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HarnessApiResponse:
        raise AssertionError("the data routes never post to the status surface")


class ClosedStatusTransport:
    """Fail-closed transport: every harness status surface is unreachable."""

    def get(self, url: str, headers: Mapping[str, str]) -> HarnessApiResponse:
        raise OSError(f"harness status endpoint unreachable: {url}")

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> HarnessApiResponse:
        raise OSError(f"harness status endpoint unreachable: {url}")


PORTFOLIO_BODY: dict[str, object] = {
    "cash": "128940.55",
    "equity": "363187.02",
    "books": [
        {
            "name": "live-main",
            "cash": "32140.12",
            "equity": "182110.44",
            "positions": [
                {"symbol": "QQQ", "quantity": "240", "average_price": "410.88"}
            ],
        }
    ],
}

RISK_BODY: dict[str, object] = {
    "decisions": [
        {"rule": "max-daily-loss", "decision": "allow", "reason": None},
        {
            "rule": "fat-finger-band",
            "decision": "clamp",
            "reason": "order price 1.8% above band mid; clamped to band",
        },
    ]
}

ALPHA_SUMMARY: dict[str, object] = {
    "id": WORKSPACE,
    "name": "Alpha Lab",
    "description": "Personal research workspace",
    "initials": "AL",
    "created_at": FIXED_CREATED_AT,
}

DECLARED_SPLIT: dict[str, object] = {
    "boundary": "2025-01-31",
    "inSample": {"returnPct": 9.8, "sharpe": 1.42, "maxDrawdown": -6.4, "trades": 31},
    "outOfSample": {
        "returnPct": 7.0,
        "sharpe": 1.18,
        "maxDrawdown": -4.9,
        "trades": 17,
    },
}

DEPLOYMENT_CARD: dict[str, object] = {
    "id": "dep-1",
    "workspace_id": WORKSPACE,
    "strategy_id": "str-2",
    "strategy_name": "Mean Reversion EQ",
    "mode": "live",
    "state": "running",
    "health": "healthy",
    "allocated_capital": 50000,
    "unrealized_pnl": 412,
    "realized_pnl": 6210,
    "since": "2026-09-08T15:20:00Z",
    "request_ref": "req-live-221",
}

ORDER_RECORD: dict[str, object] = {
    "id": "ord-9001",
    "workspace_id": WORKSPACE,
    "strategy_id": "str-2",
    "symbol": "QQQ",
    "side": "buy",
    "quantity": "120",
    "price": "412.66",
    "status": "filled",
    "mode": "live",
    "created_at": "2026-09-08T15:42:11Z",
}

BACKTEST_MANIFEST: dict[str, object] = {
    "run_id": "run-20260715-a1",
    "status": "completed",
    "run_digest": "9f2c1d4b7e8a30c5f6d1b2a4e8c7d0f3a5b6c9d2e1f4a7b8c3d6e9f0a1b2c3d4",
    "configuration_digest": (
        "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f809"
    ),
    "data_start": "2022-01-03",
    "data_end": "2025-08-29",
    "artifact_count": 4,
    "total_bytes": 1048576,
}


def backtest_record(
    backtest_id: str = "bt-101", *, split: Mapping[str, object] | None = None
) -> BacktestRecord:
    return BacktestRecord(
        id=backtest_id,
        workspace_id=WORKSPACE,
        strategy_id="str-1",
        name="Momentum Breakout v3.1 baseline",
        period_start="2022-01-03",
        period_end="2025-08-29",
        created_at="2026-07-15T09:45:00Z",
        initial_capital=100000.0,
        final_capital=112100.0,
        total_return=0.121,
        sharpe=1.05,
        max_drawdown=-0.081,
        win_rate=0.54,
        trades=96,
        split=split,
        manifest=BACKTEST_MANIFEST,
        equity=(
            {"t": 1704153600000, "v": 100225.88},
            {"t": 1704499200000, "v": 100034.42},
        ),
        trade_log=(
            {
                "id": "tr-100",
                "symbol": "SPY",
                "side": "buy",
                "qty": 80,
                "entryTime": 1720742400000,
                "entryPrice": 105553.71,
                "exitTime": 1721433600000,
                "exitPrice": 105320.33,
                "pnl": -18670.4,
            },
        ),
    )


class FakeMarketPort:
    """Deterministic screener market ports; no network, no real data."""

    def universe(self, as_of: date) -> list[UniverseEntry]:
        return [UniverseEntry(symbol="SPY", listing_date=date(2010, 1, 1))]

    def market_data(self, symbol: str, window_end: date) -> list[MarketBar]:
        return [
            MarketBar(
                bar_date=date(2026, 9, 7),
                volume=41_230_000,
                close="611.20",
                dollar_volume="25200000000",
            ),
            MarketBar(
                bar_date=date(2026, 9, 8),
                volume=41_230_000,
                close="612.40",
                dollar_volume="24800000000",
            ),
        ]

    def change_pct(self, symbol: str, as_of: date) -> float:
        return 0.42


def make_bundle(
    *,
    harness_runner: FakeHarnessRunner | None = None,
    transport: FakeStatusTransport | None = None,
    deployments: tuple[dict[str, object], ...] = (),
    orders: tuple[dict[str, object], ...] = (),
    market: FakeMarketPort | None = None,
) -> WorkspaceDataBundle:
    """Compose the local services with in-memory stores and injected fakes."""
    harness = LocalHarnessProcess(harness_runner) if harness_runner else None
    status_client = RuntimeStatusClient(
        "http://harness.test",
        transport if transport is not None else ClosedStatusTransport(),
    )
    return WorkspaceDataBundle(
        workspaces=WorkspaceDataService(WorkspaceCatalogStore(), clock=clock),
        backtests=BacktestDataService(
            InMemoryBacktestStore(),
            harness=harness,
            clock=clock,
            id_factory=counting_ids(),
        ),
        deployments=DeploymentDataService(
            InMemoryDeploymentStore(deployments, orders),
            status_client=status_client,
        ),
        dashboards=DashboardDataService(clock=clock, id_factory=counting_ids()),
        screeners=ScreenerDataService(
            store=InMemoryScreenerStore(),
            market=market if market is not None else FakeMarketPort(),
            clock=clock,
            id_factory=counting_ids(),
            strategy_matcher=lambda symbol: {"SPY": "str-1"}.get(symbol),
        ),
        state=WorkspaceStateService(InMemoryWorkspaceStateStore()),
        harness=harness,
    )


def make_client(
    bundle: WorkspaceDataBundle, *, chat_service: object = None
) -> TestClient:
    return TestClient(
        create_app(
            bearer_token=BEARER_TOKEN,
            chat_service=chat_service,
            workspace_data=bundle,
        )
    )


# ---------------------------------------------------------------------------
# Workspaces: catalog + snapshot
# ---------------------------------------------------------------------------


def test_workspaces_list_and_create_match_summary_shape() -> None:
    client = make_client(make_bundle())

    listed = client.get("/v1/workspaces", headers=HEADERS)
    assert listed.status_code == 200
    assert listed.json() == {"items": []}

    created = client.post(
        "/v1/workspaces",
        headers=HEADERS,
        json={"name": "Alpha Lab", "description": "Personal research workspace"},
    )
    assert created.status_code == 201
    summary = created.json()
    assert summary == ALPHA_SUMMARY
    assert client.get("/v1/workspaces", headers=HEADERS).json()["items"] == [summary]


def test_workspace_snapshot_returns_overview_and_real_counts() -> None:
    chat_service = ChatService(clock=lambda: NOW, id_factory=counting_ids())
    bundle = build_local_workspace_data(
        harness_runner=FakeHarnessRunner(),
        clock=clock,
        id_factory=counting_ids(),
        chat_service=chat_service,
        market_port=FakeMarketPort(),
    )
    bundle.workspaces.create_workspace(
        "Alpha Lab", description="Personal research workspace"
    )
    chat = chat_service.create_chat(
        WORKSPACE, title="Momentum breakout tuning", owner_actor_id="user-alex"
    )
    bundle.workspaces.record_activity(
        WORKSPACE,
        title="Backtest completed",
        description="Momentum Breakout v3 finished with +18.4% return",
        activity_type="backtest",
        activity_id="act-1",
    )
    bundle.dashboards.create_widget(
        WORKSPACE,
        title="Alpha Lab equity",
        widget_type="equity",
        pinned=True,
        data={"points": []},
    )

    client = make_client(bundle, chat_service=chat_service)
    response = client.get(f"/v1/workspaces/{WORKSPACE}/snapshot", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    overview = body["overview"]
    assert overview["workspace"] == ALPHA_SUMMARY
    assert overview["health"] == "operational"
    assert overview["activity"] == [
        {
            "id": "act-1",
            "workspace_id": WORKSPACE,
            "title": "Backtest completed",
            "description": "Momentum Breakout v3 finished with +18.4% return",
            "type": "backtest",
            "created_at": FIXED_CREATED_AT,
        }
    ]
    assert [item["chat_id"] for item in overview["openChats"]] == [chat.chat_id]
    assert body["counts"] == {
        "strategies": 0,
        "chats": 1,
        "backtests": 0,
        "deployments": 0,
        "dashboards": 1,
        "screeners": 0,
    }


def test_workspace_snapshot_unknown_workspace_is_404() -> None:
    client = make_client(make_bundle())
    response = client.get("/v1/workspaces/missing/snapshot", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"


# ---------------------------------------------------------------------------
# Backtests: list, detail, launch delegation
# ---------------------------------------------------------------------------


def test_backtest_list_and_detail_match_fixture_fields() -> None:
    bundle = make_bundle()
    bundle.backtests.store.add(backtest_record("bt-101"))
    client = make_client(bundle)

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/backtests", headers=HEADERS)
    assert listed.status_code == 200
    row = listed.json()["items"][0]
    assert row == {
        "id": "bt-101",
        "workspace_id": WORKSPACE,
        "strategy_id": "str-1",
        "name": "Momentum Breakout v3.1 baseline",
        "period_start": "2022-01-03",
        "period_end": "2025-08-29",
        "capital": 100000,
        "total_return": 0.121,
        "sharpe": 1.05,
        "max_drawdown": -0.081,
        "win_rate": 0.54,
        "trades": 96,
        "split_declared": False,
        "created_at": "2026-07-15T09:45:00Z",
    }

    detail = client.get(f"/v1/workspaces/{WORKSPACE}/backtests/bt-101", headers=HEADERS)
    assert detail.status_code == 200
    body = detail.json()
    assert body["initial_capital"] == 100000
    assert body["final_capital"] == 112100
    assert body["net_pnl"] == 12100
    assert body["split"] is None
    assert body["equity"][0] == {"t": 1704153600000, "v": 100225.88}
    assert body["trades"][0]["id"] == "tr-100"
    assert body["manifest"] == BACKTEST_MANIFEST


def test_backtest_detail_declared_split_shape() -> None:
    bundle = make_bundle()
    bundle.backtests.store.add(backtest_record("bt-204", split=DECLARED_SPLIT))
    client = make_client(bundle)

    body = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/bt-204", headers=HEADERS
    ).json()
    assert body["split"] == DECLARED_SPLIT


def test_backtest_detail_unknown_is_404() -> None:
    client = make_client(make_bundle())
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/bt-999", headers=HEADERS
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "backtest_not_found"


def test_backtest_launch_delegates_to_local_harness_adapter() -> None:
    runner = FakeHarnessRunner()
    client = make_client(make_bundle(harness_runner=runner))

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/backtests/launch",
        headers=HEADERS,
        json={"bundle_ref": "bundle-1", "config_path": "runs/bt-204.yaml"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "backtest"
    assert body["bundle_ref"] == "bundle-1"
    assert body["exit_code"] == 0
    assert body["request_payload"]["config_path"] == "runs/bt-204.yaml"
    assert runner.argvs == [("harness", "backtest-job", "--config", "runs/bt-204.yaml")]


def test_backtest_launch_without_adapter_fails_closed() -> None:
    client = make_client(make_bundle(harness_runner=None))

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/backtests/launch",
        headers=HEADERS,
        json={"bundle_ref": "bundle-1", "config_path": "runs/bt-204.yaml"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "harness_unavailable"


# ---------------------------------------------------------------------------
# Deployments: local records + harness status seam
# ---------------------------------------------------------------------------


def test_deployments_and_orders_return_local_records() -> None:
    bundle = make_bundle(deployments=(DEPLOYMENT_CARD,), orders=(ORDER_RECORD,))
    client = make_client(bundle)

    cards = client.get(f"/v1/workspaces/{WORKSPACE}/deployments", headers=HEADERS)
    assert cards.status_code == 200
    assert cards.json()["items"] == [DEPLOYMENT_CARD]

    orders = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/orders", headers=HEADERS
    )
    assert orders.status_code == 200
    assert orders.json()["items"] == [ORDER_RECORD]


def test_portfolio_uses_status_client_seam_when_available() -> None:
    transport = FakeStatusTransport(
        {"/v1/runtime/portfolio": HarnessApiResponse(200, PORTFOLIO_BODY)}
    )
    client = make_client(make_bundle(transport=transport))

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/portfolio", headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"state": "available", "value": PORTFOLIO_BODY}
    assert transport.urls == ["http://harness.test/v1/runtime/portfolio"]


def test_risk_decisions_use_status_client_seam_when_available() -> None:
    transport = FakeStatusTransport(
        {"risk-decisions": HarnessApiResponse(200, RISK_BODY)}
    )
    client = make_client(make_bundle(transport=transport))

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/risk-decisions",
        headers=HEADERS,
        params={"request_ref": "req-live-221"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "available"
    assert body["value"] == [
        {"rule": "max-daily-loss", "decision": "allow", "reason": None},
        {
            "rule": "fat-finger-band",
            "decision": "clamp",
            "reason": "order price 1.8% above band mid; clamped to band",
        },
    ]


def test_unconfigured_surfaces_fail_closed_with_503() -> None:
    client = make_client(make_bundle(transport=None))

    portfolio = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/portfolio", headers=HEADERS
    )
    assert portfolio.status_code == 503
    error = portfolio.json()["error"]
    assert error["code"] == "harness_status_unavailable"
    assert error["message"]

    risk = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/risk-decisions",
        headers=HEADERS,
        params={"request_ref": "req-live-221"},
    )
    assert risk.status_code == 503
    assert risk.json()["error"]["code"] == "harness_status_unavailable"


def test_harness_unpublished_surface_returns_503_not_fabricated() -> None:
    transport = FakeStatusTransport({"/v1/runtime/portfolio": HarnessApiResponse(503)})
    client = make_client(make_bundle(transport=transport))

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/deployments/portfolio", headers=HEADERS
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "surface_unavailable",
        "message": "unavailable",
    }


# ---------------------------------------------------------------------------
# Dashboards: widget CRUD
# ---------------------------------------------------------------------------


def test_widget_crud_round_trip() -> None:
    client = make_client(make_bundle())
    base = f"/v1/workspaces/{WORKSPACE}/dashboards/widgets"

    created = client.post(
        base,
        headers=HEADERS,
        json={
            "title": "Alpha Lab equity",
            "type": "equity",
            "pinned": True,
            "data": {"points": [{"t": 1767225600000, "v": 100000}]},
        },
    )
    assert created.status_code == 201
    widget = created.json()
    assert widget["id"] == "wgt-001"
    assert widget["workspace_id"] == WORKSPACE
    assert widget["created_at"] == FIXED_CREATED_AT
    assert widget["data"] == {"points": [{"t": 1767225600000, "v": 100000}]}

    listed = client.get(base, headers=HEADERS)
    assert listed.status_code == 200
    assert listed.json()["items"] == [widget]

    deleted = client.delete(f"{base}/wgt-001", headers=HEADERS)
    assert deleted.status_code == 204

    missing = client.delete(f"{base}/wgt-001", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "widget_not_found"


# ---------------------------------------------------------------------------
# Screeners: engine delegation + study persistence
# ---------------------------------------------------------------------------


def test_screener_run_delegates_to_engine_and_persists_study() -> None:
    bundle = make_bundle(market=FakeMarketPort())
    client = make_client(bundle)

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={
            "name": "Liquid large caps",
            "filters": [{"kind": "volume", "op": ">", "value": 1000}],
            "universe_revision": "u-2026-09-09",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["as_of"] == "2026-09-09"
    assert body["items"] == [
        {
            "symbol": "SPY",
            "rank": 1,
            "last": 612.4,
            "volume": 41230000.0,
            "avg_dollar_volume_20d": 25000000000.0,
            "change_pct": 0.42,
            "matched_strategy_id": "str-1",
        }
    ]

    artifact_path = body["artifact_path"]
    assert artifact_path.startswith("screeners/studies/liquid-large-caps-")
    stored = bundle.screeners.store.load_artifact(artifact_path)
    assert stored is not None
    assert b'"definition_name":"Liquid large caps"' in stored

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/screeners", headers=HEADERS)
    assert listed.status_code == 200
    definition = listed.json()["items"][0]
    assert definition["name"] == "Liquid large caps"
    assert definition["filters"] == [{"kind": "volume", "op": ">", "value": 1000}]
    assert definition["artifact_path"] == artifact_path
    assert definition["created_at"] == FIXED_CREATED_AT


def test_screener_run_with_unknown_filter_kind_is_422() -> None:
    client = make_client(make_bundle())
    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={
            "name": "Broken",
            "filters": [{"kind": "momentum", "op": ">", "value": 1}],
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Workspace state: CLEO projection + evidence + consent audit
# ---------------------------------------------------------------------------


def test_workspace_state_round_trip_and_404() -> None:
    client = make_client(make_bundle())
    document = {
        "cleo": {
            "plan_id": "plan-momentum-34",
            "title": "Tune Momentum Breakout",
            "current_stage": "validate",
            "stages": [{"name": "validate", "state": "active"}],
            "steps": [
                {
                    "id": "step-1",
                    "title": "Declared split",
                    "state": "done",
                    "gate": None,
                }
            ],
            "updated_at": "2026-09-08T18:10:00Z",
        },
        "taskTimeline": [],
        "lineages": [
            {
                "strategy_id": "str-1",
                "strategy_name": "Momentum Breakout",
                "nodes": [],
                "edges": [],
            }
        ],
        "evidence": [
            {
                "id": "ev-1",
                "kind": "backtest",
                "title": "bt-204 declared split",
                "digest": "c4d5e6f7",
                "reference": "runs/run-20260908-b4",
                "revision": None,
                "trust_tier": "workspace",
                "link": "/w/alpha-lab/backtests/bt-204",
            }
        ],
        "consentAudit": [
            {
                "grantId": "grant-11",
                "kind": "proposal",
                "at": FIXED_CREATED_AT,
                "actor": "thesis-agent",
                "bundleHash": "9f2c1d4b",
                "environment": "dev",
                "action": "paper",
                "summary": "Proposal: paper activation",
            }
        ],
    }

    saved = client.put(
        f"/v1/workspaces/{WORKSPACE}/state", headers=HEADERS, json=document
    )
    assert saved.status_code == 200
    fetched = client.get(f"/v1/workspaces/{WORKSPACE}/state", headers=HEADERS)
    assert fetched.status_code == 200
    assert fetched.json() == document

    missing = client.get("/v1/workspaces/beta-quant/state", headers=HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "workspace_state_not_found"


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/workspaces"),
        ("get", f"/v1/workspaces/{WORKSPACE}/snapshot"),
        ("get", f"/v1/workspaces/{WORKSPACE}/backtests"),
        ("post", f"/v1/workspaces/{WORKSPACE}/backtests/launch"),
        ("get", f"/v1/workspaces/{WORKSPACE}/deployments/portfolio"),
        ("get", f"/v1/workspaces/{WORKSPACE}/dashboards/widgets"),
        ("get", f"/v1/workspaces/{WORKSPACE}/screeners"),
        ("get", f"/v1/workspaces/{WORKSPACE}/state"),
    ],
)
def test_data_routes_require_bearer_token(method: str, path: str) -> None:
    client = make_client(
        make_bundle(deployments=(DEPLOYMENT_CARD,), orders=(ORDER_RECORD,))
    )
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
