"""Trusted branch launch, retained research jobs and canonical asynchronous results."""

from __future__ import annotations

import hashlib
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
from thesis_runtime.contracts.messages import from_record
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
from thesis_runtime.tools.reading import DEFAULT_READ_TOKENS, MAX_READ_TOKENS, text_page
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution
from thesis_runtime.tools.workflows import workflow_registered_tools
from thesis_runtime.workflows.execution_store import ExecutionIdentity, ExecutionStore
from thesis_runtime.workflows.extensions import RunRequest
from thesis_runtime.workflows.registry import WorkflowRegistration, WorkflowRegistry
from thesis_runtime.workflows.spec import WorkflowContext
from thesis_runtime.workflows.workflow import WorkflowPorts

from .contracts import (
    CONSTRUCTION_PHASES,
    CRITIC_PHASES,
    LAUNCH_SCHEMA,
    SETTLED_ISSUE_STATUSES,
    WORKFLOW_ID,
)
from .definition import accepted_state, build_research_investigation_workflow

SubmissionObserver = Callable[[Mapping[str, Any], str, str, Mapping[str, object]], None]


def _product_read(
    product: Mapping[str, object],
    execution_id: str | None,
    arguments: Mapping[str, object],
) -> ToolExecution:
    handoff = arguments["handoff"]
    content = json.dumps(product, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(content.encode()).hexdigest()
    if arguments.get("expected_sha256") not in (None, digest):
        raise ValueError(
            "accepted state changed since the previous page; restart the read or select an immutable handoff"
        )
    page = text_page(
        content,
        token_offset=cast(int, arguments.get("token_offset", 0)),
        max_tokens=cast(int, arguments.get("max_tokens", DEFAULT_READ_TOKENS)),
    )
    next_offset = page["nextTokenOffset"]
    return ToolExecution(
        ok=True,
        structured={
            "executionId": execution_id,
            "handoff": handoff,
            "contentSha256": digest,
            **page,
            "nextArguments": {
                "execution_id": execution_id,
                "handoff": handoff,
                "token_offset": next_offset,
                "expected_sha256": digest,
                "max_tokens": arguments.get("max_tokens", DEFAULT_READ_TOKENS),
            }
            if next_offset is not None
            else None,
        },
    )


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
                    "description": (
                        "Read accepted research products, never transcripts. Use handoff=0 for "
                        "the brief, a positive handoff index for an accepted product, or null "
                        "for current state. execution_id=null selects this phase's own branch; "
                        "discovery supplies a child execution_id. Ordinary products are returned "
                        "whole. Large products use cl100k_base token budgets; copy nextArguments "
                        "exactly to continue. Omit token_offset/max_tokens for a normal read."
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["execution_id", "handoff"],
                        "properties": {
                            "execution_id": {
                                "type": ["string", "null"],
                                "minLength": 1,
                                "description": "Branch to read. Use its ID for discovery to select one child; use null for a phase's own branch or discovery's branch list. This argument alone does not select a handoff or advance a page.",
                            },
                            "handoff": {
                                "type": ["integer", "null"],
                                "minimum": 0,
                                "description": "Product selector: 0 is the original brief; a positive integer is the exact accepted handoff index from the seed. Use null for current accepted state (phase) or branch inspection (discovery). Preserve it when paging.",
                            },
                            "expected_sha256": {
                                "type": "string",
                                "pattern": "^[a-f0-9]{64}$",
                            },
                            "token_offset": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "Token cursor into the original selected product; use nextArguments unchanged.",
                            },
                            "max_tokens": {
                                "type": "integer",
                                "minimum": 32,
                                "maximum": MAX_READ_TOKENS,
                                "description": "Read budget in cl100k_base tokens; normally omit to read the complete product.",
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
        selected = arguments["execution_id"]
        handoff = cast(int | None, arguments["handoff"])
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
        if handoff is not None or own_branch:
            if len(requests) != 1:
                raise ValueError(
                    "select exactly one execution_id when reading a handoff"
                )
            request = requests[0]
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
            return _product_read(product, request.execution_id, arguments)
        results = []
        for request in requests:
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
        return _product_read(
            {"branches": results, "count": len(requests)},
            cast(str | None, selected),
            arguments,
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
        if mandate is None:
            raise ValueError(
                "Call set_research_window to declare development and forward "
                "windows first, then retry start_workflow."
            )
        if self.programs is None:
            raise ValueError("workspace programs must be configured before delegation")
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
                    "text": from_record(operator_request).content.text,
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
        state = accepted_state(
            [
                item.as_dict()
                for item in (
                    self.workflow.causal_history(request, phase)
                    if phase is not None
                    else self.executions.get(request.execution_id).transitions
                )
            ]
        )
        state["executions"] = len(
            self.chat.get_execution(request.workspace_id, request.execution_id).job_ids
        )
        return state

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
            resolve_data_window=lambda: mandate.default_window,
            job_retention=self.retain_job
            if record.phase in CONSTRUCTION_PHASES
            else None,
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
            raise ValueError("job retention requires a construction executor")
        request = self.request(context.workspace_id, context.workflow_execution_id)
        record = self.conversations.store.get_sub_agent(
            context.workspace_id, context.sub_agent_id
        )
        phase = record.phase if record is not None else None
        if phase not in CONSTRUCTION_PHASES:
            raise ValueError(
                "only proposal, investigation and adjudicate executors may submit jobs"
            )
        self.workflow.check_phase_executor(request, context.sub_agent_id, phase)
        parent = cast(Mapping[str, Any], request.payload["parent"])
        brief = cast(Mapping[str, Any], request.payload["brief"])
        execution = self.chat.get_execution(context.workspace_id, request.execution_id)
        if len(execution.job_ids) >= brief["executionBudget"]:
            raise ValueError(
                "branch execution budget exhausted; return remaining uncertainty"
            )
        scoped = {
            **attribution,
            "phase": phase,
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
        if record.phase in ("proposal", "adjudicate"):
            product = completion.get(
                "adjudication" if record.phase == "adjudicate" else "proposal", {}
            )
            if any(
                item["issueId"] not in state["issues"]
                for item in product.get("repairs", [])
            ):
                return f"{record.phase} repair references an unknown issueId"
        if record.phase in CRITIC_PHASES:
            critique = completion.get("critique", {})
            if any(
                item["issueId"] not in state["issues"]
                for item in critique.get("verifiedRepairs", [])
            ):
                return "verified repair references an unknown issueId"
            for issue in critique.get("issues", []):
                previous = state["issues"].get(issue["issueId"])
                if (
                    previous is not None
                    and previous["status"] in SETTLED_ISSUE_STATUSES
                    and not issue.get("reopenReason")
                ):
                    return (
                        f"issue {issue['issueId']} is {previous['status']}; "
                        "supply reopenReason with new contradictory evidence or "
                        "preserve the settled finding"
                    )
                prefix = record.phase.removesuffix("_critique") + ":"
                if issue["issueId"] not in state["issues"] and not issue[
                    "issueId"
                ].startswith(prefix):
                    return f"new issueId must start with {prefix}"
        brief = cast(Mapping[str, Any], request.payload["brief"])
        if record.phase == "adjudicate":
            adjudication = completion.get("adjudication", {})
            if any(
                item["issueId"] not in state["issues"]
                for item in adjudication.get("resolutions", [])
            ):
                return "adjudication references an unknown issueId"
            review = adjudication.get("review")
            target = completion.get("target")
            if target == "completed":
                if review is not None:
                    return "completed adjudication requires review=null; there is no next review"
            elif not isinstance(review, Mapping):
                return "adjudication.review must assign the next review target, scope and issueIds, including after delegated repair"
            else:
                if any(
                    issue_id not in state["issues"] for issue_id in review["issueIds"]
                ):
                    return "adjudication.review references an unknown issueId"
                if (
                    target in ("critique", "focused_critique")
                    and review["target"] != target
                ):
                    return (
                        "adjudication.review.target must match the direct review route"
                    )
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
        if record.phase not in CONSTRUCTION_PHASES:
            return None
        investigation = completion.get(
            "adjudication" if record.phase == "adjudicate" else record.phase
        )
        if not isinstance(investigation, Mapping):
            return "construction completion requires its findings"
        attempts = investigation.get("attempts")
        if not isinstance(attempts, list):
            return "construction completion requires attempts"
        reported = [item.get("jobId") for item in attempts if isinstance(item, Mapping)]
        execution = self.chat.get_execution(workspace_id, request.execution_id)
        jobs = [
            self.chat.find_tool_job(workspace_id, job_id)
            for job_id in execution.job_ids
        ]
        retained = {
            job.job_id
            for job in jobs
            if job is not None and job.submission_certainty != "not_started"
        }
        duplicates = sorted(
            {job_id for job_id in reported if reported.count(job_id) > 1}
        )
        unknown = sorted(set(reported) - retained)
        if duplicates or unknown:
            return f"invalid attempt references: duplicateJobIds={duplicates}, unknownOrNotDispatchedJobIds={unknown}; annotate retained branch jobs only. The runtime retains the complete ledger."
        if self.programs is None:
            return "workspace programs are unavailable"
        evidence = investigation.get("evidence", [])
        supplied = brief["evidence"]
        if any(
            item.get("jobId") not in retained and item not in supplied
            for item in evidence
        ):
            return "construction evidence must reference a retained branch job or exact evidence supplied in the brief"
        verified_jobs = set(reported) | {
            item["jobId"] for item in evidence if item["jobId"] in retained
        }
        for job_id in verified_jobs:
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
            "escalations": [
                {"issueId": issue_id, "reason": issue["resolution"]}
                for issue_id, issue in state["issues"].items()
                if issue["status"] == "escalate"
            ],
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
