"""Agent-directed branch specification and JSON extension; no engine policy."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from thesis.experiments.mandate import ResearchMandate, mandate_section
from thesis_runtime.contracts.records import WorkflowDefinition
from thesis_runtime.tools.schema import render_violations, validate_arguments
from thesis_runtime.workflows.builder import WorkflowBuilder
from thesis_runtime.workflows.extensions import RunRequest, StateSnapshot
from thesis_runtime.workflows.phase import (
    DeterministicPhase,
    OneShotPhase,
    TerminalPhase,
)
from thesis_runtime.workflows.workflow import Workflow, WorkflowDoctrine, WorkflowPorts

from .contracts import (
    LAUNCH_SCHEMA,
    REVIEW_PHASES,
    ROUTES,
    VERSION,
    WORKFLOW_ID,
    completion_schema,
)
from .prompts import COMMON, INSTRUCTIONS

READ_TOOLS = (
    "read_research_investigations",
    "search_knowledge",
    "read_knowledge",
    "read_strategy_sketch",
    "read_study",
    "read_strategy",
    "read_indicator",
    "read_sdk_reference",
    "read_study_output",
)
EVIDENCE_TOOLS = ("get_job_status", "get_job_result", "list_artifacts", "read_artifact")
AUTHOR_TOOLS = (
    "author_strategy",
    "update_strategy",
    "validate_strategy",
    "publish_strategy",
    "publish_study",
    "publish_indicator",
    "run_backtest",
    "run_backtest_batch",
    "run_null_batch",
    "author_study",
    "update_study",
    "validate_study",
    "run_study",
    "author_indicator",
    "update_indicator",
    "validate_indicator",
)


def accepted_state(history: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold accepted contracts; a new proposal invalidates its prior reviews."""
    state: dict[str, Any] = {
        "phase": "proposal",
        "revisions": 0,
        "executions": 0,
        "attempts": [],
        "investigations": [],
        "reviewSubject": "proposal",
        "issues": {},
    }
    for transition in history:
        handoff = transition["sequence"]
        source = transition["fromPhase"]
        completion = transition["output"]
        state["phase"] = transition["route"]
        if source == "proposal":
            state["proposal"] = completion["proposal"]
            for repair in completion["proposal"]["repairs"]:
                if repair["issueId"] in state["issues"]:
                    state["issues"][repair["issueId"]]["response"] = repair["response"]
            state.pop("critiques", None)
            state["reviewSubject"] = "proposal"
        elif source == "investigation":
            state["investigations"].append(completion["investigation"])
            state["reviewSubject"] = "investigation"
            state["attempts"].extend(completion["investigation"]["attempts"])
            state["executions"] += len(completion["investigation"]["attempts"])
            state.pop("critiques", None)
        elif source in ("collect_critiques", "focused_critique"):
            reports = (
                completion["critiques"]
                if source == "collect_critiques"
                else {"focused": completion["critique"]}
            )
            state["critiques"] = reports
            open_ids = {
                issue["issueId"]
                for report in reports.values()
                for issue in report["issues"]
            }
            for reviewer, report in reports.items():
                for repair in report["verifiedRepairs"]:
                    if repair["issueId"] in state["issues"]:
                        carried = state["issues"][repair["issueId"]]
                        carried["verification"] = repair["evidence"]
                        # Preserve a current objection for adjudication regardless
                        # of specialist iteration order; collection is not a vote.
                        if repair["issueId"] not in open_ids:
                            carried["status"] = "verified"
                for issue in report["issues"]:
                    state["issues"][issue["issueId"]] = {
                        **state["issues"].get(issue["issueId"], {}),
                        **issue,
                        "reviewer": reviewer,
                        "status": "open",
                        "handoff": handoff,
                    }
        elif source == "adjudicate":
            state["adjudication"] = completion["adjudication"]
            for resolution in completion["adjudication"]["resolutions"]:
                if resolution["issueId"] in state["issues"]:
                    state["issues"][resolution["issueId"]].update(
                        status=resolution["disposition"],
                        resolution=resolution["reason"],
                    )
            if transition["route"] == "proposal":
                state["revisions"] += 1
    return state


def phase_context(
    brief: Mapping[str, Any], history: list[Mapping[str, Any]], state: Mapping[str, Any]
) -> dict[str, Any]:
    """Bound the seed; full accepted products remain addressable by handoff index."""

    def excerpt(value: str, limit: int) -> str:
        # Bound serialized size too: escaped control characters can expand sixfold.
        low, high = 0, min(len(value), limit)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(value[:middle], ensure_ascii=False)) <= limit:
                low = middle
            else:
                high = middle - 1
        return value[:low]

    references = {"brief": 0}
    for transition in history:
        references[transition["fromPhase"]] = transition["sequence"]
    issues = [
        {
            "issueId": issue_id,
            "status": issue["status"],
            "handoff": issue["handoff"],
            "concern": excerpt(issue["concern"], 160),
            "repair": excerpt(issue["repair"], 240),
        }
        for issue_id, issue in state["issues"].items()
        if issue["status"] not in ("verified", "accept")
    ]
    return {
        "phase": state["phase"],
        "reviewSubject": state["reviewSubject"],
        "revisions": state["revisions"],
        "executions": state["executions"],
        "briefExcerpt": {
            key: excerpt(brief[key], limit)
            for key, limit in (
                ("direction", 1200),
                ("question", 800),
                ("decision", 800),
                ("constraints", 1200),
            )
        },
        "sketchReference": brief["sketchReference"],
        "revisionBudget": brief["revisionBudget"],
        "executionBudget": brief["executionBudget"],
        "handoffs": references,
        "activeIssueExcerpts": issues[:16],
        "activeIssueCount": len(issues),
        "jobs": [attempt["jobId"] for attempt in state["attempts"]],
    }


