"""Screening launch and import behavior over the injected harness MCP boundary.

Launch allocates one execution ``job_id`` before harness submission.  The import
reads the harness job record and ``screen.json`` exactly as published: nothing
is recomputed, no value the harness did not publish is zero-filled, and an
empty row list says why it is empty.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import pytest

from thesis.integrations.mcp import HarnessMcpTransportError, McpToolResult
from thesis.screening.definitions import ScreenerDefinition
from thesis.screening.runs import (
    ScreeningArtifactError,
    ScreeningRunError,
    import_screening_run,
)
from thesis.screening.service import ScreenerService, ScreenerUnavailableError

JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{32}$")
AS_OF = "2026-01-02"


class FakeScreeningTools:
    """ScreeningToolProvider double recording every tool call.

    ``artifact`` overrides keys of the published ``screen.json`` document; a
    ``None`` value removes the key.  ``extra_artifacts`` maps further artifact
    names to documents the importer must never read.  ``job_state`` and
    ``job_error_message`` are the job record the harness reports.
    """

    def __init__(
        self,
        definition: ScreenerDefinition,
        *,
        acknowledged_job_id: str | None = None,
        omit_job_id: bool = False,
        schema: str = "fca-harness-screen-v2",
        artifact_status: str = "succeeded",
        artifact: Mapping[str, object] | None = None,
        extra_artifacts: Mapping[str, Mapping[str, object]] | None = None,
        publish_screen: bool = True,
        job_state: str = "succeeded",
        job_error_message: str | None = None,
    ) -> None:
        self._definition = definition
        self._acknowledged_job_id = acknowledged_job_id
        self._omit_job_id = omit_job_id
        self._schema = schema
        self._artifact_status = artifact_status
        self._artifact = dict(artifact or {})
        self._extra_artifacts = {
            name: dict(document) for name, document in (extra_artifacts or {}).items()
        }
        self._publish_screen = publish_screen
        self._job_state = job_state
        self._job_error_message = job_error_message
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
            tool="get_job_status", is_error=False, structured=self._job_record(job_id)
        )

    def get_job_result(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_job_result", {"job_id": job_id}))
        return McpToolResult(
            tool="get_job_result", is_error=False, structured=self._job_record(job_id)
        )

    def list_artifacts(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("list_artifacts", {"job_id": job_id}))
        names = ["screen.json"] if self._publish_screen else []
        names.extend(self._extra_artifacts)
        return McpToolResult(
            tool="list_artifacts",
            is_error=False,
            structured={
                "job_id": job_id,
                "artifacts": [
                    {"name": name, "uri": f"jobs/{job_id}/{name}", "sha256": "a" * 64}
                    for name in names
                ],
            },
        )

    def read_artifact(self, *, job_id: str, name: str) -> McpToolResult:
        self.calls.append(("read_artifact", {"job_id": job_id, "name": name}))
        if name == "screen.json":
            document: Mapping[str, object] = self._screen_document(job_id)
        else:
            document = self._extra_artifacts[name]
        return McpToolResult(
            tool="read_artifact",
            is_error=False,
            structured={"content": json.dumps(document), "encoding": "utf-8"},
        )

    def _job_record(self, job_id: str) -> dict[str, object]:
        """The job record shape the harness publishes (``JobRecord``)."""
        return {
            "job_id": job_id,
            "kind": "screen",
            "state": self._job_state,
            "error_message": self._job_error_message,
            "strategy_id": "screener",
        }

    def _screen_document(self, job_id: str) -> dict[str, object]:
        document: dict[str, object] = {
            "schema": self._schema,
            "status": self._artifact_status,
            "job_id": job_id,
            "as_of": AS_OF,
            "definition_id": self._definition.definition_id,
            "definition_hash": self._definition.definition_sha256,
            "bundle_hash": self._definition.source_sha256,
            "rank_metric": "momentum",
            "bar_interval": "1d",
            "universe_revision": None,
            "rows": [{"symbol": "AAPL", "metrics": {"momentum": "1.5"}}],
        }
        for key, value in self._artifact.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        return document


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


def test_published_rows_are_carried_as_published_with_no_reason() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        artifact={
            "rows": [
                {"symbol": "AAPL", "metrics": {"momentum": "1.5"}},
                {"symbol": "MSFT", "metrics": {"momentum": "0.25"}},
            ]
        },
    )
    service = _service(tools, definition)

    record = service.run("workspace-one", definition.definition_id, AS_OF)

    assert [row.symbol for row in record.rows] == ["AAPL", "MSFT"]
    assert [dict(row.metrics) for row in record.rows] == [
        {"momentum": "1.5"},
        {"momentum": "0.25"},
    ]
    assert record.rows_reason is None
    assert record.document()["rows_reason"] is None


def test_an_empty_published_row_list_says_it_is_empty() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, artifact={"rows": []})
    service = _service(tools, definition)

    record = service.run("workspace-one", definition.definition_id, AS_OF)

    assert record.rows == ()
    assert record.rows_reason == "artifact_has_no_rows"
    stored = service.get_result("workspace-one", record.result_id).document()
    assert stored["rows"] == []
    assert stored["rows_reason"] == "artifact_has_no_rows"


def test_a_metric_the_row_did_not_publish_is_absent_not_zero() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        artifact={
            "rows": [
                {"symbol": "AAPL", "metrics": {"momentum": "1.5"}},
                {"symbol": "MSFT", "metrics": {"volume": "9000"}},
            ]
        },
    )
    service = _service(tools, definition)

    record = service.run("workspace-one", definition.definition_id, AS_OF)

    assert "momentum" not in dict(record.rows[1].metrics)
    assert record.document()["rows"][1] == {
        "symbol": "MSFT",
        "metrics": {"volume": "9000"},
    }


def test_rows_come_only_from_the_published_screen_artifact() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        extra_artifacts={
            "fills.json": {"rows": [{"symbol": "MSFT", "metrics": {"fill": "10"}}]},
            "trades.json": {"rows": [{"symbol": "NVDA", "metrics": {"pnl": "-3"}}]},
        },
    )
    service = _service(tools, definition)

    record = service.run("workspace-one", definition.definition_id, AS_OF)

    assert [row.symbol for row in record.rows] == ["AAPL"]
    reads = [args["name"] for name, args in tools.calls if name == "read_artifact"]
    assert reads == ["screen.json"]


def test_a_succeeded_artifact_without_a_row_list_yields_no_record() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, artifact={"rows": None})
    service = _service(tools, definition)

    with pytest.raises(ScreeningArtifactError, match="row list"):
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert service.store.list_records("workspace-one") == ()


def test_a_row_without_metrics_fails_closed_instead_of_gaining_metrics() -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, artifact={"rows": [{"symbol": "AAPL"}]})
    service = _service(tools, definition)

    with pytest.raises(ScreeningArtifactError, match="'metrics'"):
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert service.store.list_records("workspace-one") == ()


@pytest.mark.parametrize(
    ("echoed", "message"),
    [
        ("job-other", "produced for job 'job-other'"),
        (None, "requires 'job_id'"),
    ],
    ids=["mismatched", "missing"],
)
def test_an_artifact_that_does_not_echo_the_imported_job_yields_no_record(
    echoed: str | None, message: str
) -> None:
    definition = _definition()
    tools = FakeScreeningTools(definition, artifact={"job_id": echoed})
    service = _service(tools, definition)

    with pytest.raises(ScreeningArtifactError, match=message) as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
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


@pytest.mark.parametrize(
    ("publish_screen", "artifact", "job_error", "reason"),
    [
        (
            True,
            {"status": "failed", "error": "universe unavailable"},
            "worker crashed",
            "universe unavailable",
        ),
        (False, None, "worker crashed", "worker crashed"),
        (False, None, None, "no failure reason was published"),
    ],
    ids=["artifact-reason", "job-record-reason", "no-reason-published"],
)
def test_a_failed_run_carries_the_reason_the_harness_published(
    publish_screen: bool,
    artifact: Mapping[str, object] | None,
    job_error: str | None,
    reason: str,
) -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        job_state="failed",
        job_error_message=job_error,
        artifact=artifact,
        publish_screen=publish_screen,
    )
    service = _service(tools, definition)

    with pytest.raises(ScreeningRunError, match=re.escape(reason)) as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
    assert service.store.list_records("workspace-one") == ()


def test_a_failed_run_never_adopts_another_jobs_artifact_error() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        job_state="failed",
        job_error_message="worker crashed",
        artifact={"job_id": "job-other", "status": "failed", "error": "foreign"},
    )
    service = _service(tools, definition)

    with pytest.raises(ScreeningRunError) as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert "worker crashed" in str(caught.value)
    assert "foreign" not in str(caught.value)


def test_a_failed_screen_artifact_yields_no_record_and_names_its_error() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition,
        artifact_status="failed",
        artifact={"error": "universe unavailable"},
    )
    service = _service(tools, definition)

    with pytest.raises(ScreeningRunError, match="universe unavailable") as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
    assert service.store.list_records("workspace-one") == ()


def test_importing_a_failed_job_names_the_published_reason() -> None:
    definition = _definition()
    tools = FakeScreeningTools(
        definition, job_state="failed", job_error_message="worker crashed"
    )

    with pytest.raises(ScreeningRunError, match="worker crashed"):
        import_screening_run(
            tools=tools,
            workspace_id="workspace-one",
            definition=definition,
            job_id="job-failed",
            as_of=AS_OF,
        )


class StatusFailingTools(FakeScreeningTools):
    """A screening provider whose job status tool answers with a tool error."""

    def get_job_status(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_job_status", {"job_id": job_id}))
        return McpToolResult(
            tool="get_job_status", is_error=True, text="status store unavailable"
        )


def test_a_status_tool_error_keeps_the_retained_job_id() -> None:
    definition = _definition()
    tools = StatusFailingTools(definition)
    service = _service(tools, definition)

    with pytest.raises(
        ScreenerUnavailableError, match="status store unavailable"
    ) as caught:
        service.run("workspace-one", definition.definition_id, AS_OF)

    assert caught.value.job_id == _submitted_job_id(tools)
    assert service.store.list_records("workspace-one") == ()
