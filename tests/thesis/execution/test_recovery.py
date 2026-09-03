from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis.execution.checkpoints import (
    CheckpointCompatibilityError,
    CheckpointStore,
    MemoryCheckpointStorage,
)
from thesis.execution.queue import (
    ExecutionQueue,
    FencingError,
    IdempotencyConflict,
    JsonExecutionStorage,
    MemoryExecutionStorage,
)
from thesis.execution.recovery import WorkflowRecovery


class _TestClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Tokens:
    def __init__(self) -> None:
        self.number = 0

    def __call__(self) -> str:
        self.number += 1
        return f"token-{self.number}"


def make_queue(
    storage: MemoryExecutionStorage | JsonExecutionStorage,
    clock: _TestClock,
    tokens: Tokens,
    *,
    max_attempts: int = 3,
) -> ExecutionQueue:
    return ExecutionQueue(storage, clock, tokens, max_attempts=max_attempts)


def test_restart_reuses_accepted_command_and_external_job(tmp_path: Path) -> None:
    clock = _TestClock()
    tokens = Tokens()
    storage = JsonExecutionStorage(tmp_path / "execution.json")
    first = make_queue(storage, clock, tokens)

    accepted = first.submit(
        "workflow-1",
        {"strategy": "mean-reversion"},
        idempotency_key="command-1",
    )
    job = first.submit_tool(
        "workflow-1",
        "backtest",
        {"symbol": "SPY"},
        idempotency_key="job-submit-1",
        job_id="external-job-1",
    )
    lease = first.acquire_lease("workflow-1", "worker-1", ttl=60)
    claimed = first.claim_job(lease)
    assert claimed is not None
    assert claimed.job_id == job.job_id

    restarted = make_queue(
        JsonExecutionStorage(tmp_path / "execution.json"), clock, tokens
    )
    recovered = restarted.recover()

    assert recovered[0].workflow_id == accepted.workflow_id
    assert (
        restarted.submit(
            "workflow-1",
            {"strategy": "mean-reversion"},
            idempotency_key="command-1",
        )
        == accepted
    )
    assert (
        restarted.submit_tool(
            "workflow-1",
            "backtest",
            {"symbol": "SPY"},
            idempotency_key="job-submit-1",
            job_id="a-different-job-id",
        ).job_id
        == "external-job-1"
    )
    recovered_job = restarted.get_job("external-job-1")
    assert recovered_job is not None
    assert recovered_job.status == "pending"
    assert recovered_job.attempts == 1

    new_lease = restarted.acquire_lease("workflow-1", "worker-2", ttl=60)
    claimed = restarted.claim_job(new_lease)
    assert claimed is not None
    assert claimed.job_id == "external-job-1"
    assert (
        restarted.complete_job(claimed.job_id, new_lease, {"sharpe": 1.2}).status
        == "succeeded"
    )


def test_duplicate_commands_and_jobs_are_idempotent_and_conflicts_are_rejected() -> (
    None
):
    clock = _TestClock()
    queue = make_queue(MemoryExecutionStorage(), clock, Tokens())

    command = queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1")
    assert (
        queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1") == command
    )
    with pytest.raises(IdempotencyConflict):
        queue.submit("workflow-1", {"value": 2}, idempotency_key="command-1")

    job = queue.submit_tool(
        "workflow-1",
        "tool",
        {"value": 1},
        idempotency_key="job-1",
        job_id="external-1",
    )
    assert (
        queue.submit_tool(
            "workflow-1",
            "tool",
            {"value": 1},
            idempotency_key="job-1",
            job_id="external-2",
        )
        == job
    )
    with pytest.raises(IdempotencyConflict):
        queue.submit_tool(
            "workflow-1",
            "tool",
            {"value": 2},
            idempotency_key="job-1",
        )


