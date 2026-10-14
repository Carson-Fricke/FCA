"""Read-only, deterministic planning for Thesis recovery scenarios.

Recovery planning answers one question per durable fact: given the process that
just restarted, what should the coordinator do next? It never mutates the queue,
the checkpoint store, or Git. Instead it inspects the real
:class:`~thesis_runtime.execution.queue.ExecutionQueue`,
:class:`~thesis_runtime.execution.checkpoints.CheckpointStore`, and Git publication
state and returns a :class:`RecoveryPlan` of typed decisions. The coordinator
applies the plan through the queue/checkpoint/publication APIs that already own
the fenced mutations.

Each :class:`RecoveryScenario` maps to one documented operator procedure in
``.github/runbooks/thesis.md`` and each :class:`RecoveryAction` names the
allowed next step, including the fail-closed outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from thesis.persistence.journal import CommandJournal, JournalEntry
from thesis_runtime.execution.checkpoints import (
    CheckpointCompatibilityError,
    CheckpointError,
    CheckpointStore,
)
from thesis_runtime.execution.queue import ExecutionQueue, Workflow


class RecoveryError(RuntimeError):
    """Recovery planning received state it cannot classify safely."""


class RecoveryScenario(StrEnum):
    """The Phase 6 failure classes with documented recovery behavior."""

    CHECKPOINT_INCOMPATIBLE = "checkpoint_incompatible"
    QUEUE_INTERRUPTED = "queue_interrupted"
    GIT_PUBLICATION_INTERRUPTED = "git_publication_interrupted"
    PARTIAL_TOOL_COMPLETION = "partial_tool_completion"


class RecoveryAction(StrEnum):
    """The next step a recovery decision authorizes."""

    RESUME_FROM_CHECKPOINT = "resume_from_checkpoint"
    MIGRATE_CHECKPOINT = "migrate_checkpoint"
    ENTER_RECOVERY = "enter_recovery"
    REQUEUE_INTERRUPTED = "requeue_interrupted"
    RECORD_RECOVERED_PUBLICATION = "record_recovered_publication"
    REPUBLISH_OUTBOX = "republish_outbox"
    RETAIN_LATE_RESULT = "retain_late_result"
    REQUEUE_PARTIAL_JOB = "requeue_partial_job"
    SUPERSEDE_STALE_RESULT = "supersede_stale_result"
    REQUIRE_OPERATOR = "require_operator"


class PublicationLookup(Protocol):
    """Read-only lookup of a recovered Git publication for a journal entry.

    :class:`~thesis.persistence.git.GitCoordinator` satisfies this protocol;
    tests inject a deterministic fake so classification never needs a repository.
    """

    def find_publication(self, entry: JournalEntry) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One scenario classification and the action it authorizes."""

    scenario: RecoveryScenario
    action: RecoveryAction
    subject: str
    reason: str
    fail_closed: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, RecoveryScenario):
            raise RecoveryError("scenario must be a RecoveryScenario")
        if not isinstance(self.action, RecoveryAction):
            raise RecoveryError("action must be a RecoveryAction")
        if not self.subject:
            raise RecoveryError("a recovery decision requires a subject")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise RecoveryError("a recovery decision requires a reason")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this decision."""
        return {
            "action": self.action.value,
            "evidence": list(self.evidence),
            "failClosed": self.fail_closed,
            "reason": self.reason,
            "scenario": self.scenario.value,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """A deterministic, ordered set of recovery decisions."""

    decisions: tuple[RecoveryDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "decisions", tuple(self.decisions))

    def for_scenario(self, scenario: RecoveryScenario) -> tuple[RecoveryDecision, ...]:
        """Return the decisions recorded for one scenario."""
        return tuple(
            decision for decision in self.decisions if decision.scenario is scenario
        )

    @property
    def actions(self) -> tuple[RecoveryAction, ...]:
        """Return the authorized actions in decision order."""
        return tuple(decision.action for decision in self.decisions)

    @property
    def requires_fail_closed(self) -> bool:
        """Return whether any decision refuses to proceed automatically."""
        return any(decision.fail_closed for decision in self.decisions)

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this plan."""
        return {
            "decisions": [decision.as_dict() for decision in self.decisions],
            "requiresFailClosed": self.requires_fail_closed,
        }


