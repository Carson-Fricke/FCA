"""The execution store: one durable document per workflow execution (REQ-B5).

The store is workflow-agnostic: it holds exactly one document per execution —
the identity, the opaque launch request, the ordered transitions each carrying
its completion contract, and the per-phase result artifacts — and treats every
payload as the workflow's own business.  Machine state is rebuilt by folding
the stored transitions, never by replaying the chat event log, so machine
history has a lifecycle independent of chat deletion.

Writes are optimistically concurrent: a document carries a monotonically
increasing ``revision``, and a mutating call may pin ``expected_revision`` to
declare the revision it observed; a mismatch raises
:class:`ExecutionStoreConflict` instead of clobbering a concurrent writer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from thesis_runtime.workflows.graph.contracts import ActivityCompletion

#: Default launch-request payload for an execution created without one.
_EMPTY_REQUEST: Mapping[str, object] = {}


class ExecutionStoreError(ValueError):
    """Raised when an execution-store request is malformed or unknown."""


class ExecutionStoreConflict(ExecutionStoreError):
    """Raised when an optimistic-concurrency expectation fails."""


class ExecutionStoreNotFound(ExecutionStoreError):
    """Raised when an execution document does not exist."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionStoreError(f"{name} must be non-empty text")
    return value.strip()


