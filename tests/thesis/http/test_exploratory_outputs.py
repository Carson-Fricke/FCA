"""Inspect canonical job evidence through the HTTP/MCP application boundary."""

import hashlib
import json
from collections.abc import Mapping

import httpx
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.harness import HarnessDataService
from thesis.integrations.mcp import HarnessMcpTopology, open_harness_mcp_client
from thesis.research.presentation import PresentationService
from thesis.research.presentation.decisions import DecisionRanker

TOKEN = "exploration-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
URL = "/v1/jobs/job-evidence/exploratory-outputs"


class OutputHarness:
    """External MCP boundary serving one immutable output artifact."""

    def __init__(self, document: dict[str, object]) -> None:
        self.content = json.dumps(document)
        self.reads: list[object] = []

    def handle(self, request: object) -> dict[str, object] | None:
        assert isinstance(request, Mapping)
        if "id" not in request:
            return None
        result: dict[str, object] = {}
        if request.get("method") == "initialize":
            result = {"protocolVersion": "2024-11-05"}
        elif request.get("method") == "tools/call":
            params = request["params"]
            assert isinstance(params, Mapping)
            assert params["name"] == "read_artifact"
            self.reads.append(params["arguments"])
            result = {
                "isError": False,
                "content": [],
                "structuredContent": {"content": self.content},
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def client_for(
    document: dict[str, object], presentation: PresentationService | None = None
) -> tuple[TestClient, OutputHarness]:
    harness = OutputHarness(document)
    topology = HarnessMcpTopology(thesis_location="local", harness_location="local")
    service = HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(topology, in_process_handler=harness),
        presentation=presentation,
    )
    return TestClient(
        create_app(bearer_token=TOKEN, harness_data_service=service)
    ), harness


