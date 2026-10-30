"""Hypothesis workflow policy and completion services for conversation tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.agent.hypothesis_skills import with_hypothesis_development_skills
from thesis.agent.router import Role, RolePolicy
from thesis.agent.tool_dispatch import with_harness_mcp
from thesis.experiments.mandate import (
    DataWindowPolicy,
    MandateError,
    ResearchMandate,
    derive_mandate,
    mandate_section,
    resolve_research_mandate,
    rfc3339,
    rfc3339_ns,
)
from thesis.extensions.tools.program import ContextJobRetention
from thesis.knowledge.service import KnowledgeScope
from thesis.research.evidence import document_digest
from thesis.workflows.research_models import (
    ProgramSnapshots,
    StudyProgramReference,
    resolve_study_reference,
    study_configuration,
)
from thesis.workspaces.program_contracts import ProgramArtifactError, ProgramKind
from thesis.workspaces.programs import JobSettlement
from thesis.workspaces.sketch_contract import StrategySketchReference
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.failures import failure_from_exception
from thesis_runtime.contracts.records import (
    RecordNotFound,
    SubAgentRecord,
    SubAgentStatus,
    ToolJobState,
    WorkflowExecution,
)
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.subagents.control import PhaseCompletionRejected
from thesis_runtime.subagents.store import SubAgentStore
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.workflows.state import TERMINAL_EXECUTION_STATES
from thesis_runtime.workflows.workflow import Workflow

from .composers.hypothesis import _compose_universe
from .contracts.creation import HypothesisDevelopmentRequest
from .design import frozen_design
from .endpoint import HypothesisExtension
from .errors import FalsificationComposeError, WorkflowError
from .exposure import (
    record_completion_attempts,
    record_submission_exposure,
    submitted_exposures,
)
from .primitives import _WORKFLOW_ACTOR, _WORKFLOW_ID
from .provenance import PROVENANCE_PHASES, verify_phase_completion
from .spec import build_hypothesis_development_workflow
from .state import HypothesisDevelopmentOutcome, HypothesisDevelopmentState

if TYPE_CHECKING:
    from thesis.extensions.workflows.bindings import (
        RuntimeToolActivity,
        WorkflowSessionBindings,
    )


def _text_or_none(value: object) -> str | None:
    """Return attribution text, or ``None`` when the attribution omits it."""
    return value if isinstance(value, str) else None


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

    workflow_id = _WORKFLOW_ID

    def __init__(
        self,
        *,
        chat: ChatService,
        extension: HypothesisExtension | None = None,
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
        programs: ProgramSnapshots | None = None,
        knowledge_scope: KnowledgeScope = "both",
    ) -> None:
        self._chat = chat
        self._extension = extension
        self._store = store
        self._workflow = workflow
        if extension is None:
            configured = build_hypothesis_development_workflow(
                knowledge_scope=knowledge_scope
            )
            self._spec = configured.spec
            self._policy = cast(HypothesisExtension, configured.extension)
        else:
            self._spec = None
            self._policy = extension
        self._harness = harness_tools
        self._activity = activity
        self._programs = programs

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings:
        """Resolve schema, retrieval policy, and workspace authoring authority."""
        from thesis.extensions.workflows.bindings import WorkflowSessionBindings

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
                knowledge_scope=self._policy.knowledge_scope, knowledge_collections=()
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
                    knowledge_scope=self._policy.knowledge_scope,
                    knowledge_collections=(),
                )
            phase = execution.phase
        programs = (
            self._programs
            if phase_executor
            and phase
            in (
                "discovery",
                "model_creation",
                "strategy_design",
                "strategy_backtesting",
                "optimization",
                "null_testing",
                "variation",
                "forward_validation",
            )
            else None
        )

        def resolve_data_window() -> DataWindowPolicy:
            mandate = self._resolve_mandate(
                context.workspace_id, cast(str, context.workflow_execution_id)
            )
            if mandate is None:
                raise MandateError(
                    "Call set_research_window to declare development and forward "
                    "windows first, then retry this tool."
                )
            return (
                mandate.forward_window
                if phase == "forward_validation"
                else mandate.default_window
            )

        return WorkflowSessionBindings(
            tools=self._research_window_tools()
            if phase_executor and phase == "discovery"
            else (),
            completion_schema=schema,
            knowledge_scope=self._policy.knowledge_scope,
            knowledge_collections=None
            if phase is None
            else self._policy.phase_knowledge.get(phase),
            programs=programs,
            resolve_data_window=resolve_data_window if programs is not None else None,
            job_retention=(
                self._program_job_retention(phase)
                if programs is not None and phase is not None
                else None
            ),
        )

    def _research_window_tools(self) -> tuple[RegisteredTool, ...]:
        window = {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end"],
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Inclusive RFC3339 UTC timestamp.",
                },
                "end": {
                    "type": "string",
                    "description": "Exclusive RFC3339 UTC timestamp.",
                },
            },
        }
        return (
            RegisteredTool(
                descriptor={
                    "name": "set_research_window",
                    "description": "Declare and freeze development and sealed forward dataset windows before empirical work. Preserve the operator's dates; use launch time for 'now'. If dates were omitted, choose defensible windows. Signal lookbacks do not move these boundaries. Repeating the same declaration is safe; changing a frozen window requires a new execution.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["development", "forward"],
                        "properties": {"development": window, "forward": window},
                    },
                },
                handler=self._set_research_window,
                operation="write",
            ),
        )

    def _set_research_window(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
        try:
            if (
                self._extension is None
                or self._extension.execution_store is None
                or context.workflow_execution_id is None
            ):
                raise WorkflowError("research setup requires the execution store")
            record = self._store.get_sub_agent(
                context.workspace_id, context.sub_agent_id or ""
            )
            if (
                record is None
                or record.phase != "discovery"
                or not context.phase_executor
                or record.chat_id != context.chat_id
                or record.workflow_execution_id != context.workflow_execution_id
                or record.phase_attempt_id != context.phase_attempt_id
            ):
                raise WorkflowError(
                    "only the current discovery executor may declare research windows"
                )
            with self._chat.execution_guard(
                context.workspace_id, context.workflow_execution_id
            ):
                failure = self._acceptance_error(
                    context.workspace_id, record, {"intentEpoch": context.intent_epoch}
                )
                if failure is not None:
                    raise WorkflowError(failure)
                store = self._extension.execution_store
                document = store.get(context.workflow_execution_id)
                mandate = derive_mandate(
                    context.workspace_id,
                    context.workflow_execution_id,
                    {**arguments, "createdAt": document.launch_request["createdAt"]},
                )
                previous = document.phase_results.get("research_window")
                if previous is not None and previous != mandate.as_dict():
                    raise WorkflowError(
                        "research windows are frozen; start a new execution to change them"
                    )
                if previous is None:
                    store.put_phase_result(
                        context.workflow_execution_id,
                        "research_window",
                        mandate.as_dict(),
                        expected_revision=document.revision,
                    )
                return ToolExecution(
                    ok=True, structured=mandate.as_dict(), text=mandate_section(mandate)
                )
        except (ValueError, LookupError, WorkflowError) as error:
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "research_window",
                    "message": str(error),
                    "certainty": "not_started",
                },
            )

    def _program_job_retention(self, phase: str) -> ContextJobRetention:
        """Retain each program execution before dispatch, with its exposure.

        Only phase executors with program capability receive this closure: they
        own a live, steerable workflow execution, so the durable records can be
        written before the harness observes the id.  Helper and chat-level
        contexts keep retention absent rather than fabricating an execution.

        Each submission writes its observed exposure first, then its tool job,
        and only then dispatches.  A failure at either durable step settles the
        job as not started, so nothing unrecorded ever reaches the harness.  The
        record is started before dispatch so a possibly-dispatched job is never
        left submitted, which would read as not started.  The returned settlement
        closes the record only for definitive failures; unknown outcomes stay
        running for reconciliation by job id.
        """

        def retain(
            context: ToolContext,
            job_id: str,
            tool_name: str,
            attribution: Mapping[str, object],
        ) -> JobSettlement:
            execution_id = context.workflow_execution_id
            if execution_id is None:
                raise RecordNotFound("program retention requires a workflow execution")
            workflow = None if self._workflow is None else self._workflow()
            if workflow is None:
                raise WorkflowError("program submissions require the workflow runtime")
            execution = self._chat.get_execution(context.workspace_id, execution_id)
            request = self._request_from_execution(execution)
            scoped: dict[str, object] = {**attribution, "phase": phase}
            design_sha256: str | None = None
            if phase == "forward_validation":
                design_sha256 = self._forward_design_sha256(
                    workflow, request, context.workspace_id
                )
                scoped["designSha256"] = design_sha256
            null_plan = attribution.get("nullPlan")
            if isinstance(null_plan, Mapping) and phase != "discovery":
                prior = [
                    entry.get("nullPlan")
                    for entry in submitted_exposures(
                        self._activities(request), execution_id
                    )
                    if entry.get("phase") == phase
                    and entry.get("phaseAttemptId") == context.phase_attempt_id
                    and entry.get("nullPlan") is not None
                ]
                if any(
                    not isinstance(item, Mapping)
                    or document_digest(item) != document_digest(null_plan)
                    for item in prior
                ):
                    raise WorkflowError(
                        "null seed plan or configuration changed after first submission"
                    )
            scoped.pop("nullPlan", None)
            if isinstance(null_plan, Mapping):
                scoped["nullPlanSha256"] = document_digest(null_plan)
            job = self._chat.submit_tool_job(
                context.workspace_id,
                execution_id,
                tool_name=tool_name,
                actor_id=_WORKFLOW_ACTOR,
                job_id=job_id,
                attribution=scoped,
            )
            try:
                record_submission_exposure(
                    self._activities(request),
                    request,
                    phase=phase,
                    job_id=job.job_id,
                    tool_name=tool_name,
                    window_start=_text_or_none(attribution.get("windowStart")),
                    window_end=_text_or_none(attribution.get("windowEnd")),
                    parameter_sha256=_text_or_none(
                        attribution.get("parameterAssignmentSha256")
                    ),
                    design_sha256=design_sha256,
                    phase_attempt_id=context.phase_attempt_id,
                    null_plan=null_plan if isinstance(null_plan, Mapping) else None,
                )
            except Exception as error:  # noqa: BLE001 - exposure precedes dispatch
                self._chat.record_tool_result(
                    context.workspace_id,
                    job.job_id,
                    result={"executionCertainty": "not_started"},
                    actor_id=_WORKFLOW_ACTOR,
                    success=False,
                    error_message=f"exposure recording failed: {error}",
                )
                raise
            if job.state is ToolJobState.SUBMITTED:
                self._chat.start_tool_job(
                    context.workspace_id, job.job_id, actor_id=_WORKFLOW_ACTOR
                )

            def settle(certainty: str, message: str | None) -> None:
                if certainty not in ("not_started", "observed_failure"):
                    return
                self._chat.record_tool_result(
                    context.workspace_id,
                    job.job_id,
                    result={"executionCertainty": certainty},
                    actor_id=_WORKFLOW_ACTOR,
                    success=False,
                    error_message=message,
                )

            return settle

        return retain

    def _forward_design_sha256(
        self,
        workflow: Workflow[Any, Any, Any],
        request: HypothesisDevelopmentRequest,
        workspace_id: str,
    ) -> str:
        """Return the digest of the frozen design a forward submission runs under."""
        mandate = self._resolve_mandate(workspace_id, request.execution_id)
        if mandate is None:
            raise WorkflowError("forward validation requires the research mandate")
        state = self._state(request)
        window = mandate.forward_window
        return frozen_design(
            state, rfc3339(window.start_ns), rfc3339(window.end_ns)
        ).sha256()

    def _activities(self, request: HypothesisDevelopmentRequest):
        if self._extension is None:
            raise WorkflowError("workflow activities are not configured")
        return self._extension.activities(request)

    def _state(
        self, request: HypothesisDevelopmentRequest
    ) -> HypothesisDevelopmentState:
        if self._extension is None or self._extension.execution_store is None:
            raise WorkflowError("workflow execution storage is not configured")
        return self._extension.state(
            request,
            self._extension.execution_store.get(request.execution_id).transitions,
        )

    def _request_from_execution(
        self, execution: WorkflowExecution
    ) -> HypothesisDevelopmentRequest:
        """Recover the preserved idea from the durable launch document."""
        if self._workflow is None:
            raise WorkflowError("request recovery requires the workflow runtime")
        store = None if self._extension is None else self._extension.execution_store
        if store is None:
            raise WorkflowError("request recovery requires the execution store")
        document = store.get(execution.execution_id)
        return HypothesisDevelopmentRequest.from_execution(
            self._chat, execution, document.launch_request
        )

    def _resolve_mandate(
        self, workspace_id: str, execution_id: str | None
    ) -> ResearchMandate | None:
        """Read discovery's frozen research windows from the execution document."""
        if self._workflow is None or execution_id is None:
            return None
        return resolve_research_mandate(
            None if self._extension is None else self._extension.execution_store,
            workspace_id=workspace_id,
            execution_id=execution_id,
        )

    def _provenance_error(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        """Bind every job the completion reports to the run that executed it."""
        if (
            record.phase not in PROVENANCE_PHASES
            or record.workflow_execution_id is None
            or self._workflow is None
        ):
            return None
        execution = self._chat.get_execution(workspace_id, record.workflow_execution_id)
        request = self._request_from_execution(execution)
        state = self._state(request)
        mandate = self._resolve_mandate(workspace_id, record.workflow_execution_id)
        if mandate is None:
            return "Call set_research_window to declare development and forward windows first, then retry request_phase_completion."
        forward_window = None
        if mandate is not None:
            forward_window = (
                rfc3339(mandate.forward_window.start_ns),
                rfc3339(mandate.forward_window.end_ns),
            )
        return verify_phase_completion(
            chat=self._chat,
            workflow=self._activities(request),
            request=request,
            state=state,
            phase=record.phase,
            phase_attempt_id=record.phase_attempt_id,
            completion=completion,
            forward_window=forward_window,
            programs=self._programs,
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
        provenance = self._provenance_error(workspace_id, record, completion)
        if provenance is not None:
            return provenance
        if record.phase not in ("model_creation", "strategy_design"):
            return self._sketch_error(workspace_id, record, completion)
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
        mandate = self._resolve_mandate(workspace_id, record.workflow_execution_id)
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
                if mandate is not None:
                    configuration = latest.get("configuration")
                    if not isinstance(configuration, Mapping):
                        raise ProgramArtifactError(
                            "committed program configuration is unavailable"
                        )
                    try:
                        start_ns = rfc3339_ns(cast(str, configuration.get("start")))
                        end_ns = rfc3339_ns(cast(str, configuration.get("end")))
                    except (MandateError, TypeError) as error:
                        raise ProgramArtifactError(
                            "committed configuration window is malformed"
                        ) from error
                    violation = mandate.window_violation(start_ns, end_ns)
                    if violation is not None:
                        raise ProgramArtifactError(violation)
                if kind == "study":
                    model = resolve_study_reference(
                        self._programs,
                        workspace_id,
                        StudyProgramReference.from_document(receipt),
                    )
                    assert isinstance(hypothesis, Mapping)
                    study_configuration(
                        model,
                        job_id="validation",
                        universe=_compose_universe(hypothesis.get("universe")),
                    )
                return ToolExecution(ok=True)
            except (
                ValueError,
                OSError,
                KeyError,
                TypeError,
                FalsificationComposeError,
            ) as error:
                return ToolExecution(
                    ok=False,
                    text=str(error),
                    failure=failure_from_exception(error, kind="program_reference"),
                )

        result = self._activity(
            context,
            "verify_program_reference",
            {"reference": dict(receipt)},
            lambda: self._guarded_validation(workspace_id, record, completion, verify),
        )
        failure = None if result.ok else result.text
        return (
            self._acceptance_error(workspace_id, record, completion)
            or failure
            or self._sketch_error(workspace_id, record, completion)
        )

    def _sketch_error(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> str | None:
        """Bind scientific interpretation to the exact accepted sketch revision."""
        field = {
            "discovery": "discovery",
            "model_creation": "hypothesis",
            "verdict": "verdict",
        }.get(record.phase or "")
        if field is None:
            return None
        payload = completion.get(field)
        if not isinstance(payload, Mapping):
            return f"completion requires {field}"
        if record.phase == "discovery" and payload.get("noCandidateReason") is not None:
            return None
        reference = payload.get("sketchReference")
        if not isinstance(reference, Mapping):
            return "completion requires sketchReference"
        if self._programs is None:
            return "workspace programs are not configured"
        try:
            parsed = StrategySketchReference.parse(reference)
            sketch = self._programs.verify_strategy_sketch(
                workspace_id, parsed.document()
            )
            if record.phase == "discovery":
                # The provenance check above already binds attempts to durable
                # submissions and authoritative harness receipts. Sketch evidence
                # must be a subset of those same executed investigations.
                attempts = cast(list[Mapping[str, object]], payload.get("attempts", []))
                investigated = {
                    item.get("jobId") for item in attempts if item.get("jobId")
                }
                prototype = sketch.get("prototype")
                if isinstance(prototype, Mapping):
                    evidence_jobs = {
                        item["jobId"] for item in prototype["searchEvidence"]
                    }
                    candidate_job_id = prototype["candidateJobId"]
                    if (
                        candidate_job_id not in investigated
                        or not evidence_jobs <= investigated
                    ):
                        return "prototype and search evidence must name accepted discovery attempts"
                    job = self._chat.find_tool_job(workspace_id, candidate_job_id)
                    strategy = prototype["strategy"]
                    if job is None or any(
                        job.attribution.get(key) != strategy[key]
                        for key in ("kind", "programId", "version", "gitCommit")
                    ):
                        return "prototype reference must match its retained candidate execution"
                    if job.attribution.get("nullMode") is not None:
                        return "prototype candidate must be an observed-data execution, not a null replicate"
                    if job.attribution.get(
                        "parameterAssignmentSha256"
                    ) != document_digest(prototype["parameters"]):
                        return "prototype parameters must match the tested candidate assignment"
                    result = self._programs.job_result(candidate_job_id)
                    if result.get("state") != "succeeded":
                        return "selected prototype requires a successful candidate execution"
                for assumption in cast(
                    list[Mapping[str, object]], sketch["assumptions"]
                ):
                    evidence = cast(list[Mapping[str, object]], assumption["evidence"])
                    measured = assumption["status"] in ("supported", "refuted")
                    if measured and not evidence:
                        return (
                            "measured sketch assumption requires executed job evidence"
                        )
                    for item in evidence:
                        job_id = item["jobId"]
                        if job_id not in investigated:
                            return "sketch evidence cites a job outside the accepted discovery attempts"
                        if measured:
                            result = self._programs.job_result(cast(str, job_id))
                            if result.get("state") != "succeeded":
                                return "failed or incomplete discovery jobs cannot support a measured sketch assumption"
                return None
            if self._workflow is None or record.workflow_execution_id is None:
                return "sketch handoff requires a workflow execution"
            execution = self._chat.get_execution(
                workspace_id, record.workflow_execution_id
            )
            state = self._state(self._request_from_execution(execution))
            if state.discovery is None or parsed != state.discovery.sketch_reference:
                return "sketchReference does not match accepted discovery"
            assumptions = cast(list[Mapping[str, object]], sketch["assumptions"])
            identities = {item["assumptionId"] for item in assumptions}
            if record.phase == "model_creation":
                assignments = payload.get("assumptionTests")
                if not isinstance(assignments, Mapping) or not assignments:
                    return "model creation requires assumptionTests"
                if set(assignments) - identities:
                    return "assumptionTests names an unknown sketch assumption"
                falsifiers = cast(
                    list[Mapping[str, object]], payload.get("falsifiers", [])
                )
                known = {
                    item.get("falsifierId") or item.get("falsifier_id")
                    for item in falsifiers
                }
                for tests in assignments.values():
                    if not isinstance(tests, list) or not tests or set(tests) - known:
                        return "assumptionTests requires declared falsifier IDs"
            else:
                assessments = payload.get("assumptionAssessments")
                if not isinstance(assessments, list):
                    return "verdict requires assumptionAssessments"
                assessed = [
                    item.get("assumptionId")
                    for item in assessments
                    if isinstance(item, Mapping)
                ]
                if len(assessed) != len(set(assessed)) or set(assessed) != identities:
                    return "verdict must assess each sketch assumption exactly once, including untested assumptions"
                trials = {item.test_id: item for item in state.trials}
                assignments = (
                    {} if state.draft is None else state.draft.assumption_tests
                )
                for item in assessments:
                    test_ids = item.get("testIds", [])
                    if set(test_ids) - trials.keys():
                        return "assumption assessment cites a test that did not execute"
                    allowed = set(assignments.get(item["assumptionId"], ()))
                    if any(
                        trials[test].falsifier_id not in allowed for test in test_ids
                    ):
                        return "assumption assessment cites another assumption's test"
                    if item.get("status") in ("supported", "refuted") and not test_ids:
                        return (
                            "measured assumption assessment requires executed testIds"
                        )
                    if item.get("status") in ("supported", "refuted") and any(
                        trials[test].observed is None
                        or trials[test].run_state != "succeeded"
                        for test in test_ids
                    ):
                        return "failed or undefined trials cannot support a measured assumption assessment"
            return None
        except (ValueError, OSError, KeyError, TypeError) as error:
            return str(error)

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
                self._request_from_execution(execution),
                intent_epoch=epoch,
            )
            state = self._state(request)
            try:
                workflow.check_phase_executor(
                    request, current.sub_agent_id, state.phase.value
                )
            except PhaseCompletionRejected as error:
                return str(error)
        return None

    def record_delegated_submission(
        self,
        parent: Mapping[str, Any],
        job_id: str,
        tool_name: str,
        attribution: Mapping[str, object],
    ) -> None:
        """Record child study exposure in discovery before the child dispatches it."""
        job = self._chat.find_tool_job(parent["workspaceId"], job_id)
        if job is None:
            raise WorkflowError(
                "delegated study must retain its job before recording exposure"
            )
        record = self._store.get_sub_agent(job.workspace_id, parent["subAgentId"])
        execution = self._chat.get_execution(job.workspace_id, parent["executionId"])
        if (
            record is None
            or not record.phase_executor
            or record.phase != "discovery"
            or record.phase_attempt_id != parent["phaseAttemptId"]
            or record.workflow_execution_id != execution.execution_id
            or execution.phase != "discovery"
            or execution.state in TERMINAL_EXECUTION_STATES
        ):
            raise WorkflowError(
                "delegated study's discovery attempt is no longer active"
            )
        request = self._request_from_execution(execution)
        record_submission_exposure(
            self._activities(request),
            request,
            phase="discovery",
            job_id=job_id,
            tool_name=tool_name,
            phase_attempt_id=record.phase_attempt_id,
            window_start=_text_or_none(attribution.get("windowStart")),
            window_end=_text_or_none(attribution.get("windowEnd")),
            parameter_sha256=_text_or_none(
                attribution.get("parameterAssignmentSha256")
            ),
        )

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
            self._request_from_execution(execution),
            intent_epoch=self._completion_epoch(completion),
        )
        workflow = self._workflow()
        assert record.phase is not None
        record_completion_attempts(
            self._activities(request), request, record.phase, completion
        )
        return workflow.apply_phase_completion(
            request,
            record.sub_agent_id,
            completion,
            attempt_id=cast(str, record.phase_attempt_id),
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
        request = self._request_from_execution(execution)
        self._workflow().fail_execution(
            request, reason=str(error) or type(error).__name__
        )
