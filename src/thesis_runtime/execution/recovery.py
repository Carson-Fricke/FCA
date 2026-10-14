"""Restart orchestration for the local durable execution projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from thesis_runtime.execution.checkpoints import (
    CheckpointError,
    CheckpointStore,
)
from thesis_runtime.execution.queue import ExecutionQueue, Workflow


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """The outcome for one workflow during restart recovery."""

    workflow_id: str
    status: str
    checkpoint_version: int | None
    reason: str | None = None

    @property
    def recovered(self) -> bool:
        """Return whether execution may resume from the checkpoint."""
        return self.status == "resumed"

    @property
    def needs_recovery(self) -> bool:
        """Return whether operator or coordinator intervention is required."""
        return self.status == "recovery"


class WorkflowRecovery:
    """Validate checkpoints and requeue abandoned leases after process restart."""

    def __init__(
        self,
        queue: ExecutionQueue,
        checkpoints: CheckpointStore,
        clock: Callable[[], datetime] | None = None,
        *,
        checkpoint_version: int | None = None,
    ) -> None:
        self.queue = queue
        self.checkpoints = checkpoints
        self.clock = queue.clock if clock is None else clock
        self.checkpoint_version = (
            checkpoints.supported_version
            if checkpoint_version is None
            else checkpoint_version
        )

    def restart(self) -> tuple[RecoveryResult, ...]:
        """Recover abandoned jobs and classify every durable workflow."""
        self.queue.recover()
        results: list[RecoveryResult] = []
        workflows = self._workflows()
        for workflow in workflows:
            checkpoint = None
            checkpoint_error: CheckpointError | None = None
            try:
                checkpoint = self.checkpoints.get(workflow.workflow_id)
            except CheckpointError as error:
                checkpoint_error = error
            if checkpoint_error is not None:
                recovered = self.queue.enter_recovery(
                    workflow.workflow_id, str(checkpoint_error)
                )
                results.append(
                    RecoveryResult(
                        workflow_id=workflow.workflow_id,
                        status=recovered.status,
                        checkpoint_version=None,
                        reason=recovered.recovery_reason,
                    )
                )
                continue
            if checkpoint is None:
                if workflow.status in {"queued", "running"}:
                    results.append(
                        RecoveryResult(
                            workflow_id=workflow.workflow_id,
                            status="resumed",
                            checkpoint_version=None,
                        )
                    )
                else:
                    results.append(
                        RecoveryResult(
                            workflow_id=workflow.workflow_id,
                            status=workflow.status,
                            checkpoint_version=None,
                            reason=workflow.failure or workflow.recovery_reason,
                        )
                    )
                continue
            try:
                self.checkpoints.validate(
                    checkpoint,
                    expected_version=self.checkpoint_version,
                    expected_workflow_id=workflow.workflow_id,
                )
            except CheckpointError as error:
                recovered = self.queue.enter_recovery(workflow.workflow_id, str(error))
                results.append(
                    RecoveryResult(
                        workflow_id=workflow.workflow_id,
                        status=recovered.status,
                        checkpoint_version=checkpoint.checkpoint_version,
                        reason=recovered.recovery_reason,
                    )
                )
                continue
            results.append(
                RecoveryResult(
                    workflow_id=workflow.workflow_id,
                    status="resumed",
                    checkpoint_version=checkpoint.checkpoint_version,
                )
            )
        return tuple(results)

    recover = restart
    recover_after_restart = restart

    def restore(self, workflow_id: str) -> dict[str, object]:
        """Restore compatible checkpoint state for coordinator execution."""
        return dict(
            self.checkpoints.restore(
                workflow_id,
                expected_version=self.checkpoint_version,
            )
        )

    def recover_workflow(self, workflow_id: str) -> RecoveryResult:
        """Recover one workflow and enter recovery for incompatible state."""
        workflow = self.queue.require_workflow(workflow_id)
        checkpoint = self.checkpoints.get(workflow_id)
        if checkpoint is None:
            if workflow.status in {"queued", "running"}:
                return RecoveryResult(workflow_id, "resumed", None)
            return RecoveryResult(
                workflow_id,
                workflow.status,
                None,
                workflow.failure or workflow.recovery_reason,
            )
        try:
            self.checkpoints.validate(
                checkpoint,
                expected_version=self.checkpoint_version,
                expected_workflow_id=workflow_id,
            )
        except CheckpointError as error:
            recovered = self.queue.enter_recovery(workflow_id, str(error))
            return RecoveryResult(
                workflow_id,
                recovered.status,
                checkpoint.checkpoint_version,
                recovered.recovery_reason,
            )
        return RecoveryResult(workflow_id, "resumed", checkpoint.checkpoint_version)

    def _workflows(self) -> tuple[Workflow, ...]:
        projection = self.queue.storage.load()
        workflows = projection.get("workflows", {})
        if not isinstance(workflows, dict):
            return ()
        return tuple(
            sorted(
                (self.queue.require_workflow(workflow_id) for workflow_id in workflows),
                key=lambda workflow: (workflow.accepted_at, workflow.workflow_id),
            )
        )


RecoveryManager = WorkflowRecovery
RestartRecovery = WorkflowRecovery


__all__ = [
    "RecoveryManager",
    "RecoveryResult",
    "RestartRecovery",
    "WorkflowRecovery",
]
