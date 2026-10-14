"""Local durable workflow execution projection with fencing and recovery.

This module is a state projection, not an external-tool runner.  Callers submit
commands and jobs, lease work, and publish results through explicit methods.  A
storage adapter owns durability; a clock and token factory own all non-pure
inputs so the projection remains deterministic in tests and during replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]
WorkflowStatus = Literal[
    "queued", "running", "succeeded", "failed", "dead_letter", "recovery"
]
JobStatus = Literal[
    "pending",
    "leased",
    "succeeded",
    "failed",
    "dead_letter",
    "superseded",
]


class _Projection(TypedDict):
    """Typed view over the validated JSON projection."""

    schemaVersion: int
    nextFenceToken: int
    workflows: dict[str, object]
    commands: dict[str, object]
    jobs: dict[str, object]
    jobKeys: dict[str, object]
    leases: dict[str, object]
    mutations: dict[str, object]
    events: list[object]


_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")


class ExecutionError(RuntimeError):
    """Base error for invalid execution projection operations."""


class IdempotencyConflict(ExecutionError):
    """An idempotency key was reused for different accepted content."""


class FencingError(ExecutionError):
    """A worker attempted a protected mutation with stale ownership."""


class LeaseUnavailable(ExecutionError):
    """Another unexpired worker lease owns the workflow."""


class LeaseError(ExecutionError):
    """A lease is malformed, expired, or no longer current."""


class InvalidTransition(ExecutionError):
    """A workflow or job cannot make the requested state transition."""


class ExecutionStorage(Protocol):
    """Durability boundary for the local execution projection."""

    def load(self) -> Mapping[str, object]:
        """Load the complete projection."""

    def save(self, value: Mapping[str, object]) -> None:
        """Atomically persist the complete projection."""


class MemoryExecutionStorage:
    """Detached in-memory storage for deterministic tests."""

    def __init__(self, initial: Mapping[str, object] | None = None) -> None:
        self._value: JsonObject = _json_object({} if initial is None else initial)

    def load(self) -> Mapping[str, object]:
        """Return a detached projection copy."""
        return _json_object(self._value)

    def save(self, value: Mapping[str, object]) -> None:
        """Replace the projection with a detached JSON copy."""
        self._value = _json_object(value)


class JsonExecutionStorage:
    """Local file storage with a replace-and-fsync write boundary."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Mapping[str, object]:
        """Load a projection, returning an empty one before first use."""
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise ExecutionError(
                f"execution storage path is not a plain file: {self.path}"
            )
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                value: object = json.load(stream)
        except (OSError, json.JSONDecodeError) as error:
            raise ExecutionError(
                f"could not read execution storage: {self.path}"
            ) from error
        return _json_object(value)

    def save(self, value: Mapping[str, object]) -> None:
        """Atomically persist a complete projection."""
        encoded = _canonical_json(_json_object(value)) + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ExecutionError(
                f"could not write execution storage: {self.path}"
            ) from error


@dataclass(frozen=True, slots=True)
class Workflow:
    """The durable command and current workflow projection."""

    workflow_id: str
    command_id: str
    command: Mapping[str, JsonValue]
    intent_epoch: int
    status: WorkflowStatus
    accepted_at: str
    updated_at: str
    failure: str | None = None
    recovery_reason: str | None = None

    @property
    def idempotency_key(self) -> str:
        """Return the accepted command's idempotency key."""
        return self.command_id

    @property
    def epoch(self) -> int:
        """Return the current steering intent epoch."""
        return self.intent_epoch

    def as_dict(self) -> JsonObject:
        """Return the durable JSON representation."""
        return {
            "acceptedAt": self.accepted_at,
            "command": _json_object(self.command),
            "commandId": self.command_id,
            "failure": self.failure,
            "intentEpoch": self.intent_epoch,
            "recoveryReason": self.recovery_reason,
            "status": self.status,
            "updatedAt": self.updated_at,
            "workflowId": self.workflow_id,
        }


@dataclass(frozen=True, slots=True)
class Lease:
    """A fenced, time-bounded ownership grant for one workflow."""

    workflow_id: str
    worker_id: str
    lease_token: str
    fence_token: int
    intent_epoch: int
    expires_at: str

    @property
    def token(self) -> str:
        """Return the opaque lease token."""
        return self.lease_token

    def as_dict(self) -> JsonObject:
        """Return the durable JSON representation."""
        return {
            "expiresAt": self.expires_at,
            "fenceToken": self.fence_token,
            "intentEpoch": self.intent_epoch,
            "leaseToken": self.lease_token,
            "workerId": self.worker_id,
            "workflowId": self.workflow_id,
        }


@dataclass(frozen=True, slots=True)
class LateResult:
    """A result observed after its worker lost authority or its intent aged."""

    result_id: str
    job_id: str
    intent_epoch: int
    fence_token: int | None
    result: JsonValue
    observed_at: str
    reason: str