def _moment(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or (value.utcoffset() is None)
    ):
        raise ExecutionStoreError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _index(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionStoreError(f"{name} must not be negative")
    return value


def _count(value: object, name: str) -> int:
    """Require a decoded integer without a historical default."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionStoreError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Who launched what, where."""

    execution_id: str
    workspace_id: str
    chat_id: str
    workflow_id: str
    workflow_version: str
    requested_by: str

    def __post_init__(self) -> None:
        for name in (
            "execution_id",
            "workspace_id",
            "chat_id",
            "workflow_id",
            "workflow_version",
            "requested_by",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready identity."""
        return {
            "chatId": self.chat_id,
            "executionId": self.execution_id,
            "requestedBy": self.requested_by,
            "workflowId": self.workflow_id,
            "workflowVersion": self.workflow_version,
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExecutionIdentity:
        """Rebuild one identity from its canonical transport form."""
        return cls(
            execution_id=str(payload["executionId"]),
            workspace_id=str(payload["workspaceId"]),
            chat_id=str(payload["chatId"]),
            workflow_id=str(payload["workflowId"]),
            workflow_version=str(payload["workflowVersion"]),
            requested_by=str(payload["requestedBy"]),
        )


@dataclass(frozen=True, slots=True)
class StoreTransition:
    """One ordered workflow transition carrying its completion contract."""

    sequence: int
    from_phase: str
    route: str
    occurred_at: datetime
    intent_epoch: int
    reason: str
    attempt_id: str = field(kw_only=True)
    executor_id: str | None = field(default=None, kw_only=True)
    completion: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ExecutionStoreError("sequence must be an integer")
        if self.sequence < 1:
            raise ExecutionStoreError("transition sequence starts at one")
        object.__setattr__(self, "from_phase", _text(self.from_phase, "from_phase"))
        object.__setattr__(self, "route", _text(self.route, "route"))
        object.__setattr__(
            self, "occurred_at", _moment(self.occurred_at, "occurred_at")
        )
        object.__setattr__(
            self, "intent_epoch", _index(self.intent_epoch, "intent_epoch")
        )
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "attempt_id", _text(self.attempt_id, "attempt_id"))
        if not isinstance(self.completion, Mapping):
            raise ExecutionStoreError("completion must be a mapping")
        try:
            validated = ActivityCompletion(self.route, self.completion)
        except (TypeError, ValueError) as error:
            raise ExecutionStoreError(str(error)) from error
        object.__setattr__(self, "completion", dict(validated.output))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready transition."""
        return {
            "output": dict(self.completion),
            "attemptId": self.attempt_id,
            "executorId": self.executor_id,
            "fromPhase": self.from_phase,
            "intentEpoch": self.intent_epoch,
            "occurredAt": self.occurred_at.astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "reason": self.reason,
            "sequence": self.sequence,
            "route": self.route,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> StoreTransition:
        """Rebuild one transition from its canonical transport form."""
        if any(
            name not in payload
            for name in ("attemptId", "executorId", "route", "output")
        ):
            raise ExecutionStoreError(
                "outdated workflow history: explicit activation, route and output are required"
            )
        occurred_at = payload["occurredAt"]
        try:
            moment = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
        except ValueError as error:
            raise ExecutionStoreError(
                "transition occurredAt must be an ISO-8601 timestamp"
            ) from error
        completion = payload["output"]
        if not isinstance(completion, Mapping):
            raise ExecutionStoreError("completion must be a mapping")
        return cls(
            sequence=_count(payload["sequence"], "sequence"),
            from_phase=str(payload["fromPhase"]),
            route=str(payload["route"]),
            occurred_at=moment,
            intent_epoch=_count(payload["intentEpoch"], "intent_epoch"),
            reason=str(payload["reason"]),
            completion=completion,
            attempt_id=_text(payload["attemptId"], "attemptId"),
            executor_id=None
            if payload["executorId"] is None
            else _text(payload["executorId"], "executorId"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionDocument:
    """The whole durable state of one workflow execution."""

    identity: ExecutionIdentity
    launch_request: Mapping[str, object] = field(default_factory=dict)
    transitions: tuple[StoreTransition, ...] = ()
    phase_results: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExecutionIdentity):
            raise ExecutionStoreError("identity must be an ExecutionIdentity")
        if not isinstance(self.launch_request, Mapping):
            raise ExecutionStoreError("launch_request must be a mapping")
        object.__setattr__(self, "launch_request", dict(self.launch_request))
        object.__setattr__(self, "transitions", tuple(self.transitions))
        results: dict[str, dict[str, object]] = {}
        for phase, artifact in self.phase_results.items():
            if not isinstance(phase, str) or not phase.strip():
                raise ExecutionStoreError("phase result keys must be non-empty text")
            if not isinstance(artifact, Mapping):
                raise ExecutionStoreError(
                    f"the artifact for phase {phase!r} must be a mapping"
                )
            results[phase] = dict(artifact)
        object.__setattr__(self, "phase_results", results)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ExecutionStoreError("revision must be an integer")
        if self.revision < 1:
            raise ExecutionStoreError("revision starts at one")

    @property
    def execution_id(self) -> str:
        """Return the execution's single identity."""
        return self.identity.execution_id

    @property
    def workspace_id(self) -> str:
        """Return the workspace the execution belongs to."""
        return self.identity.workspace_id

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, JSON-ready document."""
        return {
            "identity": self.identity.as_dict(),
            "launchRequest": dict(self.launch_request),
            "phaseResults": {
                phase: dict(artifact) for phase, artifact in self.phase_results.items()
            },
            "revision": self.revision,
            "transitions": [transition.as_dict() for transition in self.transitions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ExecutionDocument:
        """Rebuild one document from its canonical transport form."""
        identity_payload = payload["identity"]
        if not isinstance(identity_payload, Mapping):
            raise ExecutionStoreError("an execution document requires an identity")
        raw_transitions = payload["transitions"]
        if not isinstance(raw_transitions, (list, tuple)):
            raise ExecutionStoreError("transitions must be a list")
        transitions = tuple(StoreTransition.from_dict(item) for item in raw_transitions)
        raw_results = payload["phaseResults"]
        if not isinstance(raw_results, Mapping):
            raise ExecutionStoreError("phaseResults must be a mapping")
        launch_request = payload["launchRequest"]
        if not isinstance(launch_request, Mapping):
            raise ExecutionStoreError("launchRequest must be a mapping")
        return cls(
            identity=ExecutionIdentity.from_dict(identity_payload),
            launch_request=launch_request,
            transitions=transitions,
            phase_results=raw_results,
            revision=_count(payload["revision"], "revision"),
        )


class ExecutionStore(Protocol):
    """Persistence boundary for one durable document per execution."""

    def create(
        self,
        identity: ExecutionIdentity,
        *,
        launch_request: Mapping[str, object] | None = None,
    ) -> ExecutionDocument:
        """Create the document for a new execution."""
        ...

    def get(self, execution_id: str) -> ExecutionDocument:
        """Return one execution's document, or raise when it does not exist."""
        ...

    def append_transition(
        self,
        execution_id: str,
        transition: StoreTransition,
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        """Append one ordered transition and return the updated document."""
        ...

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        """Record (or replace) one phase's result artifact."""
        ...


class InMemoryExecutionStore:
    """Deterministic execution-store adapter for tests and local runs."""

    def __init__(self, documents: Iterable[ExecutionDocument] = ()) -> None:
        self._documents: dict[str, ExecutionDocument] = {}
        for document in documents:
            self._documents[document.execution_id] = document

    def create(
        self,
        identity: ExecutionIdentity,
        *,
        launch_request: Mapping[str, object] | None = None,
    ) -> ExecutionDocument:
        document = ExecutionDocument(
            identity=identity,
            launch_request=dict(launch_request or _EMPTY_REQUEST),
        )
        if document.execution_id in self._documents:
            raise ExecutionStoreConflict(
                f"execution {document.execution_id!r} already exists"
            )
        self._documents[document.execution_id] = document
        return document

    def get(self, execution_id: str) -> ExecutionDocument:
        try:
            return self._documents[execution_id]
        except KeyError as error:
            raise ExecutionStoreNotFound(
                f"execution {execution_id!r} was not found"
            ) from error

    def append_transition(
        self,
        execution_id: str,
        transition: StoreTransition,
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        document = self._checked(execution_id, expected_revision)
        if transition.sequence != len(document.transitions) + 1:
            raise ExecutionStoreError(
                f"transition sequence {transition.sequence} does not follow "
                f"{len(document.transitions)} stored transition(s)"
            )
        updated = replace(
            document,
            transitions=(*document.transitions, transition),
            revision=document.revision + 1,
        )
        self._documents[execution_id] = updated
        return updated

    def put_phase_result(
        self,
        execution_id: str,
        phase: str,
        artifact: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> ExecutionDocument:
        document = self._checked(execution_id, expected_revision)
        name = _text(phase, "phase")
        if not isinstance(artifact, Mapping):
            raise ExecutionStoreError("artifact must be a mapping")
        results = dict(document.phase_results)
        results[name] = dict(artifact)
        updated = replace(
            document, phase_results=results, revision=document.revision + 1
        )
        self._documents[execution_id] = updated
        return updated

    def _checked(
        self, execution_id: str, expected_revision: int | None
    ) -> ExecutionDocument:
        document = self.get(execution_id)
        if expected_revision is not None and expected_revision != document.revision:
            raise ExecutionStoreConflict(
                f"execution {execution_id!r} is at revision {document.revision}, "
                f"not the expected revision {expected_revision}"
            )
        return document


__all__ = [
    "ExecutionDocument",
    "ExecutionIdentity",
    "ExecutionStore",
    "ExecutionStoreConflict",
    "ExecutionStoreError",
    "ExecutionStoreNotFound",
    "InMemoryExecutionStore",
    "StoreTransition",
]
