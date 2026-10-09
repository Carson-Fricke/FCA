"""Inspect canonical job evidence through the HTTP/MCP application boundary."""

import hashlib
import json
from collections.abc import Mapping

from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.harness import HarnessDataService
from thesis.integrations.mcp import HarnessMcpTopology, open_harness_mcp_client

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


def client_for(document: dict[str, object]) -> tuple[TestClient, OutputHarness]:
    harness = OutputHarness(document)
    topology = HarnessMcpTopology(thesis_location="local", harness_location="local")
    service = HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(topology, in_process_handler=harness),
    )
    return TestClient(
        create_app(bearer_token=TOKEN, harness_data_service=service)
    ), harness


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