@dataclass(frozen=True, slots=True)
class ToolJob:
    """An idempotently accepted external-tool job."""

    job_id: str
    workflow_id: str
    submission_key: str
    tool_name: str
    payload: Mapping[str, JsonValue]
    intent_epoch: int
    status: JobStatus
    attempts: int
    accepted_at: str
    updated_at: str
    lease_token: str | None = None
    fence_token: int | None = None
    result: JsonValue = None
    error: str | None = None
    late_results: tuple[LateResult, ...] = ()

    @property
    def idempotency_key(self) -> str:
        """Return the stable external-job submission key."""
        return self.submission_key

    @property
    def external_job_id(self) -> str:
        """Return the durable job identity used by an external adapter."""
        return self.job_id

    def as_dict(self) -> JsonObject:
        """Return the durable JSON representation."""
        return {
            "acceptedAt": self.accepted_at,
            "attempts": self.attempts,
            "error": self.error,
            "fenceToken": self.fence_token,
            "intentEpoch": self.intent_epoch,
            "jobId": self.job_id,
            "lateResults": [
                {
                    "fenceToken": item.fence_token,
                    "intentEpoch": item.intent_epoch,
                    "jobId": item.job_id,
                    "observedAt": item.observed_at,
                    "reason": item.reason,
                    "result": item.result,
                    "resultId": item.result_id,
                }
                for item in self.late_results
            ],
            "leaseToken": self.lease_token,
            "payload": _json_object(self.payload),
            "result": self.result,
            "status": self.status,
            "submissionKey": self.submission_key,
            "toolName": self.tool_name,
            "updatedAt": self.updated_at,
            "workflowId": self.workflow_id,
        }


@dataclass(frozen=True, slots=True)
class Mutation:
    """A protected published mutation receipt."""

    mutation_id: str
    workflow_id: str
    payload: Mapping[str, JsonValue]
    intent_epoch: int
    fence_token: int
    published_at: str

    def as_dict(self) -> JsonObject:
        """Return the durable JSON representation."""
        return {
            "fenceToken": self.fence_token,
            "intentEpoch": self.intent_epoch,
            "mutationId": self.mutation_id,
            "payload": _json_object(self.payload),
            "publishedAt": self.published_at,
            "workflowId": self.workflow_id,
        }


