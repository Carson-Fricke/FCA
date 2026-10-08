"""Screening launch allocates one execution job_id before harness submission."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import pytest

from thesis.integrations.mcp import HarnessMcpTransportError, McpToolResult
from thesis.screening.definitions import ScreenerDefinition
from thesis.screening.runs import ScreeningArtifactError, ScreeningRunError
from thesis.screening.service import ScreenerService, ScreenerUnavailableError

JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{32}$")
AS_OF = "2026-01-02"


class FakeScreeningTools:
    """ScreeningToolProvider double recording every tool call."""

    def __init__(
        self,
        definition: ScreenerDefinition,
        *,
        acknowledged_job_id: str | None = None,
        omit_job_id: bool = False,
        schema: str = "fca-harness-screen-v2",
        artifact_status: str = "succeeded",
    ) -> None:
        self._definition = definition
        self._acknowledged_job_id = acknowledged_job_id
        self._omit_job_id = omit_job_id
        self._schema = schema
        self._artifact_status = artifact_status
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run_screener(
        self,
        *,
        config: Mapping[str, object],
        workspace: str | None = None,
        job_id: str | None = None,
    ) -> McpToolResult:
        self.calls.append(
            (
                "run_screener",
                {
                    "config": dict(config),
                    "workspace": workspace,
                    "job_id": job_id,
                },
            )
        )
        structured: dict[str, object] = {"state": "queued"}
        acknowledged = self._acknowledged_job_id or job_id
        if not self._omit_job_id and acknowledged is not None:
            structured["job_id"] = acknowledged
        return McpToolResult(tool="run_screener", is_error=False, structured=structured)

    def get_job_status(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_job_status", {"job_id": job_id}))
        return McpToolResult(
            tool="get_job_status",
            is_error=False,
            structured={"job_id": job_id, "state": "succeeded"},
        )

    def get_job_result(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_job_result", {"job_id": job_id}))
        return McpToolResult(
            tool="get_job_result",
            is_error=False,
            structured={"job_id": job_id, "status": "succeeded"},
        )

    def list_artifacts(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("list_artifacts", {"job_id": job_id}))
        return McpToolResult(
            tool="list_artifacts",
            is_error=False,
            structured={
                "job_id": job_id,
                "artifacts": [
                    {
                        "name": "screen.json",
                        "uri": f"jobs/{job_id}/screen.json",
                        "sha256": "a" * 64,
                    }
                ],
            },
        )

    def read_artifact(self, *, job_id: str, name: str) -> McpToolResult:
        self.calls.append(("read_artifact", {"job_id": job_id, "name": name}))
        document = {
            "schema": self._schema,
            "as_of": AS_OF,
            "status": self._artifact_status,
            "definition_id": self._definition.definition_id,
            "definition_hash": self._definition.definition_sha256,
            "bundle_hash": self._definition.source_sha256,
            "rows": [{"symbol": "AAPL", "metrics": {"momentum": "1.5"}}],
        }
        return McpToolResult(
            tool="read_artifact",
            is_error=False,
            structured={"content": json.dumps(document), "encoding": "utf-8"},
        )


def _definition() -> ScreenerDefinition:
    return ScreenerDefinition(
        name="momentum",
        lookback_days=20,
        bar_interval="1d",
        rank_metric="momentum",
        source="fn screen() {}",
    )


def _service(
    tools: FakeScreeningTools, definition: ScreenerDefinition
) -> ScreenerService:
    service = ScreenerService(tools=tools, sleep=lambda _seconds: None)
    service.save_definition("workspace-one", definition)
    return service


def _submitted_job_id(tools: FakeScreeningTools) -> str:
    job_id = next(
        args["job_id"] for name, args in tools.calls if name == "run_screener"
    )
    assert isinstance(job_id, str) and JOB_ID_PATTERN.fullmatch(job_id)
    return job_id


def test_run_allocates_one_job_id_before_screening() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition)
    service = _service(tools, definition)

    record = service.run("workspace-one", definition.definition_id, AS_OF)

    submitted = _submitted_job_id(tools)
    assert record.job_id == submitted
    assert record.result_id.startswith("sha256:")
    follow_up = {
        name: args["job_id"] for name, args in tools.calls if name != "run_screener"
    }
    assert follow_up == {
        "get_job_status": submitted,
        "get_job_result": submitted,
        "list_artifacts": submitted,
        "read_artifact": submitted,
    }
    assert service.get_result("workspace-one", record.result_id) == record


def test_run_allocates_a_fresh_job_id_per_execution() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition)
    service = _service(tools, definition)

    first = service.run("workspace-one", definition.definition_id, AS_OF)
    second = service.run("workspace-one", definition.definition_id, AS_OF)

    assert first.job_id != second.job_id
    assert first.result_id != second.result_id


def test_run_rejects_a_mismatched_acknowledged_job_id() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, acknowledged_job_id="job-other")
    service = _service(tools, definition)

    with pytest.raises(ScreeningRunError, match="job-other") as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
    assert [name for name, _ in tools.calls] == ["run_screener"]
    assert service.store.list_records("workspace-one") == ()


def test_run_rejects_an_absent_acknowledged_job_id() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, omit_job_id=True)
    service = _service(tools, definition)

    with pytest.raises(ScreeningRunError, match="no run id") as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
    assert [name for name, _ in tools.calls] == ["run_screener"]
    assert service.store.list_records("workspace-one") == ()


def test_run_rejects_a_retired_screen_schema() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, schema="fca-harness-screen-v1")
    service = _service(tools, definition)

    with pytest.raises(ScreeningArtifactError, match="schema"):
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert service.store.list_records("workspace-one") == ()


def test_run_rejects_a_retired_artifact_status() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, artifact_status="complete")
    service = _service(tools, definition)

    with pytest.raises(ScreeningArtifactError, match="invalid status"):
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert service.store.list_records("workspace-one") == ()


class TransportFailingTools:
    """A screening provider whose launch transport never completes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_screener(
        self,
        *,
        config: Mapping[str, object],
        workspace: str | None = None,
        job_id: str | None = None,
    ) -> McpToolResult:
        self.calls.append("run_screener")
        raise HarnessMcpTransportError("connection reset after dispatch")

    def get_job_status(self, *, job_id: str) -> McpToolResult:
        self.calls.append("get_job_status")
        raise HarnessMcpTransportError("connection reset after dispatch")

    def get_job_result(self, *, job_id: str) -> McpToolResult:
        self.calls.append("get_job_result")
        raise HarnessMcpTransportError("connection reset after dispatch")

    def list_artifacts(self, *, job_id: str) -> McpToolResult:
        self.calls.append("list_artifacts")
        raise HarnessMcpTransportError("connection reset after dispatch")

    def read_artifact(self, *, job_id: str, name: str) -> McpToolResult:
        self.calls.append("read_artifact")
        raise HarnessMcpTransportError("connection reset after dispatch")


def test_unknown_outcome_retains_the_submitted_job_id_without_replay() -> None:
    definition = _definition()
    tools = TransportFailingTools()
    service = ScreenerService(tools=tools, sleep=lambda _seconds: None)
    service.save_definition("workspace-one", definition)

    with pytest.raises(ScreenerUnavailableError) as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    retained = caught.value.job_id
    assert isinstance(retained, str) and JOB_ID_PATTERN.fullmatch(retained)
    assert tools.calls == ["run_screener"]
    assert service.store.list_records("workspace-one") == ()