def test_fenced_worker_cannot_publish_and_late_result_is_queryable() -> None:
    clock = _TestClock()
    queue = make_queue(MemoryExecutionStorage(), clock, Tokens())
    queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1")
    job = queue.submit_tool(
        "workflow-1", "tool", {}, idempotency_key="job-1", job_id="job-1"
    )

    old_lease = queue.acquire_lease("workflow-1", "worker-1", ttl=5)
    assert queue.claim_job(old_lease) is not None
    clock.advance(6)
    new_lease = queue.acquire_lease("workflow-1", "worker-2", ttl=60)
    assert new_lease.fence_token > old_lease.fence_token

    with pytest.raises(FencingError):
        queue.publish_mutation("workflow-1", old_lease, {"target": "git"})

    late = queue.complete_job(job.job_id, old_lease, {"old": True}, result_id="late-1")
    assert late.status == "pending"
    assert queue.late_results(job.job_id)[0].result == {"old": True}
    current_job = queue.get_job(job.job_id)
    assert current_job is not None
    assert current_job.result is None

    claimed = queue.claim_job(new_lease)
    assert claimed is not None
    queue.complete_job(job.job_id, new_lease, {"new": True})
    current_job = queue.get_job(job.job_id)
    assert current_job is not None
    assert current_job.result == {"new": True}


def test_steering_advances_intent_and_supersedes_late_work() -> None:
    clock = _TestClock()
    queue = make_queue(MemoryExecutionStorage(), clock, Tokens())
    queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1")
    old_job = queue.submit_tool(
        "workflow-1", "tool", {}, idempotency_key="job-old", job_id="job-old"
    )
    old_lease = queue.acquire_lease("workflow-1", "worker-1", ttl=60)
    queue.claim_job(old_lease)

    steered = queue.steer("workflow-1", {"direction": "new hypothesis"})
    assert steered.intent_epoch == 1
    late = queue.complete_job(
        old_job.job_id, old_lease, {"stale": True}, result_id="late-epoch"
    )
    assert late.status == "superseded"
    assert queue.late_results(old_job.job_id)[0].intent_epoch == 0

    new_job = queue.submit_tool(
        "workflow-1",
        "tool",
        {"epoch": 1},
        idempotency_key="job-new",
        job_id="job-new",
    )
    assert new_job.intent_epoch == 1
    new_lease = queue.acquire_lease("workflow-1", "worker-2", ttl=60)
    new_claim = queue.claim_job(new_lease)
    assert new_claim is not None
    assert new_claim.job_id == "job-new"
    assert (
        queue.complete_job("job-new", new_lease, {"fresh": True}).status == "succeeded"
    )


def test_checkpoint_versions_are_validated_and_incompatible_state_enters_recovery() -> (
    None
):
    clock = _TestClock()
    checkpoint_storage = MemoryCheckpointStorage()
    writer = CheckpointStore(checkpoint_storage, clock, supported_version=1)
    checkpoint = writer.save("workflow-1", {"cursor": 4}, intent_epoch=0)

    with pytest.raises(CheckpointCompatibilityError):
        CheckpointStore(checkpoint_storage, clock, supported_version=2).validate(
            checkpoint
        )
    assert not CheckpointStore(
        checkpoint_storage, clock, supported_version=2
    ).compatible("workflow-1")

    queue = make_queue(MemoryExecutionStorage(), clock, Tokens())
    queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1")
    recovery = WorkflowRecovery(
        queue,
        CheckpointStore(checkpoint_storage, clock, supported_version=2),
        checkpoint_version=2,
    )

    result = recovery.restart()[0]
    assert result.status == "recovery"
    recovered_workflow = queue.get_workflow("workflow-1")
    assert recovered_workflow is not None
    assert recovered_workflow.status == "recovery"
    assert recovered_workflow.recovery_reason is not None
    assert "incompatible" in recovered_workflow.recovery_reason


def test_failed_jobs_become_explicit_dead_letters_after_attempt_limit() -> None:
    clock = _TestClock()
    queue = make_queue(MemoryExecutionStorage(), clock, Tokens(), max_attempts=2)
    queue.submit("workflow-1", {"value": 1}, idempotency_key="command-1")
    queue.submit_tool("workflow-1", "tool", {}, idempotency_key="job-1", job_id="job-1")

    first_lease = queue.acquire_lease("workflow-1", "worker-1", ttl=60)
    queue.claim_job(first_lease)
    assert queue.fail_job("job-1", first_lease, "first failure").status == "pending"

    second_lease = queue.acquire_lease("workflow-1", "worker-2", ttl=60)
    queue.claim_job(second_lease)
    dead = queue.fail_job("job-1", second_lease, "terminal failure")

    assert dead.status == "dead_letter"
    assert queue.dead_letters() == (dead,)
    workflow = queue.require_workflow("workflow-1")
    assert workflow.status == "dead_letter"
    assert workflow.failure == "terminal failure"