class ExecutionQueue:
    """Durable local workflow projection with fenced worker operations."""

    def __init__(
        self,
        storage: ExecutionStorage,
        clock: Callable[[], datetime],
        token_factory: Callable[[], object],
        *,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.storage = storage
        self.clock = clock
        self.token_factory = token_factory
        self.max_attempts = max_attempts

    def submit(
        self,
        workflow_id: str,
        command: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        command_id: str | None = None,
    ) -> Workflow:
        """Durably accept one command, returning the same record on retry."""
        _validate_identifier(workflow_id, "workflow_id")
        normalized = _json_object(command)
        key = idempotency_key if idempotency_key is not None else command_id
        if key is None:
            key = f"command-{_digest({'workflowId': workflow_id, 'command': normalized})[:32]}"
        _validate_identifier(key, "idempotency_key")
        projection = self._projection()
        existing_id = projection["commands"].get(key)
        if existing_id is not None:
            existing_workflow = projection["workflows"].get(str(existing_id))
            if not isinstance(existing_workflow, dict):
                raise ExecutionError(f"command index points to missing workflow: {key}")
            existing = _workflow_from_value(existing_workflow)
            if existing.workflow_id != workflow_id or _digest(
                existing.command
            ) != _digest(normalized):
                raise IdempotencyConflict(f"command key {key!r} has different content")
            return existing
        by_workflow = projection["workflows"].get(workflow_id)
        if by_workflow is not None:
            existing = _workflow_from_value(by_workflow)
            raise IdempotencyConflict(
                f"workflow {workflow_id!r} already has accepted command {existing.command_id!r}"
            )
        now = _timestamp(self.clock)
        workflow = Workflow(
            workflow_id=workflow_id,
            command_id=key,
            command=normalized,
            intent_epoch=0,
            status="queued",
            accepted_at=now,
            updated_at=now,
        )
        projection["workflows"][workflow_id] = workflow.as_dict()
        projection["commands"][key] = workflow_id
        self._event(projection, "command_accepted", workflow_id, {"commandId": key})
        self._save(projection)
        return workflow

    accept_command = submit
    submit_command = submit
    enqueue = submit

    def submit_tool(
        self,
        workflow_id: str,
        tool_name: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        job_id: str | None = None,
        intent_epoch: int | None = None,
    ) -> ToolJob:
        """Accept one external-tool job without duplicating its submission."""
        _validate_identifier(workflow_id, "workflow_id")
        _validate_identifier(tool_name, "tool_name")
        normalized = _json_object(payload)
        projection = self._projection()
        workflow_value = projection["workflows"].get(workflow_id)
        if workflow_value is None:
            raise ExecutionError(f"unknown workflow: {workflow_id}")
        workflow = _workflow_from_value(workflow_value)
        epoch = workflow.intent_epoch if intent_epoch is None else intent_epoch
        if epoch < 0:
            raise ValueError("intent_epoch must not be negative")
        key = idempotency_key
        if key is None:
            key = f"job-{_digest({'workflowId': workflow_id, 'tool': tool_name, 'payload': normalized})[:32]}"
        _validate_identifier(key, "idempotency_key")
        existing_id = projection["jobKeys"].get(key)
        if existing_id is not None:
            existing_value = projection["jobs"].get(str(existing_id))
            if not isinstance(existing_value, dict):
                raise ExecutionError(f"job index points to missing job: {key}")
            existing = _job_from_value(existing_value)
            if (
                existing.workflow_id != workflow_id
                or existing.tool_name != tool_name
                or _digest(existing.payload) != _digest(normalized)
            ):
                raise IdempotencyConflict(f"job key {key!r} has different content")
            return existing
        if workflow.status in {"succeeded", "failed", "dead_letter", "recovery"}:
            raise InvalidTransition(f"workflow {workflow_id!r} is {workflow.status}")
        if epoch != workflow.intent_epoch:
            raise FencingError("tool submission uses a stale intent epoch")
        supplied_job_id = job_id
        if supplied_job_id is None:
            supplied_job_id = f"job-{_token(self.token_factory())}"
        _validate_identifier(supplied_job_id, "job_id")
        if supplied_job_id in projection["jobs"]:
            raise IdempotencyConflict(f"job ID {supplied_job_id!r} is already accepted")
        now = _timestamp(self.clock)
        job = ToolJob(
            job_id=supplied_job_id,
            workflow_id=workflow_id,
            submission_key=key,
            tool_name=tool_name,
            payload=normalized,
            intent_epoch=epoch,
            status="pending",
            attempts=0,
            accepted_at=now,
            updated_at=now,
        )
        projection["jobs"][job.job_id] = job.as_dict()
        projection["jobKeys"][key] = job.job_id
        if workflow.status == "queued":
            workflow = _replace_workflow(workflow, status="running", updated_at=now)
            projection["workflows"][workflow_id] = workflow.as_dict()
        self._event(projection, "tool_job_accepted", workflow_id, {"jobId": job.job_id})
        self._save(projection)
        return job

    submit_job = submit_tool
    accept_tool_submission = submit_tool

    def acquire_lease(
        self,
        workflow_id: str,
        worker_id: str,
        ttl: timedelta | float = 30.0,
    ) -> Lease:
        """Acquire the next fenced ownership grant for a workflow."""
        _validate_identifier(workflow_id, "workflow_id")
        _validate_identifier(worker_id, "worker_id")
        duration = _duration(ttl)
        projection = self._projection()
        workflow_value = projection["workflows"].get(workflow_id)
        if workflow_value is None:
            raise ExecutionError(f"unknown workflow: {workflow_id}")
        workflow = _workflow_from_value(workflow_value)
        if workflow.status in {"succeeded", "failed", "dead_letter", "recovery"}:
            raise InvalidTransition(f"workflow {workflow_id!r} is {workflow.status}")
        now = self.clock()
        current_value = projection["leases"].get(workflow_id)
        if current_value is not None:
            current = _lease_from_value(current_value)
            if _parse_timestamp(current.expires_at) > _aware(now):
                raise LeaseUnavailable(f"workflow {workflow_id!r} is already leased")
            for job_id, job_value in tuple(projection["jobs"].items()):
                job = _job_from_value(job_value)
                if (
                    job.workflow_id == workflow_id
                    and job.status == "leased"
                    and job.lease_token == current.lease_token
                    and job.fence_token == current.fence_token
                ):
                    projection["jobs"][job_id] = _replace_job(
                        job,
                        status="pending",
                        updated_at=_timestamp(self.clock),
                        lease_token=None,
                        fence_token=None,
                    ).as_dict()
            del projection["leases"][workflow_id]
        fence_token = int(projection["nextFenceToken"]) + 1
        lease_token = _token(self.token_factory())
        active_tokens = {
            _lease_from_value(value).lease_token
            for value in projection["leases"].values()
            if isinstance(value, dict)
        }
        if lease_token in active_tokens:
            raise ExecutionError("token factory returned an active lease token")
        expires_at = _format_timestamp(_aware(now) + duration)
        lease = Lease(
            workflow_id=workflow_id,
            worker_id=worker_id,
            lease_token=lease_token,
            fence_token=fence_token,
            intent_epoch=workflow.intent_epoch,
            expires_at=expires_at,
        )
        projection["nextFenceToken"] = fence_token
        projection["leases"][workflow_id] = lease.as_dict()
        self._event(
            projection,
            "lease_acquired",
            workflow_id,
            {"fenceToken": fence_token, "workerId": worker_id},
        )
        self._save(projection)
        return lease

    claim_lease = acquire_lease
    acquire = acquire_lease

    def renew_lease(self, lease: Lease, ttl: timedelta | float = 30.0) -> Lease:
        """Extend a current lease without changing its fencing token."""
        duration = _duration(ttl)
        projection = self._projection()
        current = self._require_current_lease(projection, lease, require_unexpired=True)
        expires_at = _format_timestamp(_aware(self.clock()) + duration)
        renewed = Lease(
            workflow_id=current.workflow_id,
            worker_id=current.worker_id,
            lease_token=current.lease_token,
            fence_token=current.fence_token,
            intent_epoch=current.intent_epoch,
            expires_at=expires_at,
        )
        projection["leases"][lease.workflow_id] = renewed.as_dict()
        self._save(projection)
        return renewed

    def release_lease(self, lease: Lease) -> None:
        """Release a current lease; stale releases cannot remove new ownership."""
        projection = self._projection()
        current = self._require_current_lease(
            projection, lease, require_unexpired=False
        )
        now = _timestamp(self.clock)
        for job_id, value in tuple(projection["jobs"].items()):
            job = _job_from_value(value)
            if (
                job.workflow_id == lease.workflow_id
                and job.status == "leased"
                and job.lease_token == current.lease_token
                and job.fence_token == current.fence_token
            ):
                projection["jobs"][job_id] = _replace_job(
                    job,
                    status="pending",
                    updated_at=now,
                    lease_token=None,
                    fence_token=None,
                ).as_dict()
        del projection["leases"][lease.workflow_id]
        self._save(projection)

    release = release_lease

    def claim_job(self, lease: Lease) -> ToolJob | None:
        """Fence and claim one pending job for a worker."""
        projection = self._projection()
        current = self._require_current_lease(projection, lease, require_unexpired=True)
        jobs = [_job_from_value(value) for value in projection["jobs"].values()]
        owned = next(
            (
                job
                for job in jobs
                if job.workflow_id == lease.workflow_id
                and job.status == "leased"
                and job.lease_token == current.lease_token
                and job.fence_token == current.fence_token
            ),
            None,
        )
        if owned is not None:
            return owned
        candidate = next(
            (
                job
                for job in sorted(
                    jobs, key=lambda item: (item.accepted_at, item.job_id)
                )
                if job.workflow_id == lease.workflow_id
                and job.status == "pending"
                and job.intent_epoch == current.intent_epoch
            ),
            None,
        )
        if candidate is None:
            return None
        claimed = _replace_job(
            candidate,
            status="leased",
            attempts=candidate.attempts + 1,
            updated_at=_timestamp(self.clock),
            lease_token=current.lease_token,
            fence_token=current.fence_token,
        )
        projection["jobs"][candidate.job_id] = claimed.as_dict()
        self._save(projection)
        return claimed

    claim_next = claim_job
    claim_next_job = claim_job

    def complete_job(
        self,
        job_id: str,
        lease: Lease,
        result: object,
        *,
        intent_epoch: int | None = None,
        result_id: str | None = None,
    ) -> ToolJob:
        """Publish a result, or retain it as a late observation if stale."""
        projection = self._projection()
        job = self._get_job(projection, job_id)
        normalized_result = _json_value(result)
        expected_epoch = lease.intent_epoch if intent_epoch is None else intent_epoch
        workflow = self._get_workflow(projection, job.workflow_id)
        stale_reason = self._stale_reason(
            projection,
            lease,
            workflow,
            expected_epoch,
            require_unexpired=True,
        )
        if stale_reason is not None:
            late = self._record_late_result(
                projection,
                job,
                normalized_result,
                result_id=result_id,
                reason=stale_reason,
                fence_token=lease.fence_token,
                intent_epoch=expected_epoch,
            )
            self._save(projection)
            return late
        if job.status == "succeeded":
            return job
        if job.status != "leased" or job.lease_token != lease.lease_token:
            raise InvalidTransition(f"job {job_id!r} is not leased by this worker")
        completed = _replace_job(
            job,
            status="succeeded",
            updated_at=_timestamp(self.clock),
            lease_token=None,
            fence_token=None,
            result=normalized_result,
            error=None,
        )
        projection["jobs"][job_id] = completed.as_dict()
        self._update_workflow_after_job(projection, workflow.workflow_id)
        self._event(
            projection, "tool_job_succeeded", workflow.workflow_id, {"jobId": job_id}
        )
        del projection["leases"][lease.workflow_id]
        self._save(projection)
        return completed

    complete = complete_job
    publish_job_result = complete_job

    def fail_job(
        self,
        job_id: str,
        lease: Lease,
        error: BaseException | str,
        *,
        intent_epoch: int | None = None,
    ) -> ToolJob:
        """Retry a failed job or move it and its workflow to dead letter."""
        projection = self._projection()
        job = self._get_job(projection, job_id)
        workflow = self._get_workflow(projection, job.workflow_id)
        expected_epoch = lease.intent_epoch if intent_epoch is None else intent_epoch
        stale_reason = self._stale_reason(
            projection,
            lease,
            workflow,
            expected_epoch,
            require_unexpired=True,
        )
        if stale_reason is not None:
            raise FencingError(stale_reason)
        if job.status != "leased" or job.lease_token != lease.lease_token:
            raise InvalidTransition(f"job {job_id!r} is not leased by this worker")
        message = str(error).strip() or type(error).__name__
        terminal = job.attempts >= self.max_attempts
        failed = _replace_job(
            job,
            status="dead_letter" if terminal else "pending",
            updated_at=_timestamp(self.clock),
            lease_token=None,
            fence_token=None,
            error=message[:2048],
        )
        projection["jobs"][job_id] = failed.as_dict()
        projection["leases"].pop(lease.workflow_id, None)
        if terminal:
            failed_workflow = _replace_workflow(
                workflow,
                status="dead_letter",
                updated_at=failed.updated_at,
                failure=message[:2048],
            )
            projection["workflows"][workflow.workflow_id] = failed_workflow.as_dict()
            self._event(
                projection,
                "tool_job_dead_lettered",
                workflow.workflow_id,
                {"jobId": job_id, "error": message[:2048]},
            )
        else:
            queued_workflow = _replace_workflow(
                workflow,
                status="queued",
                updated_at=failed.updated_at,
                failure=message[:2048],
            )
            projection["workflows"][workflow.workflow_id] = queued_workflow.as_dict()
            self._event(
                projection, "tool_job_failed", workflow.workflow_id, {"jobId": job_id}
            )
        self._save(projection)
        return failed

    def fail_workflow(
        self,
        workflow_id: str,
        reason: BaseException | str,
        *,
        lease: Lease | None = None,
        intent_epoch: int | None = None,
    ) -> Workflow:
        """Move a workflow to failed and invalidate all active work.

        A worker may only use a lease from the current intent epoch.  A caller
        without a lease is an explicit coordinator or operator action, not a
        worker publication.
        """
        projection = self._projection()
        workflow = self._get_workflow(projection, workflow_id)
        if lease is not None:
            expected_epoch = (
                lease.intent_epoch if intent_epoch is None else intent_epoch
            )
            self._assert_current_authority(projection, lease, workflow, expected_epoch)
        if workflow.status == "failed":
            return workflow
        if workflow.status in {"succeeded", "dead_letter", "recovery"}:
            raise InvalidTransition(f"workflow {workflow_id!r} is {workflow.status}")
        message = str(reason).strip() or type(reason).__name__
        now = _timestamp(self.clock)
        failed = _replace_workflow(
            workflow,
            status="failed",
            updated_at=now,
            failure=message[:2048],
        )
        projection["workflows"][workflow_id] = failed.as_dict()
        for job_id, value in tuple(projection["jobs"].items()):
            job = _job_from_value(value)
            if job.workflow_id != workflow_id or job.status not in {
                "pending",
                "leased",
            }:
                continue
            projection["jobs"][job_id] = _replace_job(
                job,
                status="failed",
                updated_at=now,
                lease_token=None,
                fence_token=None,
                error=message[:2048],
            ).as_dict()
        projection["leases"].pop(workflow_id, None)
        self._event(
            projection,
            "workflow_failed",
            workflow_id,
            {"error": message[:2048]},
        )
        self._save(projection)
        return failed

    mark_failed = fail_workflow
    fail_execution = fail_workflow
    fail_job_result = fail_job
    fail = fail_job

    def steer(
        self,
        workflow_id: str,
        steering: Mapping[str, object] | None = None,
    ) -> Workflow:
        """Advance intent and supersede pending work from the previous epoch."""
        projection = self._projection()
        workflow = self._get_workflow(projection, workflow_id)
        if workflow.status in {"succeeded", "failed", "dead_letter"}:
            raise InvalidTransition(f"workflow {workflow_id!r} is {workflow.status}")
        now = _timestamp(self.clock)
        advanced = _replace_workflow(
            workflow,
            intent_epoch=workflow.intent_epoch + 1,
            status="queued",
            updated_at=now,
            failure=None,
        )
        projection["workflows"][workflow_id] = advanced.as_dict()
        for value in tuple(projection["jobs"].values()):
            job = _job_from_value(value)
            if (
                job.workflow_id == workflow_id
                and job.status in {"pending", "leased"}
                and job.intent_epoch < advanced.intent_epoch
            ):
                superseded = _replace_job(job, status="superseded", updated_at=now)
                projection["jobs"][job.job_id] = superseded.as_dict()
        projection["leases"].pop(workflow_id, None)
        details = {} if steering is None else _json_object(steering)
        self._event(
            projection,
            "intent_steered",
            workflow_id,
            {"intentEpoch": advanced.intent_epoch, "steering": details},
        )
        self._save(projection)
        return advanced

    steer_workflow = steer
    increment_intent_epoch = steer

    def publish_mutation(
        self,
        workflow_id: str,
        lease: Lease,
        payload: Mapping[str, object],
        *,
        mutation_id: str | None = None,
        intent_epoch: int | None = None,
    ) -> Mutation:
        """Publish one protected strategy, Git, or findings mutation."""
        projection = self._projection()
        workflow = self._get_workflow(projection, workflow_id)
        expected_epoch = lease.intent_epoch if intent_epoch is None else intent_epoch
        self._assert_current_authority(
            projection,
            lease,
            workflow,
            expected_epoch,
        )
        normalized = _json_object(payload)
        key = mutation_id
        if key is None:
            key = f"mutation-{_digest({'workflowId': workflow_id, 'payload': normalized})[:32]}"
        _validate_identifier(key, "mutation_id")
        existing_value = projection["mutations"].get(key)
        if existing_value is not None:
            existing = _mutation_from_value(existing_value)
            if existing.workflow_id != workflow_id or _digest(
                existing.payload
            ) != _digest(normalized):
                raise IdempotencyConflict(f"mutation ID {key!r} has different content")
            return existing
        published = Mutation(
            mutation_id=key,
            workflow_id=workflow_id,
            payload=normalized,
            intent_epoch=expected_epoch,
            fence_token=lease.fence_token,
            published_at=_timestamp(self.clock),
        )
        projection["mutations"][key] = published.as_dict()
        self._event(projection, "mutation_published", workflow_id, {"mutationId": key})
        self._save(projection)
        return published

    publish = publish_mutation
    publish_result = publish_mutation

    def enter_recovery(self, workflow_id: str, reason: str) -> Workflow:
        """Put a workflow in an explicit recovery state without running it."""
        projection = self._projection()
        workflow = self._get_workflow(projection, workflow_id)
        message = str(reason).strip() or "recovery required"
        now = _timestamp(self.clock)
        recovered = _replace_workflow(
            workflow,
            status="recovery",
            updated_at=now,
            recovery_reason=message[:2048],
        )
        projection["workflows"][workflow_id] = recovered.as_dict()
        projection["leases"].pop(workflow_id, None)
        self._event(
            projection, "recovery_required", workflow_id, {"reason": message[:2048]}
        )
        self._save(projection)
        return recovered

    mark_recovery = enter_recovery

    def recover(self) -> tuple[Workflow, ...]:
        """Requeue in-flight jobs after process restart without new IDs."""
        projection = self._projection()
        recovered: list[Workflow] = []
        now = _timestamp(self.clock)
        for workflow_id, value in tuple(projection["workflows"].items()):
            workflow = _workflow_from_value(value)
            changed = False
            for job_id, job_value in tuple(projection["jobs"].items()):
                job = _job_from_value(job_value)
                if job.workflow_id != workflow_id or job.status != "leased":
                    continue
                if job.intent_epoch != workflow.intent_epoch:
                    superseded = _replace_job(
                        job,
                        status="superseded",
                        updated_at=now,
                        lease_token=None,
                        fence_token=None,
                    )
                    projection["jobs"][job_id] = superseded.as_dict()
                    changed = True
                    continue
                requeued = _replace_job(
                    job,
                    status="pending",
                    updated_at=now,
                    lease_token=None,
                    fence_token=None,
                )
                projection["jobs"][job_id] = requeued.as_dict()
                changed = True
            if workflow_id in projection["leases"]:
                del projection["leases"][workflow_id]
                changed = True
            if changed and workflow.status == "running":
                workflow = _replace_workflow(workflow, status="queued", updated_at=now)
                projection["workflows"][workflow_id] = workflow.as_dict()
            if changed:
                recovered.append(workflow)
                self._event(projection, "restart_recovered", workflow_id, {})
        if recovered:
            self._save(projection)
        return tuple(recovered)

    recover_after_restart = recover
    restart_recovery = recover

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        """Return a durable workflow projection."""
        value = self._projection()["workflows"].get(workflow_id)
        return None if value is None else _workflow_from_value(value)

    get = get_workflow

    def require_workflow(self, workflow_id: str) -> Workflow:
        """Return a workflow or raise an execution error."""
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ExecutionError(f"unknown workflow: {workflow_id}")
        return workflow

    def get_job(self, job_id: str) -> ToolJob | None:
        """Return an accepted tool job, including late observations."""
        value = self._projection()["jobs"].get(job_id)
        return None if value is None else _job_from_value(value)

    def jobs(self, workflow_id: str | None = None) -> tuple[ToolJob, ...]:
        """Return accepted jobs in deterministic acceptance order."""
        jobs = tuple(
            sorted(
                (
                    _job_from_value(value)
                    for value in self._projection()["jobs"].values()
                ),
                key=lambda job: (job.accepted_at, job.job_id),
            )
        )
        if workflow_id is None:
            return jobs
        return tuple(job for job in jobs if job.workflow_id == workflow_id)

    list_jobs = jobs

    def late_results(self, job_id: str) -> tuple[LateResult, ...]:
        """Return results retained from stale workers or intent epochs."""
        job = self.get_job(job_id)
        if job is None:
            raise ExecutionError(f"unknown job: {job_id}")
        return job.late_results

    def mutations(self, workflow_id: str | None = None) -> tuple[Mutation, ...]:
        """Return protected mutation receipts."""
        values = tuple(
            _mutation_from_value(value)
            for value in self._projection()["mutations"].values()
        )
        ordered = tuple(
            sorted(values, key=lambda item: (item.published_at, item.mutation_id))
        )
        if workflow_id is None:
            return ordered
        return tuple(item for item in ordered if item.workflow_id == workflow_id)

    published_mutations = mutations

    def dead_letters(self) -> tuple[ToolJob, ...]:
        """Return jobs in the explicit dead-letter state."""
        return tuple(job for job in self.jobs() if job.status == "dead_letter")

    def events(self) -> tuple[Mapping[str, JsonValue], ...]:
        """Return durable audit events for accepted and observed transitions."""
        projection = self._projection()
        return tuple(_json_object(value) for value in projection["events"])

    def _projection(self) -> _Projection:
        value = _json_object(self.storage.load())
        projection = cast(_Projection, value)
        schema = projection.get("schemaVersion", _SCHEMA_VERSION)
        if schema != _SCHEMA_VERSION:
            raise ExecutionError(f"unsupported execution storage schema: {schema!r}")
        for name in ("workflows", "commands", "jobs", "jobKeys", "leases", "mutations"):
            collection = projection.setdefault(name, {})
            if not isinstance(collection, dict):
                raise ExecutionError(
                    f"execution projection field {name!r} is not an object"
                )
        events = projection.setdefault("events", [])
        if not isinstance(events, list):
            raise ExecutionError("execution projection events are not an array")
        next_fence = projection.setdefault("nextFenceToken", 0)
        if (
            not isinstance(next_fence, int)
            or isinstance(next_fence, bool)
            or next_fence < 0
        ):
            raise ExecutionError("execution fence counter is invalid")
        projection["schemaVersion"] = _SCHEMA_VERSION
        return projection

    def _save(self, projection: _Projection) -> None:
        self.storage.save(projection)

    @staticmethod
    def _get_workflow(projection: _Projection, workflow_id: str) -> Workflow:
        value = projection["workflows"].get(workflow_id)
        if value is None:
            raise ExecutionError(f"unknown workflow: {workflow_id}")
        return _workflow_from_value(value)

    @staticmethod
    def _get_job(projection: _Projection, job_id: str) -> ToolJob:
        value = projection["jobs"].get(job_id)
        if value is None:
            raise ExecutionError(f"unknown job: {job_id}")
        return _job_from_value(value)

    def _require_current_lease(
        self,
        projection: _Projection,
        lease: Lease,
        *,
        require_unexpired: bool,
    ) -> Lease:
        current_value = projection["leases"].get(lease.workflow_id)
        if current_value is None:
            raise FencingError("worker lease is no longer current")
        current = _lease_from_value(current_value)
        if current != lease:
            raise FencingError("worker lease has been fenced by a newer owner")
        if require_unexpired and _parse_timestamp(current.expires_at) <= _aware(
            self.clock()
        ):
            raise LeaseError("worker lease has expired")
        return current

    def _stale_reason(
        self,
        projection: _Projection,
        lease: Lease,
        workflow: Workflow,
        expected_epoch: int,
        *,
        require_unexpired: bool,
    ) -> str | None:
        current_value = projection["leases"].get(lease.workflow_id)
        if current_value is None:
            return "worker lease is no longer current"
        current = _lease_from_value(current_value)
        if current != lease:
            return "worker lease has been fenced by a newer owner"
        if require_unexpired and _parse_timestamp(current.expires_at) <= _aware(
            self.clock()
        ):
            return "worker lease has expired"
        if expected_epoch != workflow.intent_epoch:
            return "result belongs to a stale intent epoch"
        if lease.intent_epoch != workflow.intent_epoch:
            return "worker lease belongs to a stale intent epoch"
        return None

    def _assert_current_authority(
        self,
        projection: _Projection,
        lease: Lease,
        workflow: Workflow,
        expected_epoch: int,
    ) -> None:
        reason = self._stale_reason(
            projection,
            lease,
            workflow,
            expected_epoch,
            require_unexpired=True,
        )
        if reason is not None:
            raise FencingError(reason)

    def _record_late_result(
        self,
        projection: _Projection,
        job: ToolJob,
        result: JsonValue,
        *,
        result_id: str | None,
        reason: str,
        fence_token: int | None,
        intent_epoch: int,
    ) -> ToolJob:
        key = (
            result_id
            or f"late-{_digest({'jobId': job.job_id, 'result': result, 'epoch': intent_epoch})[:32]}"
        )
        _validate_identifier(key, "result_id")
        for existing in job.late_results:
            if existing.result_id == key:
                if existing.result != result:
                    raise IdempotencyConflict(
                        f"result ID {key!r} has different content"
                    )
                return job
        late = LateResult(
            result_id=key,
            job_id=job.job_id,
            intent_epoch=intent_epoch,
            fence_token=fence_token,
            result=result,
            observed_at=_timestamp(self.clock),
            reason=reason,
        )
        updated = _replace_job(
            job,
            updated_at=late.observed_at,
            late_results=job.late_results + (late,),
        )
        projection["jobs"][job.job_id] = updated.as_dict()
        self._event(
            projection,
            "late_result_observed",
            job.workflow_id,
            {"jobId": job.job_id, "resultId": key, "reason": reason},
        )
        return updated

    def _update_workflow_after_job(
        self, projection: _Projection, workflow_id: str
    ) -> None:
        workflow = self._get_workflow(projection, workflow_id)
        jobs = [
            _job_from_value(value)
            for value in projection["jobs"].values()
            if _job_from_value(value).workflow_id == workflow_id
        ]
        active_jobs = [job for job in jobs if job.status != "superseded"]
        if active_jobs and all(job.status == "succeeded" for job in active_jobs):
            status: WorkflowStatus = "succeeded"
        else:
            status = "running"
        updated = _replace_workflow(
            workflow, status=status, updated_at=_timestamp(self.clock)
        )
        projection["workflows"][workflow_id] = updated.as_dict()

    @staticmethod
    def _event(
        projection: _Projection,
        event_type: str,
        workflow_id: str,
        details: Mapping[str, object],
    ) -> None:
        projection["events"].append(
            {
                "details": _json_object(details),
                "type": event_type,
                "workflowId": workflow_id,
            }
        )


def _workflow_from_value(value: object) -> Workflow:
    if not isinstance(value, dict):
        raise ExecutionError("workflow record must be an object")
    status = value.get("status")
    if status not in {
        "queued",
        "running",
        "succeeded",
        "failed",
        "dead_letter",
        "recovery",
    }:
        raise ExecutionError("workflow status is invalid")
    return Workflow(
        workflow_id=_required_string(value, "workflowId"),
        command_id=_required_string(value, "commandId"),
        command=_json_object(value.get("command")),
        intent_epoch=_required_nonnegative_int(value, "intentEpoch"),
        status=cast(WorkflowStatus, status),
        accepted_at=_required_string(value, "acceptedAt"),
        updated_at=_required_string(value, "updatedAt"),
        failure=_optional_string(value.get("failure")),
        recovery_reason=_optional_string(value.get("recoveryReason")),
    )


def _lease_from_value(value: object) -> Lease:
    if not isinstance(value, dict):
        raise ExecutionError("lease record must be an object")
    fence = value.get("fenceToken")
    if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
        raise ExecutionError("lease fence token is invalid")
    return Lease(
        workflow_id=_required_string(value, "workflowId"),
        worker_id=_required_string(value, "workerId"),
        lease_token=_required_string(value, "leaseToken"),
        fence_token=fence,
        intent_epoch=_required_nonnegative_int(value, "intentEpoch"),
        expires_at=_required_string(value, "expiresAt"),
    )


def _job_from_value(value: object) -> ToolJob:
    if not isinstance(value, dict):
        raise ExecutionError("job record must be an object")
    status = value.get("status")
    if status not in {
        "pending",
        "leased",
        "succeeded",
        "failed",
        "dead_letter",
        "superseded",
    }:
        raise ExecutionError("job status is invalid")
    attempts = value.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ExecutionError("job attempts are invalid")
    late_values = value.get("lateResults", [])
    if not isinstance(late_values, list):
        raise ExecutionError("late results must be an array")
    late_results = tuple(_late_result_from_value(item) for item in late_values)
    return ToolJob(
        job_id=_required_string(value, "jobId"),
        workflow_id=_required_string(value, "workflowId"),
        submission_key=_required_string(value, "submissionKey"),
        tool_name=_required_string(value, "toolName"),
        payload=_json_object(value.get("payload")),
        intent_epoch=_required_nonnegative_int(value, "intentEpoch"),
        status=cast(JobStatus, status),
        attempts=attempts,
        accepted_at=_required_string(value, "acceptedAt"),
        updated_at=_required_string(value, "updatedAt"),
        lease_token=_optional_string(value.get("leaseToken")),
        fence_token=_optional_int(value.get("fenceToken")),
        result=_json_value(value.get("result")),
        error=_optional_string(value.get("error")),
        late_results=late_results,
    )


def _late_result_from_value(value: object) -> LateResult:
    if not isinstance(value, dict):
        raise ExecutionError("late result must be an object")
    return LateResult(
        result_id=_required_string(value, "resultId"),
        job_id=_required_string(value, "jobId"),
        intent_epoch=_required_nonnegative_int(value, "intentEpoch"),
        fence_token=_optional_int(value.get("fenceToken")),
        result=_json_value(value.get("result")),
        observed_at=_required_string(value, "observedAt"),
        reason=_required_string(value, "reason"),
    )


def _mutation_from_value(value: object) -> Mutation:
    if not isinstance(value, dict):
        raise ExecutionError("mutation record must be an object")
    fence = value.get("fenceToken")
    if not isinstance(fence, int) or isinstance(fence, bool) or fence < 1:
        raise ExecutionError("mutation fence token is invalid")
    return Mutation(
        mutation_id=_required_string(value, "mutationId"),
        workflow_id=_required_string(value, "workflowId"),
        payload=_json_object(value.get("payload")),
        intent_epoch=_required_nonnegative_int(value, "intentEpoch"),
        fence_token=fence,
        published_at=_required_string(value, "publishedAt"),
    )


def _replace_workflow(workflow: Workflow, **changes: object) -> Workflow:
    values = {
        "workflow_id": workflow.workflow_id,
        "command_id": workflow.command_id,
        "command": workflow.command,
        "intent_epoch": workflow.intent_epoch,
        "status": workflow.status,
        "accepted_at": workflow.accepted_at,
        "updated_at": workflow.updated_at,
        "failure": workflow.failure,
        "recovery_reason": workflow.recovery_reason,
    }
    values.update(changes)
    return Workflow(**values)  # type: ignore[arg-type]


def _replace_job(job: ToolJob, **changes: object) -> ToolJob:
    values = {
        "job_id": job.job_id,
        "workflow_id": job.workflow_id,
        "submission_key": job.submission_key,
        "tool_name": job.tool_name,
        "payload": job.payload,
        "intent_epoch": job.intent_epoch,
        "status": job.status,
        "attempts": job.attempts,
        "accepted_at": job.accepted_at,
        "updated_at": job.updated_at,
        "lease_token": job.lease_token,
        "fence_token": job.fence_token,
        "result": job.result,
        "error": job.error,
        "late_results": job.late_results,
    }
    values.update(changes)
    return ToolJob(**values)  # type: ignore[arg-type]


def _json_object(value: object) -> JsonObject:
    normalized = _json_value(value)
    if not isinstance(normalized, dict):
        raise TypeError("value must be a JSON object")
    return normalized


def _json_value(value: object) -> JsonValue:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded: object = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise TypeError("execution values must be finite JSON values") from error
    if not _is_json_value(decoded):
        raise TypeError("execution values must be JSON values")
    return cast(JsonValue, decoded)


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ExecutionError(f"record field {key!r} must be a non-empty string")
    return item


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExecutionError("optional record string is invalid")
    return value


def _required_nonnegative_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ExecutionError(f"record field {key!r} must be a non-negative integer")
    return item


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExecutionError("optional record integer is invalid")
    return value


def _validate_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters")


def _token(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("token factory returned a boolean")
    token = str(value).strip()
    _validate_identifier(token, "token")
    return token


def _duration(value: timedelta | float) -> timedelta:
    duration = value if isinstance(value, timedelta) else timedelta(seconds=value)
    if duration <= timedelta(0):
        raise ValueError("lease duration must be positive")
    return duration


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution clock must return a timezone-aware timestamp")
    return value.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return _aware(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(clock: Callable[[], datetime]) -> str:
    return _format_timestamp(clock())


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExecutionError("record timestamp is invalid") from error
    return _aware(parsed)


__all__ = [
    "ExecutionError",
    "ExecutionQueue",
    "ExecutionStorage",
    "FencingError",
    "IdempotencyConflict",
    "InvalidTransition",
    "JobStatus",
    "JsonExecutionStorage",
    "JsonObject",
    "JsonValue",
    "LateResult",
    "Lease",
    "LeaseError",
    "LeaseUnavailable",
    "MemoryExecutionStorage",
    "Mutation",
    "ToolJob",
    "Workflow",
    "WorkflowStatus",
]
