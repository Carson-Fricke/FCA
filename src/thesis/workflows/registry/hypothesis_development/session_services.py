"""Hypothesis workflow policy and completion services for conversation tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.agent.hypothesis_skills import with_hypothesis_development_skills
from thesis.agent.router import Role, RolePolicy
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import PhaseCompletionRejected
from thesis.agent.runtime.session import ToolExecution
from thesis.agent.runtime.store import SubAgentStore
from thesis.agent.tool_dispatch import with_harness_mcp
from thesis.chat.contracts import (
    RecordNotFound,
    SubAgentRecord,
    SubAgentStatus,
    WorkflowExecution,
)
from thesis.chat.service import ChatService
from thesis.knowledge.service import KnowledgeScope
from thesis.strategies import WorkspaceStrategyArtifacts
from thesis.workflows.core.state import TERMINAL_EXECUTION_STATES
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import (
    ProgramSnapshots,
    StudyProgramReference,
    resolve_study_reference,
)
from thesis.workspaces.programs import ProgramArtifactError, ProgramKind

from .composers.hypothesis import _compose_universe
from .contracts.creation import HypothesisDevelopmentRequest
from .errors import FalsificationComposeError
from .spec import build_hypothesis_development_workflow
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

if TYPE_CHECKING:
    from thesis.workflows.runtime import RuntimeToolActivity, WorkflowSessionBindings


def role_policies(profile_id: str) -> dict[Role, RolePolicy]:
    """Select the workflow's role skills and harness capability surface."""
    return {
        role: with_hypothesis_development_skills(
            with_harness_mcp(
                RolePolicy(
                    role=role,
                    version="1.0.0",
                    model_profile=profile_id,
                    allowed_tools=(),
                )
            )
        )
        for role in ("research", "critique", "coding")
    }


