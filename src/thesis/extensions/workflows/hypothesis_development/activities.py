"""Explicit execution capabilities granted to FCA workflow activities.

An activity can read its own execution, manage its attributed jobs, publish
progress and replace an artifact by revision. It cannot advance phases, spawn
agents, deliver messages, or access the host's persistence and scheduling.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from thesis_runtime.contracts.records import ChatMessage, ToolJob
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.events import EventReplay
from thesis_runtime.execution.workflow_events import (
    append_workflow_phase_result,
    append_workflow_progress,
)
from thesis_runtime.workflows.execution_store import ExecutionDocument, ExecutionStore

from .contracts.creation import HypothesisDevelopmentRequest
from .services import HypothesisDevelopmentServices


@dataclass(frozen=True, slots=True)
class ExecutionArtifacts:
    """Read history and CAS artifacts; transition writes remain host-only."""

    _store: ExecutionStore
    _workspace_id: str
    _execution_id: str

    def get(self, execution_id: str) -> ExecutionDocument:
        if execution_id != self._execution_id:
            raise PermissionError("activity cannot read another execution")
        document = self._store.get(execution_id)
        if document.workspace_id != self._workspace_id:
            raise PermissionError("execution belongs to another workspace")
        return document

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        self.get(execution_id)
        return self._store.put_phase_result(
            execution_id, phase, artifact, expected_revision=expected_revision
        )


@dataclass(frozen=True, slots=True)
class ActivityJobs:
    """A scope-checked job client, with no general conversation service access."""

    _chat: ChatService
    _request: HypothesisDevelopmentRequest

    def _scope(self, workspace_id: str, execution_id: str) -> None:
        if (workspace_id, execution_id) != (
            self._request.workspace_id,
            self._request.execution_id,
        ):
            raise PermissionError("activity cannot access another execution")

    def get_message(self, workspace_id: str, message_id: str) -> ChatMessage:
        if (workspace_id, message_id) != (
            self._request.workspace_id,
            self._request.trigger_message_id,
        ):
            raise PermissionError("activity may read only its launch message")
        return self._chat.get_message(workspace_id, message_id)

    def submit_tool_job(
        self, workspace_id: str, execution_id: str, **fields: Any
    ) -> ToolJob:
        self._scope(workspace_id, execution_id)
        return self._chat.submit_tool_job(workspace_id, execution_id, **fields)

    def find_execution_tool_job(
        self, workspace_id: str, execution_id: str, **fields: Any
    ) -> ToolJob | None:
        self._scope(workspace_id, execution_id)
        return self._chat.find_execution_tool_job(workspace_id, execution_id, **fields)

    def start_tool_job(self, workspace_id: str, job_id: str, **fields: Any) -> ToolJob:
        self._scope(
            workspace_id, self._chat.get_tool_job(workspace_id, job_id).execution_id
        )
        return self._chat.start_tool_job(workspace_id, job_id, **fields)

    def record_tool_result(
        self, workspace_id: str, job_id: str, **fields: Any
    ) -> ToolJob:
        self._scope(
            workspace_id, self._chat.get_tool_job(workspace_id, job_id).execution_id
        )
        return self._chat.record_tool_result(workspace_id, job_id, **fields)


@dataclass(frozen=True, slots=True)
class DevelopmentActivities:
    services: HypothesisDevelopmentServices
    chat: ActivityJobs | None
    execution_store: ExecutionArtifacts | None
    clock: Callable[[], datetime]
    id_factory: Callable[[str], str]
    _events: Callable[[str, str], EventReplay] | None = None
    _progress: Callable[..., None] | None = None
    _result: Callable[..., None] | None = None

    def observe(self, workspace_id: str, execution_id: str) -> EventReplay:
        if (
            self.execution_store is None
            or workspace_id != self.execution_store._workspace_id
        ):
            raise PermissionError("activity cannot observe another execution")
        self.execution_store.get(execution_id)
        if self._events is None:
            raise RuntimeError("execution events are not configured")
        return self._events(workspace_id, execution_id)

    def emit_phase_progress(
        self, request: HypothesisDevelopmentRequest, phase: object, **fields: Any
    ) -> None:
        if self._progress is not None:
            self._progress(request, phase, **fields)

    def emit_phase_result(
        self, request: HypothesisDevelopmentRequest, phase: object, **fields: Any
    ) -> None:
        if self._result is not None:
            self._result(request, phase, **fields)


def bind_activities(
    request: HypothesisDevelopmentRequest,
    services: HypothesisDevelopmentServices,
    chat: ChatService | None,
    store: ExecutionStore | None,
    clock: Callable[[], datetime],
    id_factory: Callable[[str], str],
) -> DevelopmentActivities:
    artifacts = (
        None
        if store is None
        else ExecutionArtifacts(store, request.workspace_id, request.execution_id)
    )
    jobs = None if chat is None else ActivityJobs(chat, request)

    def check(candidate: HypothesisDevelopmentRequest) -> None:
        if (candidate.workspace_id, candidate.execution_id) != (
            request.workspace_id,
            request.execution_id,
        ):
            raise PermissionError("activity scope is immutable")

    def progress(
        candidate: HypothesisDevelopmentRequest, phase: object, **fields: Any
    ) -> None:
        check(candidate)
        assert chat is not None
        append_workflow_progress(
            chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            occurred_at=clock(),
            phase=str(getattr(phase, "value", phase)),
            actor_id="thesis-hypothesis-development",
            intent_epoch=request.intent_epoch,
            **fields,
        )

    def result(
        candidate: HypothesisDevelopmentRequest, phase: object, **fields: Any
    ) -> None:
        check(candidate)
        assert chat is not None and artifacts is not None
        name = str(getattr(phase, "value", phase))
        document = artifacts.get(request.execution_id)
        artifacts.put_phase_result(
            request.execution_id,
            name,
            fields["artifact"],
            expected_revision=document.revision,
        )
        append_workflow_phase_result(
            chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            occurred_at=clock(),
            phase=name,
            actor_id="thesis-hypothesis-development",
            intent_epoch=request.intent_epoch,
            **fields,
        )

    return DevelopmentActivities(
        services,
        jobs,
        artifacts,
        clock,
        id_factory,
        None if chat is None else chat.replay_events,
        None if chat is None else progress,
        None if chat is None else result,
    )
