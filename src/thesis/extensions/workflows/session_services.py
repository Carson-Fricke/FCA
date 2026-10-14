"""Compose FCA workflow capabilities by durable execution identity."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.records import RecordNotFound, SubAgentRecord
from thesis_runtime.conversations.service import ChatService

from .bindings import WorkflowSessionBindings, WorkflowSessionServices

if TYPE_CHECKING:
    from .hypothesis_development.session_services import HypothesisSessionServices
    from .research_investigation.runtime import ResearchInvestigationRuntime


class WorkflowSessionRouter:
    """Dispatch session policies without teaching the neutral host FCA phases."""

    def __init__(
        self,
        chat: ChatService,
        hypothesis: HypothesisSessionServices,
        research: ResearchInvestigationRuntime,
    ) -> None:
        self.chat = chat
        self.hypothesis = hypothesis
        self.research = research

    def _service(self, workspace_id: str, execution_id: str) -> WorkflowSessionServices:
        execution = self.chat.get_execution(workspace_id, execution_id)
        if execution.workflow_id == self.research.workflow.workflow_id:
            return self.research
        if execution.workflow_id == self.hypothesis.workflow_id:
            return self.hypothesis
        raise ValueError(
            f"no session capabilities registered for {execution.workflow_id}"
        )

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings:
        if context.workflow_execution_id is None:
            return WorkflowSessionBindings()
        service = self._service(context.workspace_id, context.workflow_execution_id)
        bindings = service.resolve(context)
        if service is self.hypothesis and context.phase_executor:
            record = self.research.conversations.store.get_sub_agent(
                context.workspace_id, context.sub_agent_id or ""
            )
            if record is not None and record.phase == "discovery":
                bindings = replace(
                    bindings,
                    tools=(*bindings.tools, *self.research.launch_tools(context)),
                )
        return bindings

    def validate_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        if record.workflow_execution_id is None:
            return "phase completion requires an execution"
        return self._service(
            workspace_id, record.workflow_execution_id
        ).validate_completion(workspace_id, record, completion)

    def apply_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        if record.workflow_execution_id is None:
            return None
        return self._service(
            workspace_id, record.workflow_execution_id
        ).apply_completion(workspace_id, record, completion)

    def fail(self, record: SubAgentRecord, error: Exception) -> None:
        if record.workflow_execution_id is None:
            return
        try:
            service = self._service(record.workspace_id, record.workflow_execution_id)
        except RecordNotFound:
            return
        service.fail(record, error)