class HypothesisSessionServices:
    """Resolve workflow capabilities without authoring conversation persistence."""

    def __init__(
        self,
        *,
        chat: ChatService,
        store: SubAgentStore,
        workflow: Callable[
            [],
            Workflow[
                HypothesisDevelopmentRequest,
                HypothesisDevelopmentState,
                HypothesisDevelopmentOutcome,
            ],
        ]
        | None = None,
        harness_tools: HarnessToolProvider | None,
        activity: RuntimeToolActivity,
        strategy_artifacts: WorkspaceStrategyArtifacts | None = None,
        programs: ProgramSnapshots | None = None,
        knowledge_scope: KnowledgeScope = "both",
    ) -> None:
        self._chat = chat
        self._store = store
        self._workflow = workflow
        self._spec = (
            build_hypothesis_development_workflow(knowledge_scope=knowledge_scope).spec
            if workflow is None
            else None
        )
        self._harness = harness_tools
        self._activity = activity
        self._programs = programs

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings:
        """Resolve schema, retrieval policy, and workspace authoring authority."""
        from thesis.workflows.runtime import WorkflowSessionBindings

        if context.workflow_execution_id is None:
            return WorkflowSessionBindings()
        record = (
            None
            if context.sub_agent_id is None
            else self._store.get_sub_agent(context.workspace_id, context.sub_agent_id)
        )
        spec = self._workflow().spec if self._workflow is not None else self._spec
        assert spec is not None
        if record is not None and (
            record.chat_id != context.chat_id
            or context.conversation_id != record.sub_agent_id
            or record.workflow_execution_id != context.workflow_execution_id
        ):
            return WorkflowSessionBindings(
                knowledge_scope=spec.knowledge_scope, knowledge_collections=()
            )
        phase_executor = record is not None and record.phase_executor
        phase = record.phase if phase_executor and record is not None else None
        schema = None if phase is None else spec.completion_schemas.get(phase)
        if phase is None:
            # Knowledge policy reads current execution attribution, not a helper's
            # stale parent phase. This does not grant executor capabilities.
            execution = self._chat.persistence.get(
                WorkflowExecution, context.workspace_id, context.workflow_execution_id
            )
            if (
                execution is None
                or execution.chat_id != context.chat_id
                or execution.workspace_id != context.workspace_id
            ):
                return WorkflowSessionBindings(
                    knowledge_scope=spec.knowledge_scope, knowledge_collections=()
                )
            phase = execution.phase
        return WorkflowSessionBindings(
            completion_schema=schema,
            knowledge_scope=spec.knowledge_scope,
            knowledge_collections=None
            if phase is None
            else spec.phase_knowledge.get(phase),
            programs=self._programs
            if phase_executor
            and phase
            in (
                "discovery",
                "model_creation",
                "strategy_design",
                "strategy_backtesting",
                "optimization",
            )
            else None,
        )

    def validate_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        """Verify latest stored validation without compiling or repairing source."""
        failure = self._acceptance_error(workspace_id, record, completion)
        if failure is not None:
            return failure
        if record.phase not in ("model_creation", "strategy_design"):
            return None
        receipt = completion.get("strategy")
        kind: ProgramKind = "strategy"
        if record.phase == "model_creation":
            kind = "study"
            hypothesis = completion.get("hypothesis")
            receipt = (
                hypothesis.get("model") if isinstance(hypothesis, Mapping) else None
            )
        if self._programs is None:
            return "workspace programs are not configured"
        if not isinstance(receipt, Mapping):
            return "completion requires a committed program reference"
        if receipt.get("workspaceId") != workspace_id or receipt.get("kind") != kind:
            return "program reference workspace or kind does not match the phase"
        context = ToolContext(
            workspace_id=workspace_id,
            chat_id=record.chat_id,
            conversation_id=record.sub_agent_id,
            actor_id=record.created_by,
            sub_agent_id=record.sub_agent_id,
            parent_sub_agent_id=record.parent_sub_agent_id,
            workflow_execution_id=record.workflow_execution_id,
            phase_attempt_id=record.phase_attempt_id,
            allowed_tools=record.allowed_tools,
            intent_epoch=self._completion_epoch(completion),
            phase_executor=True,
        )

        def verify() -> ToolExecution:
            assert self._programs is not None
            try:
                program_id = receipt.get("programId")
                commit = receipt.get("gitCommit")
                if not isinstance(program_id, str) or not isinstance(commit, str):
                    raise ProgramArtifactError(
                        "program reference requires programId and gitCommit"
                    )
                pinned = self._programs.load(workspace_id, kind, program_id, commit)
                latest = self._programs.load(workspace_id, kind, program_id)
                for key, expected in (
                    ("workspaceId", workspace_id),
                    ("kind", kind),
                    ("programId", program_id),
                    ("version", receipt.get("version")),
                ):
                    if pinned.get(key) != expected or latest.get(key) != expected:
                        raise ProgramArtifactError(
                            "program reference is stale or mismatched; read and validate the latest ID"
                        )
                if pinned.get("gitCommit") != commit or pinned.get("version") is None:
                    raise ProgramArtifactError(
                        "program commit does not match reference"
                    )
                validation = latest.get("validation")
                if (
                    not isinstance(validation, Mapping)
                    or validation.get("accepted") is not True
                ):
                    raise ProgramArtifactError("program requires accepted validation")
                if validation.get("gitCommit") != commit:
                    raise ProgramArtifactError(
                        "completion must use the latest validated commit"
                    )
                for key in (
                    "workspaceId",
                    "kind",
                    "programId",
                    "version",
                    "sourceSha256",
                    "bundleSha256",
                    "files",
                ):
                    if validation.get(key) != latest.get(key):
                        raise ProgramArtifactError(
                            "program validation is stale or mismatched"
                        )
                if not latest.get("sdkIdentity") or validation.get(
                    "sdkIdentity"
                ) != latest.get("sdkIdentity"):
                    raise ProgramArtifactError(
                        "program validation SDK identity is stale"
                    )
                compiler_identity = latest.get("validationIdentity")
                if (
                    not isinstance(compiler_identity, str)
                    or not compiler_identity.strip()
                    or validation.get("validationIdentity") != compiler_identity
                ):
                    raise ProgramArtifactError(
                        "program validation compiler identity is stale or unavailable; validate the latest ID again"
                    )
                if kind == "study":
                    model = resolve_study_reference(
                        self._programs,
                        workspace_id,
                        StudyProgramReference.from_document(receipt),
                    )
                    assert isinstance(hypothesis, Mapping)
                    _compose_universe(hypothesis.get("universe"), model)
                return ToolExecution(ok=True)
            except (
                ValueError,
                OSError,
                KeyError,
                TypeError,
                FalsificationComposeError,
            ) as error:
                return ToolExecution(ok=False, text=str(error))

        result = self._activity(
            context,
            "verify_program_reference",
            {"reference": dict(receipt)},
            lambda: self._guarded_validation(workspace_id, record, completion, verify),
        )
        failure = None if result.ok else result.text
        return self._acceptance_error(workspace_id, record, completion) or failure

    def _guarded_validation(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
        execute: Callable[[], ToolExecution],
    ) -> ToolExecution:
        failure = self._acceptance_error(workspace_id, record, completion)
        if failure is not None:
            raise PhaseCompletionRejected(failure)
        return execute()

    @staticmethod
    def _completion_epoch(completion: Mapping[str, object]) -> int:
        epoch = completion.get("intentEpoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise PhaseCompletionRejected("completion requires a trusted intentEpoch")
        return epoch

    def _acceptance_error(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        if not record.phase_executor or record.workflow_execution_id is None:
            return "only the owning phase executor may complete a workflow phase"
        current = self._store.get_sub_agent(workspace_id, record.sub_agent_id)
        if (
            current is None
            or not current.phase_executor
            or current.workflow_execution_id != record.workflow_execution_id
            or current.chat_id != record.chat_id
            or current.phase != record.phase
            or current.phase_attempt_id != record.phase_attempt_id
            or current.status
            in {
                SubAgentStatus.COMPLETED,
                SubAgentStatus.FAILED,
                SubAgentStatus.CANCELLED,
            }
        ):
            return "phase executor attempt is no longer current"
        try:
            execution = self._chat.get_execution(
                workspace_id, record.workflow_execution_id
            )
            epoch = self._completion_epoch(completion)
        except (RecordNotFound, PhaseCompletionRejected) as error:
            return str(error)
        if execution.intent_epoch != epoch:
            return "completion intent epoch is stale"
        if execution.state in TERMINAL_EXECUTION_STATES:
            return "execution is no longer accepting completions"
        if self._workflow is not None:
            workflow = self._workflow()
            request = replace(
                HypothesisDevelopmentRequest.from_execution(self._chat, execution),
                intent_epoch=epoch,
            )
            state = workflow._reconstruct_state(request)
            if (
                current.parent_sub_agent_id != workflow._workflow_node_id(request)
                or current.phase != state.phase.value
                or current.phase_attempt_id
                != workflow._phase_attempt_id(request, state.phase.value)
            ):
                return "completion does not belong to the current phase executor"
        return None

    def apply_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Apply the accepted contract through the workflow's own state machine."""
        if (
            not record.phase_executor
            or record.workflow_execution_id is None
            or self._workflow is None
        ):
            return None
        failure = self._acceptance_error(workspace_id, record, completion)
        if failure is not None:
            raise PhaseCompletionRejected(failure)
        execution = self._chat.get_execution(workspace_id, record.workflow_execution_id)
        request = replace(
            HypothesisDevelopmentRequest.from_execution(self._chat, execution),
            intent_epoch=self._completion_epoch(completion),
        )
        return self._workflow().apply_phase_completion(
            request, record.sub_agent_id, completion
        )

    def fail(self, record: SubAgentRecord, error: Exception) -> None:
        """Fail and report the owning execution through its workflow."""
        if (
            not record.phase_executor
            or record.workflow_execution_id is None
            or self._workflow is None
        ):
            return
        try:
            execution = self._chat.get_execution(
                record.workspace_id, record.workflow_execution_id
            )
        except RecordNotFound:
            return
        request = HypothesisDevelopmentRequest.from_execution(self._chat, execution)
        self._workflow().fail_execution(
            request, reason=str(error) or type(error).__name__
        )
