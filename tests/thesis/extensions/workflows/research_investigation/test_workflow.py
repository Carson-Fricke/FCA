"""Research branches through real workflow contracts, program tools and delivery."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from azure.cosmos import ContainerProxy
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    FakeRunner,
    _sketch_document,
)
from tests.thesis.workspaces._support import MemoryGitFactory, Repositories
from tests.thesis.workspaces.test_programs import CONFIG, Harness
from tests.thesis_runtime.adapters.cosmos.test_chunks import LimitedContainer, store_for

from thesis.agent.skills import SkillRegistry
from thesis.application.workflows import WorkflowRuntime, build_model_router
from thesis.extensions.tools.registry import build_session_registry
from thesis.extensions.workflows.research_investigation.contracts import (
    CRITIC_PHASES,
    REVIEW_PHASES,
)
from thesis.integrations.mcp import HarnessMcpTransportError, McpToolResult
from thesis.workspaces.programs import WorkspacePrograms
from thesis_runtime.adapters.cosmos.ownership import CosmosOwnershipStore
from thesis_runtime.adapters.cosmos.work import CosmosWorkStore
from thesis_runtime.adapters.providers.invocations import ModelCompletion, ToolCall
from thesis_runtime.adapters.providers.session_adapter import NativeSessionAdapter
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.errors import ProviderUnreachableError
from thesis_runtime.contracts.messages import from_record
from thesis_runtime.contracts.records import AgentInvocation, SubAgentStatus
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.execution.worker import Work
from thesis_runtime.hosting.worker import RuntimeHost, build_worker
from thesis_runtime.subagents.control import ControlToolService
from thesis_runtime.workflows.execution_store import InMemoryExecutionStore

WORKSPACE = "workspace-one"
PROPOSAL = {
    "construction": "Daily context conditions intraday pullback occurrences.",
    "design": "Compare direction-adjusted hypothetical paths across context, using session contrasts.",
    "decision": "Retain or revise the context representation.",
    "contrastingOutcomes": "Useful separation motivates development; inadequate detection motivates repair.",
    "interpretationLimits": "Does not establish strategy profitability or generalization.",
    "repairs": [],
}
CRITIQUE = {
    "assessment": "Useful scoped study.",
    "strengths": "The contrast addresses the decision.",
    "issues": [],
    "verifiedRepairs": [],
}
ADJUDICATION = {
    "reason": "Proceed with scoped uncertainty.",
    "resolutions": [],
    "remainingUncertainty": "Execution and out-of-sample behavior remain unresolved.",
}


class ResearchHarness(Harness):
    """External harness retains immutable admission input even if the ACK is lost."""

    def __init__(self):
        super().__init__()
        self.mode = "success"
        self.results = {}

    def call_tool(self, name, arguments=None):
        arguments = arguments or {}
        if name == "get_job_result":
            return McpToolResult(
                tool=name, is_error=False, structured=self.results[arguments["job_id"]]
            )
        receipt = super().call_tool(name, arguments)
        if name in ("run_study", "run_backtest"):
            config = arguments["config"]
            self.results[config["job_id"]] = {
                "job_id": config["job_id"],
                "state": "succeeded",
                "metrics": {},
                "accepted_input": dict(config),
            }
            if self.mode == "unknown":
                raise HarnessMcpTransportError(
                    "ACK lost", execution_certainty="unknown_outcome"
                )
            if self.mode == "mismatch":
                return McpToolResult(
                    tool=name, is_error=False, structured={"job_id": "wrong-job"}
                )
        return receipt


class ResearchModel:
    """External model script; each response requests real registered tools."""

    streaming = True

    def __init__(self):
        self.responses = []
        self.calls = 0

    def stream(self, *, message_id=None, **kwargs):
        self.calls += 1
        assert self.responses, "unexpected model turn, including a record-only anchor"
        requests = self.responses.pop(0)()
        yield ModelCompletion(
            "",
            "model",
            "provider",
            kwargs.get("role", "primary"),
            0,
            0,
            tool_calls=tuple(
                ToolCall(f"call-{self.calls}-{index}", name, json.dumps(arguments))
                for index, (name, arguments) in enumerate(requests)
            ),
        )


@pytest.fixture
def kit(tmp_path, request):
    git = MemoryGitFactory()
    repositories = Repositories(tmp_path / "workspaces", git)
    harness = ResearchHarness()
    programs = WorkspacePrograms(repositories, harness, git_factory=git)
    reference = programs.sketches.save(WORKSPACE, "momentum", _sketch_document())[
        "reference"
    ]

    def now():
        return datetime(2026, 10, 13, tzinfo=UTC)

    database = (
        LimitedContainer() if getattr(request, "param", None) == "durable" else None
    )
    queue = (
        CosmosWorkStore(cast(ContainerProxy, database))
        if database is not None
        else None
    )
    model = ResearchModel() if queue is not None else None
    chat = ChatService(store_for(database) if database is not None else None, clock=now)
    thread = chat.create_chat(WORKSPACE, title="Research", owner_actor_id="operator")
    trigger = chat.send_message(
        WORKSPACE,
        thread.chat_id,
        actor_id="operator",
        body="Develop a conditional intraday strategy.",
    )
    executions = InMemoryExecutionStore()
    wakes = []
    conversations = Conversations(
        chat=chat,
        spawn_thread=wakes.append,
        work_store=queue,
        provider=NativeSessionAdapter(model) if model is not None else None,
        registry_factory=lambda ctx: registry(ctx),
    )
    runtime = WorkflowRuntime(
        router=build_model_router(
            role_policies={}, model_profiles={}, providers={}, skills=SkillRegistry()
        ),
        runner=FakeRunner([]),
        programs=programs,
        conversations=conversations,
        execution_store=executions,
        clock=now,
    )
    workflow = runtime.build_hypothesis_development(chat=chat)
    request = workflow.launch(
        WORKSPACE,
        thread.chat_id,
        actor_id="operator",
        inputs={"idea": trigger.body},
        trigger_message_id=trigger.message_id,
    )
    discovery_id = workflow.prepare_phase(request)
    router = runtime.bind_session_services(
        chat=chat,
        store=conversations.store,
        activity=conversations.execute_runtime_tool,
        programs=programs,
    )
    research = cast(Any, router).research
    control = ControlToolService(
        conversations=conversations,
        phase_completions=router.apply_completion,
        contract_validator=router.validate_completion,
    )

    def context(node_id):
        node = conversations.store.get_sub_agent(WORKSPACE, node_id)
        assert node is not None and node.workflow_execution_id is not None
        execution = chat.get_execution(WORKSPACE, node.workflow_execution_id)
        return ToolContext(
            WORKSPACE,
            thread.chat_id,
            node_id,
            "operator",
            sub_agent_id=node_id,
            parent_sub_agent_id=node.parent_sub_agent_id,
            workflow_execution_id=node.workflow_execution_id,
            phase_attempt_id=node.phase_attempt_id,
            trigger_message_id=trigger.message_id,
            allowed_tools=node.allowed_tools,
            phase_executor=node.phase_executor,
            intent_epoch=execution.intent_epoch,
        )

    def registry(ctx):
        binding = router.resolve(ctx)
        return build_session_registry(
            context=ctx,
            control=control,
            programs=cast(WorkspacePrograms | None, binding.programs),
            workflow_tools=binding.tools,
            phase_completion_schema=binding.completion_schema,
            data_window=binding.data_window,
            job_retention=binding.job_retention,
        )

    def tool(node_id, name, arguments):
        ctx = context(node_id)
        return registry(ctx).dispatch(name, arguments, context=ctx)

    def node(execution_id, phase=None):
        return next(
            item
            for item in conversations.store.list_sub_agents(WORKSPACE)
            if item.workflow_execution_id == execution_id
            and item.phase_executor
            and (phase is None or item.phase == phase)
            and item.status
            not in (
                SubAgentStatus.COMPLETED,
                SubAgentStatus.FAILED,
                SubAgentStatus.CANCELLED,
            )
        )

    def complete_one(execution_id, phase, target, payload):
        record = node(execution_id, phase)
        field = (
            "adjudication"
            if record.phase == "adjudicate"
            else "critique"
            if record.phase in CRITIC_PHASES
            else record.phase
        )
        outcome = tool(
            record.sub_agent_id,
            "request_phase_completion",
            {"completion": {"target": target, "intentEpoch": 0, field: payload}},
        )
        assert outcome.ok, outcome.failure
        return outcome

    def complete(execution_id, target, payload):
        record = node(execution_id)
        if record.phase in REVIEW_PHASES.values():
            assert target == "adjudicate"
            for phase in REVIEW_PHASES.values():
                report = payload if phase == "implementation_critique" else CRITIQUE
                outcome = complete_one(execution_id, phase, "collect_critiques", report)
            return outcome
        return complete_one(execution_id, record.phase, target, payload)

    windows = {
        "development": {"start": "2024-01-01T00:00:00Z", "end": "2024-02-01T00:00:00Z"},
        "forward": {"start": "2024-02-01T00:00:00Z", "end": "2024-03-01T00:00:00Z"},
    }
    assert tool(discovery_id, "set_research_window", windows).ok
    brief = {
        "direction": "Develop conditional pullback geometry.",
        "question": "Does context distinguish pullback outcomes?",
        "decision": "Retain or revise context.",
        "constraints": "Completed daily bars only; no forward data.",
        "sketchReference": reference,
        "evidence": [],
        "revisionBudget": 1,
        "executionBudget": 1,
    }

    def launch(**changes):
        result = tool(
            discovery_id,
            "start_workflow",
            {"workflow": "research-investigation", "input": {**brief, **changes}},
        )
        assert result.ok, result.failure
        return result.structured["executionId"]

    return SimpleNamespace(**locals())


@pytest.mark.parametrize("kit", ["durable"], indirect=True)
def test_durable_research_phases_read_and_complete_without_turning_workflow_node(kit):
    branch = kit.launch()
    workflow_node = kit.conversations.lifecycle.workflow_node(WORKSPACE, branch)
    assert workflow_node is not None
    worker = build_worker(
        RuntimeHost(kit.chat, kit.conversations, kit.research.workflows),
        kit.queue,
        CosmosOwnershipStore(cast(ContainerProxy, kit.database)),
        kit.executions,
    )
    # Already-retained or explicitly queued work cannot turn a record anchor.
    stale = Work(
        WORKSPACE,
        "stale-record-turn",
        f"turn:{kit.thread.chat_id}:{workflow_node.sub_agent_id}",
        "conversation",
        {"chat_id": kit.thread.chat_id, "anchor_id": workflow_node.sub_agent_id},
    )
    kit.queue.enqueue(stale)
    assert worker.tick((kit.queue.get(WORKSPACE, stale.key),)) == 1
    assert kit.model.calls == 0
    assert (
        kit.conversations.store.get_sub_agent(
            WORKSPACE, workflow_node.sub_agent_id
        ).status
        is SubAgentStatus.PENDING
    )
    stages = [
        ("proposal", "critique", PROPOSAL),
        *((phase, "collect_critiques", CRITIQUE) for phase in REVIEW_PHASES.values()),
        ("adjudicate", "investigation", ADJUDICATION),
        (
            "investigation",
            "critique",
            {
                "interpretation": "Execution is blocked; the direction remains unresolved.",
                "limitations": "No study was dispatched, so no empirical verdict is possible.",
                "attempts": [],
                "evidence": [],
            },
        ),
        *((phase, "collect_critiques", CRITIQUE) for phase in REVIEW_PHASES.values()),
        ("adjudicate", "completed", ADJUDICATION),
    ]
    for phase, target, payload in stages:
        executor = kit.node(branch, phase)
        assert executor.phase == phase
        field = (
            "adjudication"
            if phase == "adjudicate"
            else "critique"
            if phase in CRITIC_PHASES
            else phase
        )
        completion = {"target": target, "intentEpoch": 0, field: payload}
        kit.model.responses = [
            lambda: [
                (
                    "read_strategy_sketch",
                    {"sketch_id": "momentum", "version": kit.reference["version"]},
                ),
                ("read_sdk_reference", {"topic": "building_blocks"}),
            ],
            lambda completion=completion: [
                ("request_phase_completion", {"completion": completion})
            ],
        ]
        work = next(
            work
            for work in kit.queue.ready()
            if work.payload.get("anchor_id") == executor.sub_agent_id
        )
        assert worker.tick((work,)) == 1
        assert kit.queue.get(WORKSPACE, work.key).state == "completed"
        assert not kit.model.responses
        if kit.chat.get_execution(WORKSPACE, branch).active_phases == (
            "collect_critiques",
        ):
            join_work = next(
                item
                for item in kit.queue.ready()
                if item.payload.get("phase") == "collect_critiques"
            )
            assert worker.tick((join_work,)) == 1
        active = kit.chat.get_execution(WORKSPACE, branch).active_phases
        if target == "critique":
            assert set(active) == set(REVIEW_PHASES.values())
        elif target == "collect_critiques":
            if phase == "alignment_critique":
                assert active == ("adjudicate",)
            else:
                assert "adjudicate" not in active
        else:
            assert active == (target,)
        owner = kit.conversations.store.get_sub_agent(
            WORKSPACE, workflow_node.sub_agent_id
        )
        assert owner.status is (
            SubAgentStatus.COMPLETED
            if target == "completed"
            else SubAgentStatus.PENDING
        )
        assert not any(
            work.payload.get("anchor_id") == workflow_node.sub_agent_id
            for work in kit.queue.ready()
        )

    assert not kit.chat.persistence.list_conversation(
        AgentInvocation,
        WORKSPACE,
        kit.thread.chat_id,
        conversation_id=workflow_node.sub_agent_id,
    )
    outcomes = [
        row
        for row in kit.conversations.get(WORKSPACE, kit.discovery_id).transcript()
        if row.data.get("kind") == "workflow_outcome"
    ]
    assert len(outcomes) == 1
    assert kit.model.calls == 2 * len(stages)


def test_large_handoffs_are_paged_and_repairs_survive_phase_reentry(kit):
    branch = kit.launch(executionBudget=4)
    sibling = kit.launch()
    proposer = kit.node(branch).sub_agent_id
    proposal = {**PROPOSAL, "design": "measured construction " * 500}
    kit.complete(branch, "critique", proposal)
    critic = kit.node(branch).sub_agent_id
    issue = {
        "issueId": "implementation:missing-wavelet-comparison",
        "concern": "The wavelet feature was omitted. " * 120,
        "consequence": "The composed construction remains untested. " * 80,
        "evidence": "The proposal omits the wavelet comparison named in its brief.",
        "repair": "Measure the feature interaction or narrow the conclusion. " * 60,
    }
    kit.complete(branch, "adjudicate", {**CRITIQUE, "issues": [issue]})
    kit.complete(
        branch,
        "proposal",
        {
            **ADJUDICATION,
            "resolutions": [
                {
                    "issueId": issue["issueId"],
                    "disposition": "repair",
                    "reason": "The proposed conclusion exceeds the measurement.",
                }
            ],
        },
    )
    assert kit.node(branch).sub_agent_id == proposer
    repair = {
        "issueId": issue["issueId"],
        "response": "Add the missing interaction comparison.",
    }
    kit.complete(branch, "critique", {**proposal, "repairs": [repair]})
    assert kit.node(branch).sub_agent_id == critic

    def read_all(arguments):
        parts = []
        offset = 0
        while offset is not None:
            response = kit.tool(
                critic,
                "read_research_investigations",
                {**arguments, "offset": offset, "max_chars": 1000},
            )
            assert response.ok, response.failure
            page = response.structured
            assert len(page["content"]) <= 1000
            parts.append(page["content"])
            offset = page["nextOffset"]
        return json.loads("".join(parts))

    history = read_all({})
    assert history["issues"][issue["issueId"]]["status"] == "repair"
    assert history["issues"][issue["issueId"]]["response"] == repair["response"]
    assert read_all({"handoff": 2})["completion"]["critique"]["issues"] == [issue]
    assert read_all({"handoff": 0}) == {
        **kit.brief,
        "executionBudget": 4,
        "operatorRequest": {
            "messageId": kit.trigger.message_id,
            "text": kit.trigger.body,
        },
    }
    assert not kit.tool(
        critic, "read_research_investigations", {"execution_id": sibling}
    ).ok
    kit.complete(
        branch,
        "adjudicate",
        {
            **CRITIQUE,
            "verifiedRepairs": [
                {
                    "issueId": issue["issueId"],
                    "evidence": "Re-read the revised design: the requested comparison is now present.",
                }
            ],
        },
    )
    kit.complete(branch, "investigation", ADJUDICATION)
    investigation = {
        "interpretation": "The specified construction remains unresolved. " * 250,
        "limitations": "Sparse support cannot refute the omitted interaction. " * 70,
        "attempts": [],
        "evidence": [],
    }
    kit.complete(branch, "critique", investigation)
    assert kit.node(branch).sub_agent_id == critic
    investigation_handoff = next(
        item.sequence
        for item in reversed(kit.executions.get(branch).transitions)
        if item.from_phase == "investigation"
    )
    assert (
        read_all({"handoff": investigation_handoff})["completion"]["investigation"]
        == investigation
    )
    assert read_all({})["issues"][issue["issueId"]]["status"] == "verified"
    seeds = [
        from_record(row)
        for row in kit.conversations.get(WORKSPACE, critic).transcript()
        if from_record(row).content.source == "agent_prompt"
    ]
    for seed in seeds:
        assert len(seed.content.text) < 32768
        assert seed.content.result is not None
        assert seed.content.result["text"] == ""
        assert seed.content.result["summary"] != seed.content.text


def test_independent_specialists_preserve_findings_and_narrow_repairs_stay_focused(kit):
    branch = kit.launch()
    kit.complete(branch, "critique", PROPOSAL)
    reviewers = {name: kit.node(branch, phase) for name, phase in REVIEW_PHASES.items()}
    assert len({node.sub_agent_id for node in reviewers.values()}) == 3
    issues = {}
    for name, reviewer in reviewers.items():
        brief = kit.tool(
            reviewer.sub_agent_id, "read_research_investigations", {"handoff": 0}
        )
        original = json.loads(brief.structured["content"])["operatorRequest"]
        assert original == {
            "messageId": kit.trigger.message_id,
            "text": kit.trigger.body,
        }
        assert not kit.tool(
            reviewer.sub_agent_id, "run_backtest", {"program_id": "x"}
        ).ok
        issue = {
            "issueId": f"{name}:boundary",
            "concern": "Boundary claim is false.",
            "evidence": f"{name} traced the equality case against the accepted source.",
            "consequence": "The proposed conclusion is unsupported.",
            "repair": "Correct this boundary claim and demonstrate the result.",
        }
        issues[name] = issue
        if name == "implementation":
            missing_evidence = {
                key: value for key, value in issue.items() if key != "evidence"
            }
            rejected = kit.tool(
                reviewer.sub_agent_id,
                "request_phase_completion",
                {
                    "completion": {
                        "target": "collect_critiques",
                        "intentEpoch": 0,
                        "critique": {**CRITIQUE, "issues": [missing_evidence]},
                    }
                },
            )
            assert not rejected.ok
        kit.complete_one(
            branch, reviewer.phase, "collect_critiques", {**CRITIQUE, "issues": [issue]}
        )
        if name != "alignment":
            assert (
                "adjudicate"
                not in kit.chat.get_execution(WORKSPACE, branch).active_phases
            )
            # A completed sibling's contract is not part of this reviewer's inputs.
            sibling_read = kit.tool(
                reviewers["alignment"].sub_agent_id,
                "read_research_investigations",
                {"handoff": 2},
            )
            assert not sibling_read.ok

    judge = kit.node(branch, "adjudicate")
    read = kit.tool(
        judge.sub_agent_id, "read_research_investigations", {"max_chars": 8000}
    )
    state = json.loads(read.structured["content"])
    assert set(state["critiques"]) == set(REVIEW_PHASES)
    assert set(state["issues"]) == {issue["issueId"] for issue in issues.values()}
    assert all(item["status"] == "open" for item in state["issues"].values())
    kit.complete(
        branch,
        "proposal",
        {
            **ADJUDICATION,
            "reason": "Implementation boundary needs a narrow calculation check via focused_critique.",
            "resolutions": [
                {
                    "issueId": issues["implementation"]["issueId"],
                    "disposition": "repair",
                    "reason": "Verify the corrected equality case.",
                }
            ],
        },
    )
    kit.complete(
        branch,
        "focused_critique",
        {
            **PROPOSAL,
            "repairs": [
                {
                    "issueId": issues["implementation"]["issueId"],
                    "response": "The corrected boundary calculation is provided.",
                }
            ],
        },
    )
    assert kit.chat.get_execution(WORKSPACE, branch).active_phases == (
        "focused_critique",
    )
    focused = kit.node(branch)
    invalid = kit.tool(
        focused.sub_agent_id,
        "request_phase_completion",
        {
            "completion": {
                "target": "adjudicate",
                "intentEpoch": 0,
                "critique": {
                    **CRITIQUE,
                    "verifiedRepairs": [issues["implementation"]["issueId"]],
                },
            }
        },
    )
    assert not invalid.ok
    proof = "Independently recomputed the equality case: the revised predicate now excludes it."
    kit.complete(
        branch,
        "adjudicate",
        {
            **CRITIQUE,
            "verifiedRepairs": [
                {
                    "issueId": issues["implementation"]["issueId"],
                    "evidence": proof,
                }
            ],
        },
    )
    result = kit.tool(
        kit.discovery_id, "read_research_investigations", {"execution_id": branch}
    ).structured["branches"][0]
    assert (
        result["issues"][issues["implementation"]["issueId"]]["verification"] == proof
    )
    assert result["issues"][issues["experimental"]["issueId"]]["status"] == "open"
    assert all(
        kit.conversations.store.get_sub_agent(WORKSPACE, node.sub_agent_id).status
        is SubAgentStatus.COMPLETED
        for node in reviewers.values()
    )
    kit.complete(branch, "investigation", ADJUDICATION)
    kit.complete(
        branch,
        "critique",
        {
            "interpretation": "A changed construction needs substantive review.",
            "limitations": "No executions; these are source-level findings.",
            "attempts": [],
            "evidence": [],
        },
    )
    kit.complete_one(
        branch,
        "implementation_critique",
        "collect_critiques",
        {
            **CRITIQUE,
            "issues": [issues["implementation"]],
        },
    )
    kit.complete_one(
        branch,
        "experimental_critique",
        "collect_critiques",
        {
            **CRITIQUE,
            "verifiedRepairs": [
                {
                    "issueId": issues["implementation"]["issueId"],
                    "evidence": proof,
                }
            ],
        },
    )
    kit.complete_one(branch, "alignment_critique", "collect_critiques", CRITIQUE)
    conflict = kit.tool(
        kit.discovery_id, "read_research_investigations", {"execution_id": branch}
    ).structured["branches"][0]
    assert conflict["issues"][issues["implementation"]["issueId"]]["status"] == "open"
    assert (
        conflict["critiques"]["experimental"]["verifiedRepairs"][0]["evidence"] == proof
    )


@pytest.mark.parametrize("kind", ["study", "strategy"])
def test_executed_program_read_uses_immutable_job_input_after_program_edit(kit, kind):
    branch = kit.launch()
    kit.complete(branch, "critique", PROPOSAL)
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "investigation", ADJUDICATION)
    investigator = kit.node(branch).sub_agent_id
    authored = kit.tool(
        investigator,
        f"author_{kind}",
        {"program_id": "executed-study", "template": "basic", "configuration": CONFIG},
    )
    assert authored.ok
    assert kit.tool(
        investigator, f"validate_{kind}", {"program_id": "executed-study"}
    ).ok
    submitted = kit.tool(
        investigator,
        "run_study" if kind == "study" else "run_backtest",
        {"program_id": "executed-study"},
    )
    assert submitted.ok
    job_id = submitted.structured["job_id"]
    original = kit.tool(
        investigator, f"read_{kind}", {"program_id": "executed-study", "job_id": job_id}
    )
    assert original.ok
    changed = kit.tool(
        investigator,
        f"update_{kind}",
        {
            "program_id": "executed-study",
            "expected_version": authored.structured["version"],
            "expected": f"use harness_strategy_sdk::{{export_{kind}_program,",
            "replacement": f"// representation repair\nuse harness_strategy_sdk::{{export_{kind}_program,",
        },
    )
    assert changed.ok, changed.failure
    current = kit.tool(investigator, f"read_{kind}", {"program_id": "executed-study"})
    executed = kit.tool(
        investigator, f"read_{kind}", {"program_id": "executed-study", "job_id": job_id}
    )
    assert current.ok and executed.ok
    assert executed.structured == original.structured
    assert current.structured["source"] != executed.structured["source"]
    assert not kit.tool(
        investigator, f"read_{kind}", {"program_id": "wrong-study", "job_id": job_id}
    ).ok


@pytest.mark.parametrize("kit", ["durable"], indirect=True)
def test_provider_recovery_after_study_submission_retains_one_job_and_completes(
    kit, monkeypatch
):
    monkeypatch.setattr("thesis_runtime.sessions.recovery.sleep", lambda _: None)
    branch = kit.launch()
    kit.complete(branch, "critique", PROPOSAL)
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "investigation", ADJUDICATION)
    investigator = kit.node(branch).sub_agent_id

    def job_id():
        jobs = kit.chat.get_execution(WORKSPACE, branch).job_ids
        assert len(jobs) == 1
        return jobs[0]

    def disconnect():
        assert len([call for call in kit.harness.calls if call[0] == "run_study"]) == 1
        raise ProviderUnreachableError("connection lost after submitted study")

    kit.model.responses = [
        lambda: [
            (
                "author_study",
                {
                    "program_id": "recover-study",
                    "template": "basic",
                    "configuration": CONFIG,
                },
            ),
            ("validate_study", {"program_id": "recover-study"}),
        ],
        lambda: [("run_study", {"program_id": "recover-study"})],
        disconnect,
        lambda: [("read_study", {"program_id": "recover-study", "job_id": job_id()})],
        lambda: [
            (
                "request_phase_completion",
                {
                    "completion": {
                        "intentEpoch": 0,
                        "target": "critique",
                        "investigation": {
                            "interpretation": "The executed construction requires independent review.",
                            "limitations": "Development-only exploratory evidence.",
                            "attempts": [
                                {
                                    "jobId": job_id(),
                                    "purpose": "Conditional construction",
                                    "dataTypes": ["bars"],
                                    "window": "development",
                                    "result": "Scoped exploratory finding",
                                    "changed": False,
                                }
                            ],
                            "evidence": [],
                        },
                    }
                },
            )
        ],
    ]
    worker = build_worker(
        RuntimeHost(kit.chat, kit.conversations, kit.research.workflows),
        kit.queue,
        CosmosOwnershipStore(cast(ContainerProxy, kit.database)),
        kit.executions,
    )
    work = next(
        item
        for item in kit.queue.ready()
        if item.payload.get("anchor_id") == investigator
    )
    assert worker.tick((work,)) == 1
    assert not kit.model.responses
    assert set(kit.chat.get_execution(WORKSPACE, branch).active_phases) == set(
        REVIEW_PHASES.values()
    )
    assert len([call for call in kit.harness.calls if call[0] == "run_study"]) == 1
    assert kit.harness.results[job_id()]["accepted_input"]["job_id"] == job_id()
    messages = [
        from_record(row)
        for row in kit.conversations.get(WORKSPACE, investigator).transcript()
    ]
    assert not any(message.content.source == "user" for message in messages)
    assert (
        sum(
            "providerRecovery" in (message.content.result or {}) for message in messages
        )
        == 1
    )
    audits = kit.chat.persistence.list_conversation(
        AgentInvocation, WORKSPACE, kit.thread.chat_id, conversation_id=investigator
    )
    assert (
        sum(
            item.state.value == "failed" and item.kind.value == "model"
            for item in audits
        )
        == 1
    )


def test_parallel_branches_loop_and_return_independently_with_original_scope(kit):
    a = kit.launch()
    b = kit.launch(question="Does setup geometry distinguish occurrences?")
    assert kit.node(a).parent_sub_agent_id != kit.node(b).parent_sub_agent_id
    kit.complete(a, "critique", PROPOSAL)
    kit.complete(a, "adjudicate", CRITIQUE)
    kit.complete(a, "proposal", ADJUDICATION)
    assert kit.node(a).phase == "proposal" and kit.node(b).phase == "proposal"
    kit.complete(b, "critique", PROPOSAL)
    kit.complete(b, "adjudicate", CRITIQUE)
    changed = _sketch_document()
    changed["conditioning"] = "Operator now prioritizes trend age."
    latest = kit.programs.sketches.save(
        WORKSPACE, "momentum", changed, expected_version=kit.reference["version"]
    )["reference"]
    kit.conversations.get(WORKSPACE, kit.discovery_id).send(
        "Prioritize trend age next.", "operator"
    )
    kit.complete(
        b,
        "completed",
        {
            **ADJUDICATION,
            "reason": "Direction requires discovery's decision; no empirical claim.",
        },
    )
    anchor = Anchor(WORKSPACE, kit.thread.chat_id, kit.discovery_id)
    outcomes = [
        item
        for item in kit.conversations.transcript(anchor)
        if item.data.get("kind") == "workflow_outcome"
    ]
    assert len(outcomes) == 1
    result = cast(Any, from_record(outcomes[0]).content.result)["structured"]
    assert (
        result["executionId"] == b
        and result["brief"]["question"] != kit.brief["question"]
    )
    assert result["brief"]["sketchReference"] == kit.reference
    assert result["brief"]["sketchReference"] != latest
    assert kit.node(a).phase == "proposal"
    # Returning a reviewed result is idempotent across version-pinned recovery.
    document = kit.executions.get(b)
    kit.research.resume(document.identity, document.launch_request, None)
    assert (
        len(
            [
                item
                for item in kit.conversations.transcript(anchor)
                if item.data.get("kind") == "workflow_outcome"
            ]
        )
        == 1
    )
    kit.complete(a, "critique", PROPOSAL)
    kit.complete(a, "adjudicate", CRITIQUE)
    judge = kit.node(a)
    rejected = kit.tool(
        judge.sub_agent_id,
        "request_phase_completion",
        {
            "completion": {
                "target": "proposal",
                "intentEpoch": 0,
                "adjudication": ADJUDICATION,
            }
        },
    )
    assert not rejected.ok and kit.node(a).sub_agent_id == judge.sub_agent_id
    kit.complete(a, "completed", ADJUDICATION)
    branches = kit.tool(
        kit.discovery_id, "read_research_investigations", {}
    ).structured["branches"]
    assert {item["executionId"] for item in branches} == {a, b}
    assert all(item["status"] == "completed" for item in branches)


@pytest.mark.parametrize("mode", ["success", "unknown", "mismatch"])
@pytest.mark.parametrize("kind", ["study", "strategy"])
def test_child_retention_reconciliation_and_discovery_evidence_handoff(kit, mode, kind):
    branch = kit.launch()
    kit.complete(branch, "critique", PROPOSAL)
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "investigation", ADJUDICATION)
    investigator = kit.node(branch)
    ctx = kit.context(investigator.sub_agent_id)
    bindings = kit.router.resolve(ctx)
    assert bindings.data_window.label == "development"
    assert not kit.tool(investigator.sub_agent_id, "author_strategy_sketch", {}).ok
    assert not kit.tool(investigator.sub_agent_id, "start_workflow", {}).ok
    assert kit.tool(
        investigator.sub_agent_id,
        f"author_{kind}",
        {"program_id": "branch-study", "template": "basic", "configuration": CONFIG},
    ).ok
    assert kit.tool(
        investigator.sub_agent_id, f"validate_{kind}", {"program_id": "branch-study"}
    ).ok
    kit.harness.mode = mode

    def retained_before_dispatch():
        if kit.harness.calls[-1][0] not in ("run_study", "run_backtest"):
            return
        submitted = kit.harness.calls[-1][1]["config"]["job_id"]
        assert kit.chat.find_tool_job(WORKSPACE, submitted).execution_id == branch
        ledger = kit.executions.get(kit.request.execution_id).phase_results[
            "experiment_ledger"
        ]
        assert any(item["payload"]["jobId"] == submitted for item in ledger["attempts"])

    kit.harness.during = retained_before_dispatch
    run = kit.tool(
        investigator.sub_agent_id,
        "run_study" if kind == "study" else "run_backtest",
        {"program_id": "branch-study"},
    )
    assert run.ok is (mode == "success"), str(run.structured)
    execution = kit.chat.get_execution(WORKSPACE, branch)
    assert len(execution.job_ids) == 1
    job_id = execution.job_ids[0]
    tracking = kit.chat.find_tool_job(WORKSPACE, job_id)
    assert tracking.attribution["parentExecutionId"] == kit.request.execution_id
    assert tracking.attribution["submittedInputSha256"]
    assert kit.harness.results[job_id]["accepted_input"]["job_id"] == job_id
    document = kit.executions.get(branch)
    kit.research.resume(document.identity, document.launch_request, None)
    assert (
        len(
            [
                name
                for name, _ in kit.harness.calls
                if name in ("run_study", "run_backtest")
            ]
        )
        == 1
    )
    attempt = {
        "jobId": job_id,
        "purpose": "Conditional comparison",
        "dataTypes": ["bars"],
        "window": "2024-01-01 to 2024-01-02",
        "result": "Inconclusive scoped finding",
        "changed": False,
    }
    kit.complete(
        branch,
        "critique",
        {
            "interpretation": "Useful question remains unresolved.",
            "limitations": "No evidence of strategy profitability.",
            "attempts": [attempt],
            "evidence": [],
        },
    )
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "completed", ADJUDICATION)
    read = kit.tool(
        kit.discovery_id, "read_research_investigations", {"execution_id": branch}
    )
    assert read.structured["branches"][0]["attempts"] == [attempt]
    # The original discovery contract accepts genuine delegated evidence without
    # changing job identity or admitting unrelated executions.
    completion = {
        "target": "completed",
        "intentEpoch": 0,
        "discovery": {
            "noCandidateReason": "Investigation is inconclusive; the construction remains unresolved.",
            "attempts": [attempt],
            "untouchedData": "Forward data sealed.",
            "limitations": "One scoped study.",
        },
    }
    outcome = kit.tool(
        kit.discovery_id, "request_phase_completion", {"completion": completion}
    )
    assert outcome.ok, outcome.failure


def test_registered_definition_and_launch_scope_use_production_composition(kit):
    from fastapi.testclient import TestClient

    from thesis.application.workflow_launcher import RuntimeWorkflowLauncher
    from thesis.http.application import create_app
    from thesis_runtime.workflows.registry import UnknownWorkflowError
    from thesis_runtime.workflows.spec import WorkflowContext

    launcher = RuntimeWorkflowLauncher(
        kit.runtime,
        kit.chat,
        conversations=kit.conversations,
        research_registration=kit.runtime.research_registration(chat=kit.chat),
    )
    registration = launcher.workflows.resolve("research-investigation")
    with pytest.raises(UnknownWorkflowError):
        launcher.workflows.resolve("research-investigation", "outdated")
    before = len(kit.chat.list_executions(WORKSPACE))
    with pytest.raises(ValueError, match="discovery executor"):
        registration.execute(
            WorkflowContext(
                WORKSPACE,
                kit.thread.chat_id,
                "operator",
                trigger_message_id=kit.trigger.message_id,
            ),
            kit.brief,
        )
    assert len(kit.chat.list_executions(WORKSPACE)) == before
    with TestClient(
        create_app(bearer_token="test", workflow_registry=launcher.workflows)
    ) as client:
        result = client.get(
            "/v1/workflows/research-investigation",
            headers={"Authorization": "Bearer test"},
        )
        assert result.status_code == 200
        assert result.json()["phases"] == [
            "proposal",
            "critique",
            *REVIEW_PHASES.values(),
            "collect_critiques",
            "focused_critique",
            "adjudicate",
            "investigation",
            "completed",
            "failed",
        ]
        assert result.json()["transitions"]["adjudicate"] == [
            "completed",
            "investigation",
            "proposal",
        ]


def test_invalid_launch_scope_windows_and_foreign_evidence_fail_before_dispatch(kit):
    before = len(kit.chat.list_executions(WORKSPACE))
    invalid = kit.tool(
        kit.discovery_id,
        "start_workflow",
        {
            "workflow": "research-investigation",
            "input": {**kit.brief, "workspaceId": "forged"},
        },
    )
    assert not invalid.ok and len(kit.chat.list_executions(WORKSPACE)) == before
    branch = kit.launch()
    kit.complete(branch, "critique", PROPOSAL)
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "investigation", ADJUDICATION)
    investigator = kit.node(branch)
    assert kit.tool(
        investigator.sub_agent_id,
        "author_study",
        {
            "program_id": "future",
            "template": "basic",
            "configuration": {
                **CONFIG,
                "start": "2024-02-01T00:00:00Z",
                "end": "2024-02-02T00:00:00Z",
            },
        },
    ).ok
    assert kit.tool(
        investigator.sub_agent_id, "validate_study", {"program_id": "future"}
    ).ok
    rejected = kit.tool(
        investigator.sub_agent_id, "run_study", {"program_id": "future"}
    )
    assert not rejected.ok
    assert not [name for name, _ in kit.harness.calls if name == "run_study"]
    rejected = kit.tool(
        investigator.sub_agent_id,
        "request_phase_completion",
        {
            "completion": {
                "target": "critique",
                "intentEpoch": 0,
                "investigation": {
                    "interpretation": "Unsupported",
                    "limitations": "Unrelated job",
                    "attempts": [
                        {
                            "jobId": "foreign-job",
                            "purpose": "Invented",
                            "dataTypes": ["bars"],
                            "window": "development",
                            "result": "None",
                            "changed": False,
                        }
                    ],
                    "evidence": [],
                },
            }
        },
    )
    assert (
        not rejected.ok and kit.node(branch).sub_agent_id == investigator.sub_agent_id
    )


def test_training_search_nulls_and_prototype_handoff_retain_exact_policy(kit):
    branch = kit.launch(executionBudget=4)
    kit.complete(branch, "critique", PROPOSAL)
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "investigation", ADJUDICATION)
    investigator = kit.node(branch).sub_agent_id
    authored = kit.tool(
        investigator,
        "author_strategy",
        {
            "program_id": "training-policy",
            "template": "basic",
            "configuration": CONFIG,
            "parameter_declaration": {
                "lookback": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 30,
                }
            },
        },
    )
    assert authored.ok, authored.failure
    assert kit.tool(
        investigator, "validate_strategy", {"program_id": "training-policy"}
    ).ok
    batch = kit.tool(
        investigator,
        "run_backtest_batch",
        {
            "program_id": "training-policy",
            "assignments": [{"lookback": n} for n in (5, 10, 20)],
        },
    )
    assert batch.ok, batch.failure
    candidate_job = batch.structured["points"][1]["jobId"]
    null = kit.tool(
        investigator,
        "run_null_batch",
        {
            "program_id": "training-policy",
            "parameters": {"lookback": 10},
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [7],
        },
    )
    assert null.ok, null.structured
    jobs = kit.chat.get_execution(WORKSPACE, branch).job_ids
    assert len(jobs) == 4
    for job_id in jobs:
        accepted = kit.harness.results[job_id]["accepted_input"]
        assert accepted["shell"] == "backtest"
        assert accepted["start"] >= kit.windows["development"]["start"]
        assert accepted["end"] <= kit.windows["development"]["end"]
        assert (
            kit.chat.find_tool_job(WORKSPACE, job_id).attribution["parentExecutionId"]
            == kit.request.execution_id
        )
    assert kit.harness.results[jobs[-1]]["accepted_input"]["null"]["seed"] == 7
    before = len(kit.harness.calls)
    exhausted = kit.tool(
        investigator, "run_backtest", {"program_id": "training-policy"}
    )
    assert not exhausted.ok
    assert len(kit.harness.calls) == before
    attempts = [
        {
            "jobId": job_id,
            "purpose": "Training search or fixed-policy null",
            "dataTypes": ["bars"],
            "window": "development",
            "result": "Conditional training evidence",
            "changed": False,
        }
        for job_id in jobs
    ]
    kit.complete(
        branch,
        "critique",
        {
            "interpretation": "Selected lookback=10; null is conditional on selection.",
            "limitations": "Search adjustment remains unresolved.",
            "attempts": attempts,
            "evidence": [],
        },
    )
    critic = kit.node(branch).sub_agent_id
    executed = kit.tool(
        critic,
        "read_strategy",
        {"program_id": "training-policy", "job_id": candidate_job},
    )
    assert executed.ok, executed.failure
    assert not kit.tool(critic, "run_backtest", {"program_id": "training-policy"}).ok
    kit.complete(branch, "adjudicate", CRITIQUE)
    kit.complete(branch, "completed", ADJUDICATION)
    job = kit.chat.find_tool_job(WORKSPACE, candidate_job)
    reference = {
        key: job.attribution[key]
        for key in ("kind", "programId", "version", "gitCommit")
    }
    sketch = _sketch_document()
    sketch["prototype"] = {
        "strategy": reference,
        "parameters": {"lookback": 10},
        "candidateJobId": candidate_job,
        "searchEvidence": [{"jobId": j, "artifactName": "results.json"} for j in jobs],
    }
    saved = kit.programs.sketches.save(
        WORKSPACE, "momentum", sketch, expected_version=kit.reference["version"]
    )["reference"]
    completion = {
        "target": "model_creation",
        "intentEpoch": 0,
        "discovery": {
            "sketchReference": saved,
            "attempts": attempts,
            "untouchedData": "Forward sealed",
            "limitations": "Conditional null only",
        },
    }
    # A forged selected assignment cannot silently become the downstream policy.
    sketch["prototype"]["parameters"] = {"lookback": 20}
    forged = kit.programs.sketches.save(
        WORKSPACE, "momentum", sketch, expected_version=saved["version"]
    )["reference"]
    completion["discovery"]["sketchReference"] = forged
    rejected = kit.tool(
        kit.discovery_id, "request_phase_completion", {"completion": completion}
    )
    assert not rejected.ok
    completion["discovery"]["sketchReference"] = saved
    accepted = kit.tool(
        kit.discovery_id, "request_phase_completion", {"completion": completion}
    )
    assert accepted.ok, accepted.failure
    current = kit.programs.verify_strategy_sketch(WORKSPACE, saved)
    assert current["prototype"]["parameters"] == {"lookback": 10}
    assert current["prototype"]["strategy"] == reference


def test_invalid_output_selection_and_indicator_ids_fail_before_external_work(kit):
    before = len(kit.harness.calls)
    selection = kit.tool(
        kit.discovery_id,
        "read_study_output",
        {"job_id": "job-any", "output_name": "summary"},
    )
    assert not selection.ok and selection.failure["kind"] == "invalid_arguments"
    assert "instance_id" in str(selection.failure)
    indicator = kit.tool(
        kit.discovery_id,
        "author_indicator",
        {"program_id": "wavelet-feature", "template": "basic", "configuration": CONFIG},
    )
    assert not indicator.ok and indicator.failure["kind"] == "invalid_arguments"
    assert "program_id" in str(indicator.failure)
    assert len(kit.harness.calls) == before


def test_discovery_itself_can_search_and_compare_conditional_null_plans(kit):
    node = kit.discovery_id
    authored = kit.tool(
        node,
        "author_strategy",
        {
            "program_id": "root-policy",
            "template": "basic",
            "configuration": CONFIG,
            "parameter_declaration": {"lookback": {"type": "integer", "default": 5}},
        },
    )
    assert authored.ok, authored.failure
    assert kit.tool(node, "validate_strategy", {"program_id": "root-policy"}).ok
    batch = kit.tool(
        node,
        "run_backtest_batch",
        {
            "program_id": "root-policy",
            "assignments": [{"lookback": 5}, {"lookback": 10}],
        },
    )
    assert batch.ok, batch.failure
    for lookback in (5, 10):
        null = kit.tool(
            node,
            "run_null_batch",
            {
                "program_id": "root-policy",
                "parameters": {"lookback": lookback},
                "mode": "permutation",
                "block_size": 1,
                "seeds": [3],
            },
        )
        assert null.ok, null.structured
    jobs = kit.chat.get_execution(WORKSPACE, kit.request.execution_id).job_ids
    assert len(jobs) == 4
    for job_id in jobs:
        accepted = kit.harness.results[job_id]["accepted_input"]
        assert accepted["start"] >= kit.windows["development"]["start"]
        assert accepted["end"] <= kit.windows["development"]["end"]
        assert (
            kit.chat.find_tool_job(WORKSPACE, job_id).attribution["phase"]
            == "discovery"
        )