def test_recommendations_rank_full_output_profiles_and_cache_without_requiring_authored_views(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def decide(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        data = next(d for d in payload["state"]["datasets"] if d["name"] == "energy")
        assert data["row_meaning"] == "One outcome for each lookback and horizon."
        support = next(f for f in data["profiles"] if f["name"] == "support")
        assert support["description"] == "Trailing history length in observations."
        assert support["range"] == [2, 4]
        assert support["cardinality"] == 2
        assert sum(bucket["count"] for bucket in support["histogram"]) == 4
        assert "rows" not in data
        return httpx.Response(
            200,
            json={
                "model": payload["model"],
                "answers": {
                    key: {
                        "type": "score",
                        "score": 4 if "Heatmap" in question["instructions"] else 2,
                    }
                    for key, question in payload["questions"].items()
                },
                "usage": {"cost": 0.00001},
            },
        )

    document = evidence()
    programs = document["programs"]
    assert isinstance(programs, dict)
    outputs = programs["study"]
    assert isinstance(outputs, dict)
    outputs["energy"] = {
        "kind": "table",
        "columns": ["support", "horizon", "energy"],
        "rows": [[2, 1, 0.1], [2, 2, 0.2], [4, 1, 0.3], [4, 2, 0.4]],
    }
    outputs["meaning"] = {
        "kind": "dataset_semantics",
        "semantics": {
            "schema_version": 1,
            "dataset": {
                "id": "energy",
                "output": "energy",
                "fields": [
                    {
                        "name": name,
                        "label": name,
                        "description": description,
                        "field_type": "number",
                        "unit": None,
                    }
                    for name, description in [
                        ("support", "Trailing history length in observations."),
                        ("horizon", "Outcome horizon in sessions."),
                        ("energy", "Observed normalized outcome."),
                    ]
                ],
                "identity": None,
                "selection_group": None,
                "event_time": None,
                "available_at": None,
            },
            "row_meaning": "One outcome for each lookback and horizon.",
            "grain": ["support", "horizon"],
            "dimensions": ["support", "horizon"],
            "measures": ["energy"],
            "ordered": ["support", "horizon"],
            "preferred_views": [],
        },
    }
    with httpx.Client(transport=httpx.MockTransport(decide)) as transport:
        client, harness = client_for(
            document, PresentationService(DecisionRanker(client=transport))
        )
        url = "/v1/jobs/job-evidence/research-recommendations"
        assert client.post(url).status_code == 401
        response = client.post(url, headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "ranked"
        energy = next(d for d in result["datasets"] if d["name"] == "energy")
        assert energy["panels"][0]["kind"] == "heatmap"
        assert {p["kind"] for p in energy["panels"]} >= {"heatmap", "scatter"}
        assert energy["dataset"]["output"] == "energy"
        assert (
            result["artifactSha256"]
            == hashlib.sha256(harness.content.encode()).hexdigest()
        )
        assert client.post(url, headers=HEADERS).json() == result
        assert len(calls) == 1


def test_recommendations_check_collisions_beyond_browser_preview_and_preserve_manual_data(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    document = evidence()
    programs = document["programs"]
    assert isinstance(programs, dict)
    outputs = programs["study"]
    assert isinstance(outputs, dict)
    outputs["energy"] = {
        "kind": "table",
        "columns": ["x", "y", "value"],
        "rows": [[x, y, x + y] for x in range(70) for y in range(73)] + [[0, 0, 999]],
    }
    client, _ = client_for(document)
    response = client.post(
        "/v1/jobs/job-evidence/research-recommendations", headers=HEADERS
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "deterministic"
    energy = next(d for d in result["datasets"] if d["name"] == "energy")
    assert energy["total"] == 5111
    assert all(p["kind"] != "heatmap" for p in energy["candidates"])
    assert any(p["kind"] == "scatter" for p in energy["panels"])
    assert (
        next(f for f in energy["profiles"] if f["name"] == "value")["range"][1] == 999
    )
    assert client.get(
        URL,
        headers=HEADERS,
        params={"instance_id": "study", "output_name": "energy", "offset": 5110},
    ).json()["output"]["rows"] == [[0, 0, 999]]


def test_invalid_decision_scores_fall_back_to_valid_plans_without_retry(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def invalid(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"answers": {"invented-plan": {"type": "score", "score": 999}}}
        )

    with httpx.Client(transport=httpx.MockTransport(invalid)) as transport:
        client, _ = client_for(
            evidence(), PresentationService(DecisionRanker(client=transport))
        )
        response = client.post(
            "/v1/jobs/job-evidence/research-recommendations", headers=HEADERS
        )
        assert response.status_code == 200
        assert response.json()["status"] == "deterministic"
        assert "invalid score" in response.json()["note"]
        assert len(calls) == 1


def evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": "job-evidence",
        "programs": {
            "study": {
                "dispersion": {
                    "kind": "series",
                    "points": [
                        {
                            "symbol": "MSFT",
                            "interval": "15min",
                            "event_time_ns": 10,
                            "available_at_ns": 20,
                            "value": None,
                            "undefined_reason": "warm_up",
                        },
                        {
                            "symbol": "MSFT",
                            "interval": "15min",
                            "event_time_ns": 30,
                            "available_at_ns": 40,
                            "value": 1.5,
                            "undefined_reason": None,
                        },
                    ],
                },
                "release": {
                    "kind": "events",
                    "events": [
                        {
                            "symbol": "MSFT",
                            "event_time_ns": 10,
                            "available_at_ns": 30,
                            "fields": {"direction": 1},
                        }
                    ],
                },
                "energy": {
                    "kind": "table",
                    "columns": ["support", "energy"],
                    "rows": [[2, 1.5], [4, None]],
                },
                "fit": {
                    "kind": "fitted_state",
                    "method": "population_normalization",
                    "training_job_id": "job-evidence",
                    "training_start_ns": 0,
                    "training_end_ns": 20,
                    "available_at_ns": 20,
                    "seed": 0,
                    "parameters": {"feature": "range"},
                    "weights": {"mean": 2.0, "scale": 1.0},
                },
            }
        },
    }


def test_exploratory_inspector_pages_causal_evidence_and_preserves_fit_metadata() -> (
    None
):
    client, harness = client_for(evidence())
    catalog = client.get(URL, headers=HEADERS, params={"limit": 2}).json()
    assert catalog["total"] == 4
    assert catalog["nextOffset"] == 2
    assert [row["kind"] for row in catalog["outputs"]] == ["series", "events"]
    assert (
        catalog["artifactSha256"]
        == hashlib.sha256(harness.content.encode()).hexdigest()
    )
    assert (
        client.get(URL, headers=HEADERS, params={"offset": 2, "limit": 2}).json()[
            "nextOffset"
        ]
        is None
    )

    selection = {"instance_id": "study", "output_name": "dispersion", "limit": 1}
    first = client.get(URL, headers=HEADERS, params=selection).json()
    assert first["output"]["points"][0]["undefined_reason"] == "warm_up"
    assert first["output"]["points"][0]["available_at_ns"] == 20
    assert first["nextOffset"] == 1
    second = client.get(URL, headers=HEADERS, params={**selection, "offset": 1}).json()
    assert second["output"]["points"][0]["value"] == 1.5
    assert second["nextOffset"] is None

    for name, key, expected in (
        (
            "release",
            "events",
            [
                {
                    "symbol": "MSFT",
                    "event_time_ns": 10,
                    "available_at_ns": 30,
                    "fields": {"direction": 1},
                }
            ],
        ),
        ("energy", "rows", [[2, 1.5], [4, None]]),
    ):
        response = client.get(
            URL, headers=HEADERS, params={"instance_id": "study", "output_name": name}
        )
        assert response.status_code == 200
        assert response.json()["output"][key] == expected
    fit = client.get(
        URL, headers=HEADERS, params={"instance_id": "study", "output_name": "fit"}
    ).json()
    programs = evidence()["programs"]
    assert isinstance(programs, dict)
    assert fit["output"] == programs["study"]["fit"]
    assert fit["nextOffset"] is None
    assert all(
        read == {"job_id": "job-evidence", "name": "exploratory_outputs.json"}
        for read in harness.reads
    )


def test_exploratory_inspector_rejects_identity_mismatch_and_bounds_records() -> None:
    document = evidence()
    document["job_id"] = "job-other"
    client, _ = client_for(document)
    mismatch = client.get(URL, headers=HEADERS)
    assert mismatch.status_code == 422
    assert "identity" in mismatch.json()["error"]["message"]
    client, _ = client_for(evidence())
    for params in (
        {"offset": -1},
        {"limit": 0},
        {"limit": 501},
        {"output_name": "dispersion"},
    ):
        assert client.get(URL, headers=HEADERS, params=params).status_code == 422
    assert (
        client.get(
            URL,
            headers=HEADERS,
            params={"instance_id": "study", "output_name": "absent"},
        ).status_code
        == 404
    )
    assert client.get(URL).status_code == 401


def test_inspector_byte_budget_advances_by_returned_records() -> None:
    document = evidence()
    programs = document["programs"]
    assert isinstance(programs, dict)
    points = programs["study"]["dispersion"]["points"]
    for point in points:
        point["value"] = None
        point["undefined_reason"] = "unavailable:" + "x" * 20_000
    client, _ = client_for(document)
    params = {"instance_id": "study", "output_name": "dispersion", "limit": 2}
    first = client.get(URL, headers=HEADERS, params=params)
    assert first.status_code == 200
    assert len(first.json()["output"]["points"]) == 1
    assert first.json()["nextOffset"] == 1
    second = client.get(URL, headers=HEADERS, params={**params, "offset": 1})
    assert second.status_code == 200
    assert second.json()["output"]["points"][0]["available_at_ns"] == 40
    assert second.json()["nextOffset"] is None

    programs["study"]["fit"]["weights"] = {
        f"coefficient_{index}": 1.0 for index in range(3000)
    }
    state_client, _ = client_for(document)
    state = state_client.get(
        URL, headers=HEADERS, params={"instance_id": "study", "output_name": "fit"}
    )
    assert state.status_code == 422
    assert "complete artifact export" in state.json()["error"]["message"]


def test_saved_research_view_and_exact_coordinates_cross_http_without_invention() -> (
    None
):
    document = evidence()
    programs = document["programs"]
    assert isinstance(programs, dict)
    study = programs["study"]
    stamp = 1_783_958_220_000_000_001
    study["dispersion"]["points"][0]["event_time_ns"] = stamp
    study["dispersion"]["points"][0]["available_at_ns"] = stamp + 1
    study["research_view"] = {
        "kind": "research_view",
        "view": {"schema_version": 1, "title": "Saved exhibit", "panels": []},
    }
    client, harness = client_for(document)
    catalog = client.get(URL, headers=HEADERS).json()
    assert catalog["outputs"][-1] == {
        "instanceId": "study",
        "name": "research_view",
        "kind": "research_view",
        "count": None,
    }
    view = client.get(
        URL,
        headers=HEADERS,
        params={"instance_id": "study", "output_name": "research_view"},
    ).json()
    assert view["output"] == study["research_view"]
    points = client.get(
        URL,
        headers=HEADERS,
        params={"instance_id": "study", "output_name": "dispersion"},
    ).json()
    assert points["output"]["points"][0]["event_time_ns"] == str(stamp)
    assert points["output"]["points"][0]["available_at_ns"] == str(stamp + 1)
    assert points["output"]["points"][0]["value"] is None
    assert (
        points["artifactSha256"] == hashlib.sha256(harness.content.encode()).hexdigest()
    )


def test_standalone_dataset_semantics_cross_http_unchanged_without_an_exhibit() -> None:
    document = evidence()
    semantics = {
        "schema_version": 1,
        "dataset": {
            "id": "energy",
            "output": "energy",
            "fields": [
                {
                    "name": "support",
                    "label": "Support",
                    "field_type": "number",
                    "description": "Numeric test observation coordinate or measurement.",
                    "unit": "samples",
                },
                {
                    "name": "energy",
                    "label": "Energy",
                    "field_type": "number",
                    "description": "Numeric test observation coordinate or measurement.",
                    "unit": None,
                },
            ],
            "identity": None,
            "selection_group": None,
            "event_time": None,
            "available_at": None,
        },
        "row_meaning": "One energy estimate per support",
        "grain": ["support"],
        "dimensions": ["support"],
        "measures": ["energy"],
        "ordered": ["support"],
        "preferred_views": [],
    }
    programs = document["programs"]
    assert isinstance(programs, dict)
    study = programs["study"]
    assert isinstance(study, dict)
    study["description"] = {
        "kind": "dataset_semantics",
        "semantics": semantics,
    }
    client, _ = client_for(document)
    catalog = client.get(URL, headers=HEADERS).json()
    assert any(row["kind"] == "dataset_semantics" for row in catalog["outputs"])
    response = client.get(
        URL,
        headers=HEADERS,
        params={"instance_id": "study", "output_name": "description"},
    )
    assert response.status_code == 200
    assert response.json()["output"] == {
        "kind": "dataset_semantics",
        "semantics": semantics,
    }
    assert response.json()["nextOffset"] is None
