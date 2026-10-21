"""Trusted branch launch, retained research jobs and canonical asynchronous results."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, cast

from thesis.experiments.mandate import ResearchMandate, resolve_research_mandate
from thesis.extensions.workflows.bindings import WorkflowSessionBindings
from thesis.knowledge.service import KnowledgeScope
from thesis.research.evidence import document_digest
from thesis.workflows.research_models import ProgramSnapshots
from thesis.workspaces.programs import JobSettlement
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import DatumType, datum_from_result
from thesis_runtime.contracts.records import (
    RecordNotFound,
    SubAgentRecord,
    ToolJobState,
    WorkflowExecutionState,
)
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.subagents.control import PhaseCompletionRejected
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.tools.workflows import workflow_registered_tools
from thesis_runtime.workflows.execution_store import ExecutionIdentity, ExecutionStore
from thesis_runtime.workflows.extensions import RunRequest
from thesis_runtime.workflows.registry import WorkflowRegistration, WorkflowRegistry
from thesis_runtime.workflows.spec import WorkflowContext
from thesis_runtime.workflows.workflow import WorkflowPorts

from .contracts import CRITIC_PHASES, LAUNCH_SCHEMA, WORKFLOW_ID
from .definition import accepted_state, build_research_investigation_workflow

SubmissionObserver = Callable[[Mapping[str, Any], str, str, Mapping[str, object]], None]


class ResearchInvestigationRuntime:
    """Compose one workflow capability without adding a runner or delivery path."""

    def __init__(
        self,
        *,
        ports: WorkflowPorts,
        programs: ProgramSnapshots | None,
        observe_submission: SubmissionObserver,
        knowledge_scope: KnowledgeScope,
    ) -> None:
        if ports.conversations is None or ports.execution_store is None:
            raise ValueError(
                "research investigations require conversations and durable execution storage"
            )
        self.ports = ports
        self.chat: ChatService = ports.chat
        self.conversations: Conversations = ports.conversations
        self.executions: ExecutionStore = ports.execution_store
        self.programs = programs
        self.observe_submission = observe_submission
        self.knowledge_scope = knowledge_scope
        self.workflow = build_research_investigation_workflow(ports)
        self.registration = WorkflowRegistration(
            execute=self.launch,
            resume=self.resume,
            launch_schema=LAUNCH_SCHEMA,
            workflow_factory=lambda: self.workflow,
        )
        self.workflows = WorkflowRegistry()
        self.workflows.register(self.registration)

    def launch_tools(self, context: ToolContext) -> tuple[RegisteredTool, ...]:
        """Advertise only this child workflow to the owning discovery executor."""
        return (
            *workflow_registered_tools(self, context),
            *self.read_tools(),
        )

    def read_tools(self) -> tuple[RegisteredTool, ...]:
        """Read accepted products only; the current phase is scoped to its branch."""
        return (
            RegisteredTool(
                descriptor={
                    "name": "read_research_investigations",
                    "description": "Read accepted research products, never agent transcripts. Discovery may inspect its own branches by execution_id. Research phases may read only their own branch. Set handoff=0 for the full original brief, or a positive handoff index from the seed for that exact accepted completion. Reads with handoff or from a research phase return bounded JSON text with offset/nextOffset; follow pages to read the complete product. Without handoff, a phase reads the current accepted state including the repair ledger. No searches or mutations.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "execution_id": {"type": "string", "minLength": 1},
                            "handoff": {"type": "integer", "minimum": 0},
                            "offset": {"type": "integer", "minimum": 0},
                            "max_chars": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 8000,
                            },
                        },
                    },
                },
                handler=self.read,
                operation="read",
            ),
        )

    def read(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
        selected = arguments.get("execution_id")
        caller = self.conversations.store.get_sub_agent(
            context.workspace_id, context.sub_agent_id or ""
        )
        own_branch = bool(
            caller is not None
            and caller.phase_executor
            and caller.chat_id == context.chat_id
            and caller.workflow_execution_id is not None
            and self.chat.get_execution(
                context.workspace_id, caller.workflow_execution_id
            ).workflow_id
            == WORKFLOW_ID
        )
        if own_branch:
            assert caller is not None
            if selected not in (None, caller.workflow_execution_id):
                raise ValueError("a research phase may read only its own branch")
            selected = caller.workflow_execution_id
            requests = [self.request(context.workspace_id, str(selected))]
        else:
            requests = self._read_requests(context, selected)
        if "handoff" in arguments or own_branch:
            if len(requests) != 1:
                raise ValueError("a paged research read requires one execution_id")
            request = requests[0]
            handoff = cast(int | None, arguments.get("handoff"))
            if handoff == 0:
                product = {
                    **cast(Mapping[str, object], request.payload["brief"]),
                    "operatorRequest": request.payload["operatorRequest"],
                }
            elif handoff is not None:
                transitions = self.executions.get(request.execution_id).transitions
                if handoff > len(transitions):
                    raise ValueError("accepted handoff does not exist")
                if own_branch and caller is not None and caller.phase is not None:
                    allowed = {
                        item.sequence
                        for item in self.workflow.visible_history(request, caller.phase)
                    }
                    if handoff not in allowed:
                        raise ValueError(
                            "accepted handoff is outside this phase's causal inputs"
                        )
                product = transitions[handoff - 1].as_dict()
            else:
                product = self.result(
                    request,
                    phase=caller.phase if own_branch and caller is not None else None,
                )
            content = json.dumps(product, ensure_ascii=False, separators=(",", ":"))
            offset = cast(int, arguments.get("offset", 0))
            end = min(
                len(content), offset + cast(int, arguments.get("max_chars", 6000))
            )
            return ToolExecution(
                ok=True,
                structured={
                    "executionId": request.execution_id,
                    "handoff": handoff,
                    "offset": offset,
                    "nextOffset": end if end < len(content) else None,
                    "totalChars": len(content),
                    "content": content[offset:end],
                },
            )
        results = []
        for request in requests[:20]:
            if selected is not None:
                results.append(self.result(request))
                continue
            brief = cast(Mapping[str, object], request.payload["brief"])
            execution = self.chat.get_execution(
                context.workspace_id, request.execution_id
            )
            results.append(
                {
                    "executionId": request.execution_id,
                    "status": execution.state.value,
                    "activePhases": list(execution.active_phases),
                    "question": brief["question"],
                    "sketchReference": brief["sketchReference"],
                    "jobIds": list(execution.job_ids),
                }
            )
        return ToolExecution(
            ok=True,
            structured={
                "branches": results,
                "count": len(requests),
                "truncated": len(requests) > 20,
            },
        )

    def _read_requests(
        self, context: ToolContext, selected: object
    ) -> list[RunRequest]:
        nodes = [
            node
            for node in self.conversations.store.list_sub_agents(context.workspace_id)
            if node.role == "workflow-node"
            and node.parent_sub_agent_id == context.sub_agent_id
            and node.chat_id == context.chat_id
            and node.workflow_execution_id is not None
        ]
        if selected is not None:
            nodes = [node for node in nodes if node.workflow_execution_id == selected]
            if not nodes:
                raise ValueError("research execution is not a child of this discovery")
        requests = []
        for node in reversed(nodes):
            execution = self.chat.get_execution(
                context.workspace_id, cast(str, node.workflow_execution_id)
            )
            if execution.workflow_id != WORKFLOW_ID:
                continue
            requests.append(self.request(context.workspace_id, execution.execution_id))
        return requests

    def launch(
        self, context: WorkflowContext, inputs: Mapping[str, Any]
    ) -> Mapping[str, object]:
        parent_id = context.anchor_sub_agent_id
        parent = (
            None
            if parent_id is None
            else self.conversations.store.get_sub_agent(context.workspace_id, parent_id)
        )
        if (
            parent is None
            or parent.chat_id != context.chat_id
            or not parent.phase_executor
            or parent.phase != "discovery"
            or parent.workflow_execution_id is None
        ):
            raise ValueError(
                "research investigations must be launched by the discovery executor"
            )
        parent_execution = self.chat.get_execution(
            context.workspace_id, parent.workflow_execution_id
        )
        if (
            parent_execution.state is not WorkflowExecutionState.RUNNING
            or parent_execution.phase != "discovery"
        ):
            raise ValueError("the launching discovery is no longer active")
        mandate = resolve_research_mandate(
            self.executions,
            workspace_id=context.workspace_id,
            execution_id=parent.workflow_execution_id,
        )
        if mandate is None or self.programs is None:
            raise ValueError(
                "discovery must declare research windows and have workspace programs before delegation"
            )
        self.programs.verify_strategy_sketch(
            context.workspace_id, inputs["sketchReference"]
        )
        if parent_execution.trigger_message_id is None:
            raise ValueError("discovery has no original operator request")
        operator_request = self.chat.get_message(
            context.workspace_id, parent_execution.trigger_message_id
        )
        request = self.workflow.launch(
            context.workspace_id,
            context.chat_id,
            actor_id=context.actor_id,
            inputs={
                "brief": dict(inputs),
                "operatorRequest": {
                    "messageId": operator_request.message_id,
                    "text": operator_request.body,
                },
                "mandate": mandate.as_dict(),
                "parent": {
                    "workspaceId": context.workspace_id,
                    "executionId": parent.workflow_execution_id,
                    "subAgentId": parent.sub_agent_id,
                    "phaseAttemptId": parent.phase_attempt_id,
                    "intentEpoch": parent_execution.intent_epoch,
                },
            },
            trigger_message_id=context.trigger_message_id,
            anchor_sub_agent_id=parent.sub_agent_id,
        )

        def start() -> None:
            try:
                self.workflow.prepare_phase(request)
            except Exception as error:
                self.workflow.fail_execution(
                    request, reason=str(error) or type(error).__name__
                )

        self._dispatch(start)
        node = self.conversations.lifecycle.workflow_node(
            context.workspace_id, request.execution_id
        )
        return {
            "executionId": request.execution_id,
            "state": "running",
            "activePhases": list(
                self.chat.get_execution(
                    request.workspace_id, request.execution_id
                ).active_phases
            ),
            "subAgentId": None if node is None else node.sub_agent_id,
            "question": inputs["question"],
            "sketchReference": inputs["sketchReference"],
        }

    def _dispatch(self, action: Callable[[], object]) -> None:
        if self.ports.detach is None:
            action()
        else:

            def execute() -> None:
                action()

            self.ports.detach(execute)

    def request(self, workspace_id: str, execution_id: str) -> RunRequest:
        execution = self.chat.get_execution(workspace_id, execution_id)
        if (
            execution.workflow_id != WORKFLOW_ID
            or execution.workflow_version != self.workflow.version
        ):
            raise ValueError("execution is not a current research investigation")
        return RunRequest.bind(
            execution,
            {
                **self.executions.get(execution_id).launch_request,
                "intentEpoch": execution.intent_epoch,
            },
        )

    def state(self, request: RunRequest, phase: str | None = None) -> dict[str, Any]:
        return accepted_state(
            [
                item.as_dict()
                for item in (
                    self.workflow.causal_history(request, phase)
                    if phase is not None
                    else self.executions.get(request.execution_id).transitions
                )
            ]
        )

    def resume(
        self,
        identity: ExecutionIdentity,
        launch_request: Mapping[str, object],
        work: Mapping[str, object] | None,
    ) -> None:
        if (
            identity.workflow_id != WORKFLOW_ID
            or identity.workflow_version != self.workflow.version
        ):
            raise ValueError("outdated research investigation execution")
        execution = self.chat.get_execution(
            identity.workspace_id, identity.execution_id
        )
        request = RunRequest.bind(
            execution,
            {**launch_request, "intentEpoch": execution.intent_epoch},
        )
        if self.chat.get_execution(
            identity.workspace_id, identity.execution_id
        ).state in (
            WorkflowExecutionState.COMPLETED,
            WorkflowExecutionState.FAILED,
        ):
            self.publish(request)
        elif work is None:
            self.workflow.prepare_phase(request)
        else:
            self.workflow.enter_scheduled_phase(request, work)

    def resolve(self, context: ToolContext) -> WorkflowSessionBindings:
        record = (
            None
            if context.sub_agent_id is None
            else self.conversations.store.get_sub_agent(
                context.workspace_id, context.sub_agent_id
            )
        )
        if (
            record is None
            or not record.phase_executor
            or record.workflow_execution_id != context.workflow_execution_id
            or record.chat_id != context.chat_id
            or record.sub_agent_id != context.conversation_id
        ):
            return WorkflowSessionBindings(knowledge_collections=())
        request = self.request(
            context.workspace_id, cast(str, record.workflow_execution_id)
        )
        mandate = ResearchMandate.from_dict(
            cast(Mapping[str, object], request.payload["mandate"])
        )
        return WorkflowSessionBindings(
            tools=self.read_tools(),
            completion_schema=self.workflow.spec.completion_schemas.get(
                record.phase or ""
            ),
            knowledge_scope=self.knowledge_scope,
            knowledge_collections=(
                "discovery",
                "data-quality",
                "trading-costs",
                "model-cards",
                "inference",
                "validation",
            ),
            programs=self.programs,
            data_window=mandate.default_window,
            job_retention=self.retain_job if record.phase == "investigation" else None,
        )

    def retain_job(
        self,
        context: ToolContext,
        job_id: str,
        tool_name: str,
        attribution: Mapping[str, object],
    ) -> JobSettlement:
        """Retain immutable child attribution and parent exposure before dispatch."""
        if context.workflow_execution_id is None or context.sub_agent_id is None:
            raise ValueError("study retention requires the investigation executor")
        request = self.request(context.workspace_id, context.workflow_execution_id)
        self.workflow.check_phase_executor(
            request, context.sub_agent_id, "investigation"
        )
        parent = cast(Mapping[str, Any], request.payload["parent"])
        brief = cast(Mapping[str, Any], request.payload["brief"])
        execution = self.chat.get_execution(context.workspace_id, request.execution_id)
        if len(execution.job_ids) >= brief["executionBudget"]:
            raise ValueError(
                "branch execution budget exhausted; return remaining uncertainty"
            )
        scoped = {
            **attribution,
            "phase": "investigation",
            "phaseAttemptId": context.phase_attempt_id,
            "parentExecutionId": parent["executionId"],
            "parentSubAgentId": parent["subAgentId"],
            "parentPhaseAttemptId": parent["phaseAttemptId"],
            "sketchId": brief["sketchReference"]["sketchId"],
            "sketchVersion": brief["sketchReference"]["version"],
            "sketchGitCommit": brief["sketchReference"]["gitCommit"],
        }
        null_plan = scoped.pop("nullPlan", None)
        if isinstance(null_plan, Mapping):
            # Tracking attribution is scalar; the full immutable null plan is
            # retained by WorkspacePrograms' submission store before dispatch.
            scoped["nullPlanSha256"] = document_digest(null_plan)
        job = self.chat.submit_tool_job(
            context.workspace_id,
            request.execution_id,
            tool_name=tool_name,
            actor_id="thesis-research",
            job_id=job_id,
            attribution=scoped,
        )
        try:
            self.observe_submission(parent, job_id, tool_name, scoped)
        except Exception as error:
            self.chat.record_tool_result(
                context.workspace_id,
                job_id,
                result={"executionCertainty": "not_started"},
                actor_id="thesis-research",
                success=False,
                error_message=str(error),
            )
            raise
        if job.state is ToolJobState.SUBMITTED:
            self.chat.start_tool_job(
                context.workspace_id, job_id, actor_id="thesis-research"
            )

        def settle(certainty: str, message: str | None) -> None:
            if certainty in ("not_started", "observed_failure"):
                self.chat.record_tool_result(
                    context.workspace_id,
                    job_id,
                    result={"executionCertainty": certainty},
                    actor_id="thesis-research",
                    success=False,
                    error_message=message,
                )

        return settle

    def validate_completion(
        self, workspace_id: str, record: SubAgentRecord, completion: Mapping[str, Any]
    ) -> str | None:
        if (
            not record.phase_executor
            or record.workflow_execution_id is None
            or record.phase is None
        ):
            return "only the owning phase executor may complete research"
        request = self.request(workspace_id, record.workflow_execution_id)
        try:
            self.workflow.check_phase_executor(
                request, record.sub_agent_id, record.phase
            )
        except PhaseCompletionRejected as error:
            return str(error)
        state = self.state(request, record.phase)
        if record.phase == "proposal":
            proposal = completion.get("proposal", {})
            if any(
                item["issueId"] not in state["issues"]
                for item in proposal.get("repairs", [])
            ):
                return "proposal repair references an unknown issueId"
        if record.phase in CRITIC_PHASES:
            critique = completion.get("critique", {})
            if any(
                item["issueId"] not in state["issues"]
                for item in critique.get("verifiedRepairs", [])
            ):
                return "verified repair references an unknown issueId"
            for issue in critique.get("issues", []):
                prefix = record.phase.removesuffix("_critique") + ":"
                if issue["issueId"] not in state["issues"] and not issue[
                    "issueId"
                ].startswith(prefix):
                    return f"new issueId must start with {prefix}"
        brief = cast(Mapping[str, Any], request.payload["brief"])
        if record.phase == "adjudicate":
            if any(
                item["issueId"] not in state["issues"]
                for item in completion.get("adjudication", {}).get("resolutions", [])
            ):
                return "adjudication references an unknown issueId"
            if (
                completion.get("target") == "proposal"
                and state["revisions"] >= brief["revisionBudget"]
            ):
                return "revision budget exhausted; return the scoped findings and unresolved issues"
            execution = self.chat.get_execution(workspace_id, request.execution_id)
            if completion.get("target") == "investigation" and (
                len(execution.job_ids) >= brief["executionBudget"]
                or len(state["investigations"]) >= brief["executionBudget"]
            ):
                return "execution budget exhausted; return remaining uncertainty"
        if record.phase != "investigation":
            return None
        investigation = completion.get("investigation")
        if not isinstance(investigation, Mapping):
            return "investigation completion requires its findings"
        attempts = investigation.get("attempts")
        if not isinstance(attempts, list):
            return "investigation requires attempts"
        reported = [item.get("jobId") for item in attempts if isinstance(item, Mapping)]
        execution = self.chat.get_execution(workspace_id, request.execution_id)
        jobs = [
            self.chat.find_tool_job(workspace_id, job_id)
            for job_id in execution.job_ids
        ]
        expected = {
            job.job_id
            for job in jobs
            if job is not None
            and job.attribution.get("phaseAttemptId") == record.phase_attempt_id
            and job.submission_certainty != "not_started"
        }
        if len(reported) != len(set(reported)) or set(reported) != expected:
            return "investigation must report exactly its retained dispatched jobs, including failed attempts"
        if self.programs is None:
            return "workspace programs are unavailable"
        for job_id in reported:
            job = self.chat.find_tool_job(workspace_id, str(job_id))
            try:
                result = self.programs.job_result(str(job_id))
            except Exception as error:
                return f"reconcile retained job {job_id}: {error}"
            if (
                job is None
                or result.get("job_id") != job_id
                or not isinstance(result.get("accepted_input"), Mapping)
                or document_digest(cast(Mapping[str, object], result["accepted_input"]))
                != job.attribution.get("submittedInputSha256")
            ):
                return f"job {job_id} does not match its immutable retained submission"
        evidence = investigation.get("evidence", [])
        if any(item.get("jobId") not in expected for item in evidence):
            return "investigation evidence names a job outside its executed attempts"
        return None

    def apply_completion(
        self,
        workspace_id: str,
        record: SubAgentRecord,
        completion: Mapping[str, object],
    ) -> Mapping[str, object]:
        if record.workflow_execution_id is None:
            raise PhaseCompletionRejected("research completion requires an execution")
        request = self.request(workspace_id, record.workflow_execution_id)
        request = replace(request, intent_epoch=cast(int, completion["intentEpoch"]))
        result = self.workflow.apply_phase_completion(
            request,
            record.sub_agent_id,
            completion,
            attempt_id=cast(str, record.phase_attempt_id),
        )
        if result["phase"] == "completed":
            self.publish(request)
        return result

    def result(
        self, request: RunRequest, phase: str | None = None
    ) -> dict[str, object]:
        """Read accepted products and retained job references, never transcripts."""
        execution = self.chat.get_execution(request.workspace_id, request.execution_id)
        jobs = [
            self.chat.find_tool_job(request.workspace_id, job_id)
            for job_id in execution.job_ids
        ]
        return {
            "executionId": request.execution_id,
            "workflowId": WORKFLOW_ID,
            "status": execution.state.value,
            "brief": request.payload["brief"],
            "operatorRequest": request.payload["operatorRequest"],
            "parent": request.payload["parent"],
            "jobs": [
                {
                    "jobId": job.job_id,
                    "attribution": dict(job.attribution),
                    "submissionCertainty": job.submission_certainty,
                }
                for job in jobs
                if job is not None
            ],
            **self.state(request, phase),
            "activePhases": list(execution.active_phases),
        }

    def publish(self, request: RunRequest) -> None:
        """Deliver completed accepted findings once; the host delivers faults."""
        execution = self.chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state is not WorkflowExecutionState.COMPLETED:
            return
        state = self.state(request)
        handoffs = self.executions.get(request.execution_id).transitions
        adjudication = cast(Mapping[str, Any], state.get("adjudication", {}))
        # A completion wakes discovery with references and a scoped finding,
        # not every proposal/review/job body from a potentially large search.
        result = {
            "executionId": request.execution_id,
            "workflowId": WORKFLOW_ID,
            "status": execution.state.value,
            "brief": request.payload["brief"],
            "summary": adjudication.get("reason"),
            "remainingUncertainty": adjudication.get("remainingUncertainty"),
            "jobIds": list(execution.job_ids),
            "handoffs": {
                transition.from_phase: index
                for index, transition in enumerate(handoffs, 1)
            },
        }
        parent = cast(Mapping[str, Any], request.payload["parent"])
        datum = datum_from_result(
            DatumType.WORKFLOW_OUTCOME.value,
            ok=execution.state is WorkflowExecutionState.COMPLETED,
            structured=result,
            text=f"Research investigation {request.execution_id} returned: {result['status']}.",
        )
        self.conversations.deliver(
            Anchor(
                request.workspace_id, request.chat_id, sub_agent_id=parent["subAgentId"]
            ),
            datum,
            execution_id=request.execution_id,
            idempotency_key=f"research-outcome:{request.execution_id}",
            data={"kind": "workflow_outcome", "executionId": request.execution_id},
        )

    def fail(self, record: SubAgentRecord, error: Exception) -> None:
        if not record.phase_executor or record.workflow_execution_id is None:
            return
        try:
            request = self.request(record.workspace_id, record.workflow_execution_id)
        except RecordNotFound:
            return
        self.workflow.fail_execution(request, reason=str(error) or type(error).__name__)
