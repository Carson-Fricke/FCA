"""Materialize harness backtest runs into the workspace backtest store.

The chat tool loop emits a :class:`~thesis.chat.responder.ToolResultChunk` for
every ``run_backtest``/``get_run`` call.  The coordinator recognizes those
results and either imports an already-terminal run (attaching ``backtest_id`` to
the tool message) or reports the run id and state so the UI can call the Thesis
materialization endpoint once the run reaches a terminal state.  It never blocks
the reply on a long poll, so local and remote (Azure) execution behave the same.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.run_import import (
    _FAILED_STATES,
    BacktestRunError,
    BacktestRunPending,
    import_backtest_run,
)
from thesis.http.backtests import BacktestDataService, BacktestRecord
from thesis.integrations.mcp import (
    TOOL_GET_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_RUN_BACKTEST,
)

_OBSERVED_TOOLS = frozenset({TOOL_RUN_BACKTEST, TOOL_GET_RUN})


class BacktestRunCoordinator:
    """Import harness backtest runs and serve the materialization endpoint."""

    def __init__(
        self,
        service: BacktestDataService,
        tools: HarnessToolProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._service = service
        self._tools = tools
        self._clock = clock if clock is not None else _utc_now
        self._id_factory = id_factory

    @property
    def service(self) -> BacktestDataService:
        return self._service

    def handle_tool_result(
        self,
        workspace_id: str,
        tool_result: HarnessToolResult,
        *,
        chat_id: str | None = None,
        execution_id: str | None = None,
    ) -> Mapping[str, object] | None:
        """Enrich a backtest tool message; never blocks on a running run.

        Returns ``{"run_id","state",...}`` (plus ``backtest_id`` when the run is
        already terminal) merged into the persisted tool message, or ``None``
        when the tool result is not a backtest launch/read.
        """
        if tool_result.tool not in _OBSERVED_TOOLS or not tool_result.ok:
            return None
        structured = tool_result.structured
        run_id = _text(structured.get("run_id"))
        job_id = _text(structured.get("job_id")) or run_id
        if job_id is None:
            return None
        state = _state_of(structured)
        if run_id is None:
            resolved = self._resolve_run(job_id)
            if resolved is not None:
                run_id, state = resolved
        if run_id is None:
            return {"job_id": job_id, "state": state or "running"}
        card = self.materialize(
            workspace_id,
            run_id,
            chat_id=chat_id,
            execution_id=execution_id,
            state=state,
        )
        enrichment: dict[str, object] = {
            "run_id": run_id,
            "state": card.get("state") or card.get("status"),
        }
        if "backtest_id" in card:
            enrichment["backtest_id"] = card["backtest_id"]
        if chat_id is not None:
            enrichment["chat_id"] = chat_id
        if execution_id is not None:
            enrichment["execution_id"] = execution_id
        return enrichment

    def materialize(
        self,
        workspace_id: str,
        run_id: str,
        *,
        chat_id: str | None = None,
        execution_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, object]:
        """Return the card payload for one run, importing it when terminal.

        ``status`` is ``pending``/``failed``/``complete``.  On ``complete`` the
        payload carries ``backtest_id`` and the workspace ``backtest`` detail so
        the UI renders the report; a repeated call returns the existing record.
        """
        existing = self.find_by_run(workspace_id, run_id)
        if existing is not None:
            return _complete_card(run_id, existing)
        if state in _FAILED_STATES:
            return {"status": "failed", "run_id": run_id, "state": state}
        try:
            record = import_backtest_run(
                tools=self._tools,
                workspace_id=workspace_id,
                run_id=run_id,
                chat_id=chat_id,
                execution_id=execution_id,
                clock=self._clock,
                id_factory=self._id_factory,
            )
        except BacktestRunPending:
            return {"status": "pending", "run_id": run_id, "state": "running"}
        except BacktestRunError as error:
            return {"status": "failed", "run_id": run_id, "error": str(error)}
        self._service.store.add(record)
        return _complete_card(run_id, record)

    def find_by_run(self, workspace_id: str, run_id: str) -> BacktestRecord | None:
        """Return the workspace record already imported for ``run_id``, if any."""
        for record in self._service.store.list(workspace_id):
            if record.run_id == run_id:
                return record
        return None

    def _resolve_run(self, job_id: str) -> tuple[str | None, str | None] | None:
        result = self._tools.call_tool(TOOL_GET_BACKTEST_STATUS, {"job_id": job_id})
        if not result.ok:
            return None
        return _text(result.structured.get("run_id")), _state_of(result.structured)


def _complete_card(run_id: str, record: BacktestRecord) -> dict[str, object]:
    return {
        "status": "complete",
        "run_id": run_id,
        "state": record.status,
        "backtest_id": record.id,
        "backtest": record.detail(),
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _state_of(document: Mapping[str, object]) -> str | None:
    state = _text(document.get("state")) or _text(document.get("status"))
    return state.lower() if state is not None else None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["BacktestRunCoordinator"]
