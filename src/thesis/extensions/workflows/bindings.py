"""FCA workflow capabilities exposed to application composition and tool factories."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from thesis.experiments.mandate import DataWindowResolver
from thesis.extensions.tools.program import ContextJobRetention
from thesis.knowledge.service import KnowledgeScope
from thesis.strategies import WorkspaceStrategyArtifacts
from thesis.workflows.research_models import ProgramSnapshots
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.records import SubAgentRecord
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution


class RuntimeToolActivity(Protocol):
    """Execute an internal tool through the conversation's activity writer."""

    def __call__(
        self,
        context: ToolContext,
        tool: str,
        arguments: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution: ...


@dataclass(frozen=True, slots=True)
class WorkflowSessionBindings:
    """Workflow-owned capabilities resolved for one trusted session context."""

    tools: tuple[RegisteredTool, ...] = ()
    completion_schema: Mapping[str, object] | None = None
    knowledge_scope: KnowledgeScope = "both"
    knowledge_collections: tuple[str, ...] | None = None
    strategy_authoring: WorkspaceStrategyArtifacts | None = None
    programs: ProgramSnapshots | None = None
    resolve_data_window: DataWindowResolver | None = None
    job_retention: ContextJobRetention | None = None


class WorkflowSessionServices(Protocol):
    """Reusable port between conversation tools and a workflow's policies."""

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings: ...

    def validate_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None: ...

    def apply_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None: ...

    def fail(self, record: SubAgentRecord, error: Exception) -> None: ...
