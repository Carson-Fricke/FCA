"""Focused job-keyed artifact reader tests."""

from __future__ import annotations

from collections.abc import Mapping

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.artifacts import HarnessArtifactReader

JOB_ID = "job-0123456789abcdef"


class FakeProvider(HarnessToolProvider):
    """MCP artifact responses keyed by tool name."""

    def __init__(self, responses: Mapping[str, HarnessToolResult]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        self.calls.append((name, dict(arguments or {})))
        return self._responses[name]


def test_reader_uses_job_id_for_mcp_artifact_calls() -> None:
    provider = FakeProvider(
        {
            "read_artifact": HarnessToolResult(
                tool="read_artifact",
                ok=True,
                structured={"content": "hello", "encoding": "utf-8"},
            ),
            "list_artifacts": HarnessToolResult(
                tool="list_artifacts",
                ok=True,
                structured={"job_id": JOB_ID, "artifacts": []},
            ),
        }
    )
    reader = HarnessArtifactReader(provider)

    assert reader.read(JOB_ID, "metrics.json") == b"hello"
    assert provider.calls == [
        ("read_artifact", {"job_id": JOB_ID, "name": "metrics.json"})
    ]


def test_reader_falls_back_to_published_sas_url() -> None:
    provider = FakeProvider(
        {
            "read_artifact": HarnessToolResult(
                tool="read_artifact", ok=True, structured={}
            ),
            "list_artifacts": HarnessToolResult(
                tool="list_artifacts",
                ok=True,
                structured={
                    "job_id": JOB_ID,
                    "artifacts": [
                        {
                            "name": "metrics.json",
                            "uri": "jobs/job-x/artifacts/metrics.json",
                            "sas_url": "https://objects.example/metrics.json",
                        }
                    ],
                },
            ),
        }
    )
    reader = HarnessArtifactReader(provider, http_get=lambda url: b"remote-bytes")

    assert reader.read(JOB_ID, "metrics.json") == b"remote-bytes"
    assert provider.calls[-1] == ("list_artifacts", {"job_id": JOB_ID})