class RecoveryPlanner:
    """Classify restart recovery without mutating durable state."""

    def __init__(
        self,
        *,
        checkpoints: CheckpointStore,
        queue: ExecutionQueue,
        journal: CommandJournal | None = None,
        publications: PublicationLookup | None = None,
        checkpoint_version: int | None = None,
    ) -> None:
        self._checkpoints = checkpoints
        self._queue = queue
        self._journal = journal
        self._publications = publications
        self._checkpoint_version = (
            checkpoints.supported_version
            if checkpoint_version is None
            else checkpoint_version
        )

    @property
    def queue(self) -> ExecutionQueue:
        """Return the injected execution queue projection."""
        return self._queue

    @property
    def checkpoints(self) -> CheckpointStore:
        """Return the injected checkpoint store."""
        return self._checkpoints

    def classify_checkpoint(self, workflow_id: str) -> RecoveryDecision:
        """Classify a workflow's checkpoint compatibility without mutating it."""
        try:
            checkpoint = self._checkpoints.get(workflow_id)
        except CheckpointError as error:
            return RecoveryDecision(
                scenario=RecoveryScenario.CHECKPOINT_INCOMPATIBLE,
                action=RecoveryAction.ENTER_RECOVERY,
                subject=workflow_id,
                reason=f"checkpoint storage error: {error}",
                fail_closed=True,
            )
        if checkpoint is None:
            return RecoveryDecision(
                scenario=RecoveryScenario.CHECKPOINT_INCOMPATIBLE,
                action=RecoveryAction.RESUME_FROM_CHECKPOINT,
                subject=workflow_id,
                reason="no durable checkpoint; resume from workflow start",
            )
        try:
            self._checkpoints.validate(
                checkpoint,
                expected_version=self._checkpoint_version,
                expected_workflow_id=workflow_id,
            )
        except CheckpointCompatibilityError as error:
            return RecoveryDecision(
                scenario=RecoveryScenario.CHECKPOINT_INCOMPATIBLE,
                action=RecoveryAction.MIGRATE_CHECKPOINT,
                subject=workflow_id,
                reason=str(error),
                fail_closed=True,
                evidence=(f"checkpoint_version={checkpoint.checkpoint_version}",),
            )
        return RecoveryDecision(
            scenario=RecoveryScenario.CHECKPOINT_INCOMPATIBLE,
            action=RecoveryAction.RESUME_FROM_CHECKPOINT,
            subject=workflow_id,
            reason="checkpoint is compatible with the active runner",
            evidence=(f"checkpoint_version={checkpoint.checkpoint_version}",),
        )

    def classify_queue(self, workflow_id: str) -> RecoveryDecision:
        """Classify abandoned leases and interrupted workflow state."""
        workflow = self._require_workflow(workflow_id)
        leased = tuple(
            job for job in self._queue.jobs(workflow_id) if job.status == "leased"
        )
        if workflow.status == "running" or leased:
            return RecoveryDecision(
                scenario=RecoveryScenario.QUEUE_INTERRUPTED,
                action=RecoveryAction.REQUEUE_INTERRUPTED,
                subject=workflow_id,
                reason=(
                    f"workflow status={workflow.status!r} with "
                    f"{len(leased)} leased job(s)"
                ),
                evidence=tuple(job.job_id for job in leased),
            )
        return RecoveryDecision(
            scenario=RecoveryScenario.QUEUE_INTERRUPTED,
            action=RecoveryAction.RESUME_FROM_CHECKPOINT,
            subject=workflow_id,
            reason=f"no interrupted lease (status={workflow.status!r})",
        )

    def classify_publication(self) -> RecoveryDecision | None:
        """Classify the oldest pending Git publication, if any."""
        if self._journal is None or self._publications is None:
            return None
        for item in self._journal.outbox():
            entry = self._journal.get_publication(item.publication_id)
            if entry is None:
                raise RecoveryError(
                    f"outbox publication {item.publication_id!r} has no entry"
                )
            commit = self._publications.find_publication(entry)
            if commit is not None:
                return RecoveryDecision(
                    scenario=RecoveryScenario.GIT_PUBLICATION_INTERRUPTED,
                    action=RecoveryAction.RECORD_RECOVERED_PUBLICATION,
                    subject=item.publication_id,
                    reason="Git commit exists without a journal receipt",
                    evidence=(commit,),
                )
            return RecoveryDecision(
                scenario=RecoveryScenario.GIT_PUBLICATION_INTERRUPTED,
                action=RecoveryAction.REPUBLISH_OUTBOX,
                subject=item.publication_id,
                reason="accepted publication is still pending",
            )
        return None

    def classify_partial_tool(self, workflow_id: str) -> RecoveryDecision:
        """Classify incomplete or late tool work for a workflow."""
        workflow = self._require_workflow(workflow_id)
        jobs = self._queue.jobs(workflow_id)
        late = tuple(job for job in jobs if job.late_results)
        if late:
            return RecoveryDecision(
                scenario=RecoveryScenario.PARTIAL_TOOL_COMPLETION,
                action=RecoveryAction.RETAIN_LATE_RESULT,
                subject=workflow_id,
                reason="stale worker results are retained as evidence",
                evidence=tuple(f"{job.job_id}:{len(job.late_results)}" for job in late),
            )
        pending = tuple(job for job in jobs if job.status in {"pending", "leased"})
        if pending and workflow.status in {"queued", "running"}:
            return RecoveryDecision(
                scenario=RecoveryScenario.PARTIAL_TOOL_COMPLETION,
                action=RecoveryAction.REQUEUE_PARTIAL_JOB,
                subject=workflow_id,
                reason="unfinished tool jobs remain for the workflow",
                evidence=tuple(job.job_id for job in pending),
            )
        if any(job.status == "superseded" for job in jobs):
            return RecoveryDecision(
                scenario=RecoveryScenario.PARTIAL_TOOL_COMPLETION,
                action=RecoveryAction.SUPERSEDE_STALE_RESULT,
                subject=workflow_id,
                reason="stale-epoch jobs are superseded and cannot mutate state",
            )
        return RecoveryDecision(
            scenario=RecoveryScenario.PARTIAL_TOOL_COMPLETION,
            action=RecoveryAction.RESUME_FROM_CHECKPOINT,
            subject=workflow_id,
            reason="no partial tool work remains",
        )

    def plan(self) -> RecoveryPlan:
        """Classify every durable workflow and the pending publication."""
        projection = self._queue.storage.load()
        workflows = projection.get("workflows", {})
        if not isinstance(workflows, Mapping):
            raise RecoveryError("execution projection workflows are not an object")
        decisions: list[RecoveryDecision] = []
        for workflow_id in sorted(str(item) for item in workflows):
            decisions.append(self.classify_checkpoint(workflow_id))
            decisions.append(self.classify_queue(workflow_id))
            decisions.append(self.classify_partial_tool(workflow_id))
        publication = self.classify_publication()
        if publication is not None:
            decisions.append(publication)
        return RecoveryPlan(tuple(decisions))

    def _require_workflow(self, workflow_id: str) -> Workflow:
        workflow = self._queue.get_workflow(workflow_id)
        if workflow is None:
            raise RecoveryError(f"unknown workflow: {workflow_id}")
        return workflow


__all__ = [
    "PublicationLookup",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryError",
    "RecoveryPlan",
    "RecoveryPlanner",
    "RecoveryScenario",
]