class ResearchExtension:
    """One current request, phase contracts, explicit handoffs and result report."""

    def invoke(
        self, operation: str, payload: Mapping[str, Any]
    ) -> Mapping[str, object]:
        if payload.get("protocolVersion") != 2:
            raise ValueError("unsupported workflow extension protocol")
        if operation == "validate_launch":
            errors = validate_arguments(LAUNCH_SCHEMA, payload["inputs"]["brief"])
            if errors:
                raise ValueError(render_violations(errors))
            if not payload.get("triggerMessageId") or not payload.get(
                "anchorSubAgentId"
            ):
                raise ValueError(
                    "research requires a requesting discovery conversation"
                )
            ResearchMandate.from_dict(payload["inputs"]["mandate"])
            return {}
        if operation == "request":
            execution = payload["execution"]
            return {
                "workspaceId": execution["workspace_id"],
                "chatId": execution["chat_id"],
                "executionId": execution["execution_id"],
                "triggerMessageId": execution["trigger_message_id"],
                "requestedBy": execution["requested_by"],
                "intentEpoch": execution["intent_epoch"],
                **payload["inputs"],
            }
        if operation == "validate":
            errors = validate_arguments(
                completion_schema(payload["phase"]), payload["completion"]
            )
            return {"failure": render_violations(errors) if errors else None}
        if operation == "execute":
            # Pure collection: keep all independent findings, never select a verdict.
            branches = payload["inputs"]
            return {
                "completion": {
                    "target": "adjudicate",
                    "intentEpoch": payload["request"]["intentEpoch"],
                    "critiques": {
                        name: branches[name]["critique"] for name in REVIEW_PHASES
                    },
                }
            }
        request = payload["request"]
        state = accepted_state(payload["history"])
        state["phase"] = payload["phase"]
        if operation == "state":
            return {"phase": state["phase"], "intentEpoch": request["intentEpoch"]}
        if operation == "report":
            return {
                "artifact": state,
                "summary": f"Research {state['phase']}: {request['brief']['question']}",
            }
        if operation == "seed":
            if state["phase"] not in INSTRUCTIONS:
                return {"text": None}
            return {
                "text": "\n\n".join(
                    (
                        COMMON,
                        INSTRUCTIONS[state["phase"]],
                        mandate_section(ResearchMandate.from_dict(request["mandate"])),
                        f"Branch execution ID: {request['executionId']}",
                        "Original operator request (runtime-preserved; branch choices may not replace it):\n"
                        + json.dumps(request["operatorRequest"], ensure_ascii=False),
                        "Current phase context (excerpts; retrieve full accepted handoffs):\n"
                        + json.dumps(
                            phase_context(request["brief"], payload["history"], state),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
            }
        raise ValueError(f"unknown research extension operation: {operation}")


def build_research_investigation_workflow(
    ports: WorkflowPorts | None = None,
) -> Workflow[RunRequest, StateSnapshot, Mapping[str, object]]:
    """Register a separate bounded child workflow using the unchanged host."""
    builder = WorkflowBuilder().workflow(
        WORKFLOW_ID, version=VERSION, title="Research investigation"
    )
    for phase in ROUTES:
        if phase == "collect_critiques":
            builder.phase(
                DeterministicPhase(
                    phase=phase,
                    inputs=tuple(REVIEW_PHASES),
                    contract="execute",
                    replay_safe=True,
                    validate="validate",
                    completion_schema=completion_schema(phase),
                    on_complete="adjudicate",
                )
            )
            continue
        if phase not in INSTRUCTIONS:
            builder.phase(
                TerminalPhase(
                    phase=phase, outcome="failed" if phase == "failed" else "completed"
                )
            )
            continue
        builder.phase(
            OneShotPhase(
                phase=phase,
                seed="seed",
                validate="validate",
                completion_schema=completion_schema(phase),
            )
        )
    builder.routes(ROUTES)
    builder.doctrine(
        WorkflowDoctrine(
            definition=WorkflowDefinition(
                workflow_id=WORKFLOW_ID,
                version=VERSION,
                title="Research investigation",
                phases=tuple(ROUTES),
            ),
            initial_phase="proposal",
            actor="thesis-research",
            active_phases=tuple(INSTRUCTIONS),
            conversational_phases=frozenset(),
            orchestrators={
                phase: (
                    "critique"
                    if phase.endswith("critique") or phase == "adjudicate"
                    else "research",
                    f"Research {phase}",
                )
                for phase in INSTRUCTIONS
            },
            tasks={
                phase: f"Complete the research {phase} against the original discovery brief."
                for phase in INSTRUCTIONS
            },
            activity={phase: f"Research {phase}." for phase in INSTRUCTIONS},
            default_activity="Investigating a discovery direction.",
            mcp_tools={},
            phase_mcp_tools={phase: EVIDENCE_TOOLS for phase in INSTRUCTIONS},
            phase_tools={
                phase: (
                    *READ_TOOLS,
                    *(AUTHOR_TOOLS if phase == "investigation" else ()),
                )
                for phase in INSTRUCTIONS
            },
            extension=ResearchExtension(),
        )
    )
    if ports is not None:
        builder.ports(ports)
    return builder.build()
