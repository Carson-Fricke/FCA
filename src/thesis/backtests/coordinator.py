"""Materialize harness backtest jobs into the workspace backtest store.

The chat tool loop emits a :class:`~thesis_runtime.contracts.reply.ToolResultChunk` for
every ``run_backtest``/``get_job_result`` call.  The coordinator recognizes
those results and either imports an already-terminal job (attaching the
``job_id`` to the tool message) or reports the job id and state so the UI can
call the Thesis materialization endpoint once the job reaches a terminal state.
It never blocks the reply on a long poll, so local and remote (Azure)
execution behave the same.

Harness job records own execution state and results; this coordinator only
projects them into workspace reports.  The tracking classification from
:mod:`thesis_runtime.contracts.records` decides whether a job id may be treated as a
harness execution at all, so a generic/non-harness tool job is never observed,
queried, or reported here, and a pre-dispatch failure is never presented as a
failed harness execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.application.job_classification import is_harness_execution_tool
from thesis.backtests.records import BacktestRecord, BacktestStore
from thesis.backtests.run_import import (
    _COMPLETE_STATES,
    _FAILED_STATES,
    BacktestRunError,
    BacktestRunPending,
    import_backtest_run,
)
from thesis.integrations.mcp import TOOL_GET_JOB_RESULT, TOOL_RUN_BACKTEST
from thesis_runtime.contracts.records import ToolJob

#: Harness operations whose tool results can carry a backtest observation.
_OBSERVED_TOOLS = frozenset({TOOL_RUN_BACKTEST, TOOL_GET_JOB_RESULT})

#: Resolves one ``(workspace_id, job_id)`` to its Thesis tracking record.
TrackingLookup = Callable[[str, str], ToolJob | None]


class BacktestRunCoordinator:
    """Import harness backtest jobs and serve the materialization endpoint."""

    def __init__(
        self,
        store: BacktestStore,
        tools: HarnessToolProvider,
        *,
        tracking: TrackingLookup | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._tools = tools
        self._tracking = tracking
        self._clock = clock if clock is not None else _utc_now

    def tracked_job(self, workspace_id: str, job_id: str) -> ToolJob | None:
        """Return the tracking record for one job id, when the boundary knows it."""
        if self._tracking is None:
            return None
        return self._tracking(workspace_id, job_id)

    def handle_tool_result(
        self,
        workspace_id: str,
        tool_result: HarnessToolResult,
        *,
        chat_id: str | None = None,
        execution_id: str | None = None,
    ) -> Mapping[str, object] | None:
        """Enrich a backtest tool message; never blocks on a running job.

        Returns ``{"job_id","state",...}`` merged into the persisted tool
        message, or ``None`` when the tool result is not a backtest
        launch/result read or the job is a non-harness tracking job.
        """
        if tool_result.tool not in _OBSERVED_TOOLS or not tool_result.ok:
            return None
        structured = tool_result.structured
        job_id = _text(structured.get("job_id"))
        if job_id is None:
            return None
        tracked = self.tracked_job(workspace_id, job_id)
        if tracked is not None and not is_harness_execution_tool(tracked.tool_name):
            return None
        state = _state_of(structured)
        if state is not None and state not in _COMPLETE_STATES:
            enrichment: dict[str, object] = {
                "job_id": job_id,
                "state": state,
            }
            if chat_id is not None:
                enrichment["chat_id"] = chat_id
            if execution_id is not None:
                enrichment["execution_id"] = execution_id
            return enrichment
        card = self.materialize(
            workspace_id,
            job_id,
            chat_id=chat_id,
            execution_id=execution_id,
            state=state,
        )
        enrichment = {
            "job_id": job_id,
            "state": card.get("state"),
        }
        if chat_id is not None:
            enrichment["chat_id"] = chat_id
        if execution_id is not None:
            enrichment["execution_id"] = execution_id
        return enrichment

    def materialize(
        self,
        workspace_id: str,
        job_id: str,
        *,
        chat_id: str | None = None,
        execution_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, object]:
        """Return the card payload for one job, importing it when terminal.

        The payload reports the job ``state`` (the single job vocabulary) and
        the workspace ``backtest`` projection when one exists.  A tracked
        non-harness job never consults the harness: it reports tracking
        certainty instead of a fabricated harness outcome.  An unresolved
        harness-backed job likewise reports its submission certainty rather
        than claiming a failed execution; only a harness-observed terminal
        failure produces ``state`` ``failed``.
        """
        tracked = self.tracked_job(workspace_id, job_id)
        if tracked is not None and not is_harness_execution_tool(tracked.tool_name):
            return _tracking_card(tracked)
        existing = self.find_by_job(workspace_id, job_id)
        if existing is not None:
            return _complete_card(job_id, existing)
        if state in _FAILED_STATES:
            return {"job_id": job_id, "state": state}
        try:
            record = import_backtest_run(
                tools=self._tools,
                workspace_id=workspace_id,
                job_id=job_id,
                chat_id=chat_id,
                execution_id=execution_id,
                clock=self._clock,
            )
        except BacktestRunPending:
            return {"job_id": job_id, "state": state or "running"}
        except BacktestRunError as error:
            if error.state in _FAILED_STATES:
                return {"job_id": job_id, "state": error.state}
            if tracked is not None:
                return _tracking_card(tracked, error=str(error))
            return {"job_id": job_id, "state": "unknown", "error": str(error)}
        record = self._store.add(record)
        return _complete_card(job_id, record)

    def find_by_job(self, workspace_id: str, job_id: str) -> BacktestRecord | None:
        """Return the workspace record already imported for ``job_id``, if any."""
        return self._store.get(workspace_id, job_id)


def _complete_card(job_id: str, record: BacktestRecord) -> dict[str, object]:
    return {
        "job_id": job_id,
        "state": record.status,
        "backtest": record.detail(),
    }


def _tracking_card(job: ToolJob, *, error: str | None = None) -> dict[str, object]:
    """Return a truthful tracking projection that never claims harness execution."""
    certainty = job.submission_certainty
    document: dict[str, object] = {
        "job_id": job.job_id,
        "state": job.state.value,
        "executionCertainty": certainty,
        "harness_execution": is_harness_execution_tool(job.tool_name),
    }
    if job.error_message is not None:
        document["error"] = job.error_message
    elif error is not None:
        document["error"] = error
    return document


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _state_of(document: Mapping[str, object]) -> str | None:
    state = _text(document.get("state"))
    return state.lower() if state is not None else None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["BacktestRunCoordinator"]
