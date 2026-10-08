"""Workspace screener service over the injected harness MCP tool provider.

The service owns the thin-client lifecycle: persist definitions, materialize
one run configuration from a definition, launch ``run_screener``, poll
``get_job_status`` to a terminal state with a bounded injectable sleep,
import the harness ``screen.json`` artifact, and persist the result record.
There is no partial success: an unavailable harness or a failed run raises a
typed error and no record is stored.  The service never reads market data; the
harness executes the authored program through its lake routes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from uuid import uuid4

from thesis.integrations.mcp import HarnessMcpError, McpToolResult
from thesis.screening.definitions import ScreenerDefinition
from thesis.screening.runs import (
    ScreeningRecord,
    ScreeningRunError,
    ScreeningToolProvider,
    import_screening_run,
)
from thesis.screening.store import InMemoryScreenerStore, ScreenerStore

#: Screening job states the status tool reports while the run is in flight.
_PENDING_JOB_STATES = frozenset({"queued", "running"})
#: Screening job states that ended without a usable result.
_FAILED_JOB_STATES = frozenset({"failed", "cancelled"})
#: Screening job states that ended with a result.
_COMPLETE_JOB_STATES = frozenset({"succeeded"})


class ScreenerError(RuntimeError):
    """Base class for screener service failures.

    ``job_id`` carries the retained execution identity when one was allocated,
    so a caller can inspect an unknown outcome instead of replaying it.
    """

    def __init__(self, message: str, *, job_id: str | None = None) -> None:
        super().__init__(message)
        self.job_id = job_id


class ScreenerUnavailableError(ScreenerError):
    """The harness MCP boundary is not composed or could not be reached."""


class ScreenerService:
    """Workspace-scoped facade over the harness screener MCP tools."""

    def __init__(
        self,
        *,
        tools: ScreeningToolProvider | None = None,
        store: ScreenerStore | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 0.25,
        max_polls: int = 240,
    ) -> None:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if max_polls < 1:
            raise ValueError("max_polls must be a positive integer")
        self._tools = tools
        self._store = store if store is not None else InMemoryScreenerStore()
        self._sleep = sleep
        self._poll_interval_seconds = poll_interval_seconds
        self._max_polls = max_polls

    @property
    def tools(self) -> ScreeningToolProvider | None:
        """Return the injected harness MCP tool provider, if composed."""
        return self._tools

    @property
    def store(self) -> ScreenerStore:
        """Return the persistence boundary for definitions and records."""
        return self._store

    def save_definition(
        self, workspace_id: str, definition: ScreenerDefinition
    ) -> ScreenerDefinition:
        """Persist one content-addressed definition and return it."""
        return self._store.save_definition(
            _required_text(workspace_id, "workspace id"), definition
        )

    def list_definitions(self, workspace_id: str) -> tuple[ScreenerDefinition, ...]:
        """List the workspace's definitions ordered by content address."""
        return self._store.list_definitions(
            _required_text(workspace_id, "workspace id")
        )

    def run(self, workspace_id: str, definition_id: str, as_of: str) -> ScreeningRecord:
        """Launch, await, and import one screening run for a definition.

        Raises :class:`KeyError` for an unknown definition,
        :class:`ScreenerUnavailableError` when no harness boundary is composed
        or the launch/poll transport fails, and :class:`ScreeningRunError` for
        a terminal harness failure, a status timeout, or an unusable result
        artifact.  A record is stored only after a complete import.
        """
        workspace = _required_text(workspace_id, "workspace id")
        resolved_id = _required_text(definition_id, "definition id")
        definition = self._store.get_definition(workspace, resolved_id)
        if definition is None:
            raise KeyError(resolved_id)
        if self._tools is None:
            raise ScreenerUnavailableError(
                "no harness MCP boundary is composed for screening"
            )
        job_id = f"job-{uuid4().hex}"
        try:
            config = definition.materialize_config(as_of=as_of)
            launched = self._tools.run_screener(
                config=config, workspace=workspace, job_id=job_id
            )
            if launched.is_error:
                raise ScreenerUnavailableError(
                    _failure(launched, "run_screener"), job_id=job_id
                )
            _require_acknowledged_job_id(launched, job_id)
            self._await_terminal(job_id)
            record = import_screening_run(
                tools=self._tools,
                workspace_id=workspace,
                definition=definition,
                job_id=job_id,
                as_of=as_of,
            )
        except ScreeningRunError as error:
            if error.job_id is None:
                error.job_id = job_id
            raise
        except HarnessMcpError as error:
            raise ScreenerUnavailableError(
                f"the harness MCP screener boundary is unavailable: {error}",
                job_id=job_id,
            ) from error
        return self._store.add_record(workspace, record)

    def get_result(self, workspace_id: str, result_id: str) -> ScreeningRecord:
        """Return one imported result, or raise :class:`KeyError` when absent."""
        record = self._store.get_record(
            _required_text(workspace_id, "workspace id"),
            _required_text(result_id, "result id"),
        )
        if record is None:
            raise KeyError(result_id)
        return record

    def list_results(self, workspace_id: str) -> tuple[ScreeningRecord, ...]:
        """List the workspace's results ordered by result id."""
        return self._store.list_records(_required_text(workspace_id, "workspace id"))

    def _await_terminal(self, job_id: str) -> None:
        assert self._tools is not None
        for attempt in range(self._max_polls):
            status = self._tools.get_job_status(job_id=job_id)
            if status.is_error:
                raise ScreenerUnavailableError(_failure(status, "get_job_status"))
            state = _state_of(status.structured)
            if state in _COMPLETE_JOB_STATES:
                return
            if state in _FAILED_JOB_STATES:
                raise ScreeningRunError(
                    f"harness screener run {job_id!r} ended in state {state!r}",
                    job_id=job_id,
                )
            if state is None:
                raise ScreeningRunError(
                    f"harness screener run {job_id!r} reported no state",
                    job_id=job_id,
                )
            if state not in _PENDING_JOB_STATES:
                raise ScreeningRunError(
                    f"harness screener run {job_id!r} reported unknown state {state!r}",
                    job_id=job_id,
                )
            if attempt + 1 < self._max_polls:
                self._sleep(self._poll_interval_seconds)
        raise ScreeningRunError(
            f"harness screener run {job_id!r} did not reach a terminal state "
            f"within {self._max_polls} status polls",
            job_id=job_id,
        )


def _require_acknowledged_job_id(result: McpToolResult, submitted: str) -> None:
    job_id = result.structured.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ScreeningRunError("run_screener returned no run id", job_id=submitted)
    if job_id.strip() != submitted:
        raise ScreeningRunError(
            f"run_screener acknowledged job_id {job_id.strip()!r} for "
            f"submitted job_id {submitted!r}",
            job_id=submitted,
        )


def _failure(result: McpToolResult, tool: str) -> str:
    message = result.text.strip()
    if message:
        return f"harness MCP tool {tool!r} failed: {message}"
    return f"harness MCP tool {tool!r} failed"


def _state_of(document: Mapping[str, object]) -> str | None:
    value = document.get("state")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "ScreenerError",
    "ScreenerService",
    "ScreenerUnavailableError",
]
