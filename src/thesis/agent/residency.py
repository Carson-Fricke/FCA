"""Workspace-resident agent binding with durable session records.

The Thesis agent is a workspace-resident entity, not a per-request
assistant.  An :class:`AgentResidency` binds one agent instance to exactly
one workspace: the context root, working-state directory, and audit scope
all derive from the binding, and the workspace id is frozen at
construction.  Boundary methods never accept a workspace parameter, so a
resident agent structurally cannot operate outside its workspace.

Sessions are durable JSON v1 records persisted through an injected
:class:`ResidencyStore` port, so identity and session state survive chat
sessions and process restarts.  The store is deliberately a small port: a
production Azure adapter delegates to the existing workspace persistence
contracts without changing these shapes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol, runtime_checkable

from thesis.agent.skills import content_hash

WorkspaceId = str
AgentId = str
SessionId = str

OperationKind = Literal[
    "edit_strategy_code",
    "request_backtest",
    "deploy_paper",
    "open_investigation",
]
OperationState = Literal["recorded", "unavailable"]

_SCHEMA_VERSION = 1
_WORKSPACE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS = frozenset(
    ("edit_strategy_code", "request_backtest", "deploy_paper", "open_investigation")
)
_STATES = frozenset(("recorded", "unavailable"))
_LOGICAL_ROOT = "workspace://{workspace_id}"
_WORKING_STATE_ROOT = ".thesis/agent-state"


class ResidencyError(ValueError):
    """Raised when an act would cross or weaken the workspace residency."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def _workspace_id(value: WorkspaceId) -> WorkspaceId:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise ResidencyError("workspace_id must be a safe workspace identifier")
    return value


def _agent_id(value: AgentId, field: str = "agent_id") -> AgentId:
    if not isinstance(value, str) or _AGENT_ID.fullmatch(value) is None:
        raise ResidencyError(f"{field} must be a portable identifier")
    return value


def _stamp(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ResidencyError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def validate_reference(value: str, field: str = "working_state_ref") -> str:
    """Validate one workspace-relative reference used by residency records."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or value.startswith("/")
        or value.startswith("\\")
        or ".." in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ResidencyError(f"{field} must be a safe workspace-relative reference")
    return value


def _reference(value: str, field: str = "working_state_ref") -> str:
    return validate_reference(value, field)


def _resolve_beneath(boundary: Path, relative: str | Path) -> Path:
    relative_path = Path(relative)
    windows_path = PureWindowsPath(relative)
    if (
        relative_path.is_absolute()
        or relative_path.anchor
        or relative_path.drive
        or windows_path.is_absolute()
        or windows_path.anchor
        or windows_path.drive
    ):
        raise ResidencyError("workspace paths must be relative to the context root")
    if ".." in relative_path.parts:
        raise ResidencyError("workspace paths must not contain traversal")
    return boundary.joinpath(*relative_path.parts)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class AgentSession:
    """One durable agent session record bound to exactly one workspace.

    The transcript cursor matches the execution stream's resume semantics:
    a non-negative, non-decreasing replay position.  Working-state
    references are the bounded workspace-relative pointers a restart needs.
    """

    workspace_id: WorkspaceId
    agent_id: AgentId
    session_id: SessionId
    created_at: datetime
    last_active_at: datetime
    transcript_cursor: int = 0
    working_state_refs: tuple[str, ...] = ()
    parent_session_id: SessionId | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        _agent_id(self.agent_id)
        _agent_id(self.session_id, "session_id")
        if self.parent_session_id is not None:
            _agent_id(self.parent_session_id, "parent_session_id")
        created_at = _stamp(self.created_at, "created_at")
        last_active_at = _stamp(self.last_active_at, "last_active_at")
        if last_active_at < created_at:
            raise ResidencyError("last_active_at cannot precede created_at")
        if self.transcript_cursor < 0:
            raise ResidencyError("transcript_cursor must not be negative")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "last_active_at", last_active_at)
        refs: list[str] = []
        for reference in self.working_state_refs:
            normalized = _reference(reference)
            if normalized not in refs:
                refs.append(normalized)
        object.__setattr__(self, "working_state_refs", tuple(refs))

    def advance(
        self,
        *,
        transcript_cursor: int | None = None,
        working_state_refs: tuple[str, ...] | None = None,
        at: datetime,
    ) -> AgentSession:
        """Return the next session snapshot without mutating this record."""
        at = _stamp(at, "at")
        if at < self.created_at:
            raise ResidencyError("session activity cannot precede created_at")
        if at < self.last_active_at:
            raise ResidencyError("session activity cannot rewind last_active_at")
        cursor = (
            self.transcript_cursor if transcript_cursor is None else transcript_cursor
        )
        if cursor < 0:
            raise ResidencyError("transcript_cursor must not be negative")
        if cursor < self.transcript_cursor:
            raise ResidencyError(
                "transcript_cursor is a non-decreasing resume position"
            )
        refs = (
            self.working_state_refs
            if working_state_refs is None
            else working_state_refs
        )
        return replace(
            self,
            transcript_cursor=cursor,
            working_state_refs=tuple(refs),
            last_active_at=at,
        )

    @property
    def schema_version(self) -> int:
        """Return the durable document schema version."""
        return _SCHEMA_VERSION

    def as_dict(self) -> dict[str, object]:
        """Return the canonical JSON v1 document values."""
        return {
            "agentId": self.agent_id,
            "createdAt": _iso(self.created_at),
            "lastActiveAt": _iso(self.last_active_at),
            "parentSessionId": self.parent_session_id,
            "schemaVersion": self.schema_version,
            "sessionId": self.session_id,
            "transcriptCursor": self.transcript_cursor,
            "workingStateRefs": list(self.working_state_refs),
            "workspaceId": self.workspace_id,
        }

    @property
    def document_json(self) -> str:
        """Return the deterministic JSON v1 serialization of this session."""
        return _canonical_json(self.as_dict())

    @property
    def document_hash(self) -> str:
        """Return the content digest of the canonical document."""
        return content_hash(self.document_json)


@dataclass(frozen=True, slots=True)
class OperationAuditRecord:
    """One immutable audit record for a resident agent operation.

    Every recorded act carries its operation id, the bound workspace, both
    stamps, and the digest of the declared inputs.  An ``unavailable``
    record is a fail-closed outcome for a backing service that does not
    exist yet; it carries a reason and never fabricates a result.
    """

    operation_id: str
    workspace_id: WorkspaceId
    operation: OperationKind
    status: OperationState
    inputs_digest: str
    created_at: datetime
    recorded_at: datetime
    session_id: SessionId
    reason: str | None = None
    route_ref: str | None = None
    detail_ref: str | None = None

    def __post_init__(self) -> None:
        _agent_id(self.operation_id, "operation_id")
        _workspace_id(self.workspace_id)
        if self.operation not in _OPERATIONS:
            raise ResidencyError(f"unsupported operation kind: {self.operation!r}")
        if self.status not in _STATES:
            raise ResidencyError(f"unsupported operation state: {self.status!r}")
        if not isinstance(self.inputs_digest, str) or not _DIGEST.fullmatch(
            self.inputs_digest
        ):
            raise ResidencyError("inputs_digest must be a lowercase SHA-256 hash")
        created_at = _stamp(self.created_at, "created_at")
        recorded_at = _stamp(self.recorded_at, "recorded_at")
        if recorded_at < created_at:
            raise ResidencyError("recorded_at cannot precede created_at")
        if self.status == "unavailable" and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ResidencyError("an unavailable operation requires a reason")
        if self.status == "recorded" and self.reason is not None:
            raise ResidencyError("a recorded operation carries no reason")
        for field in ("route_ref", "detail_ref"):
            value = getattr(self, field)
            if value is not None:
                _reference(value, field)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "recorded_at", recorded_at)

    def as_dict(self) -> dict[str, object]:
        """Return the canonical audit document values."""
        return {
            "createdAt": _iso(self.created_at),
            "detailRef": self.detail_ref,
            "inputsDigest": self.inputs_digest,
            "operation": self.operation,
            "operationId": self.operation_id,
            "reason": self.reason,
            "recordedAt": _iso(self.recorded_at),
            "routeRef": self.route_ref,
            "sessionId": self.session_id,
            "status": self.status,
            "workspaceId": self.workspace_id,
        }


@runtime_checkable
class ResidencyStore(Protocol):
    """Injected persistence port for durable sessions and audit records."""

    def load(
        self, workspace_id: WorkspaceId, agent_id: AgentId
    ) -> AgentSession | None: ...

    def save(self, session: AgentSession) -> AgentSession: ...

    def append_audit(self, record: OperationAuditRecord) -> OperationAuditRecord: ...

    def list_audit(
        self, workspace_id: WorkspaceId
    ) -> tuple[OperationAuditRecord, ...]: ...


def _validate_root(value: str | Path) -> str:
    if isinstance(value, Path):
        value = value.as_posix()
    if not isinstance(value, str) or not value.strip():
        raise ResidencyError("workspace_root must be a non-empty root reference")
    if len(value) > 2048:
        raise ResidencyError("workspace_root reference is too large")
    return value


def _session_for(
    session: AgentSession,
    workspace_id: WorkspaceId,
    agent_id: AgentId,
) -> AgentSession:
    if not isinstance(session, AgentSession):
        raise ResidencyError("residency state must be an AgentSession record")
    if session.workspace_id != workspace_id or session.agent_id != agent_id:
        raise ResidencyError("stored session identity does not match the binding")
    return session


class AgentResidency:
    """One agent instance bound to exactly one workspace.

    The workspace id, agent id, and context root are frozen at
    construction: assignment raises and no boundary method accepts a
    workspace parameter.  The working-state directory and audit scope
    derive from the binding.
    """

    __slots__ = (
        "_agent_id",
        "_clock",
        "_id_factory",
        "_session",
        "_store",
        "_workspace_id",
        "_workspace_root",
    )

    def __setattr__(self, name: str, value: object) -> None:
        raise ResidencyError("agent residency identity is frozen at construction")

    def __delattr__(self, name: str) -> None:
        raise ResidencyError("agent residency identity is frozen at construction")

    def __init__(
        self,
        *,
        store: ResidencyStore,
        workspace_id: WorkspaceId,
        agent_id: AgentId,
        workspace_root: str | Path,
        session: AgentSession,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        session = _session_for(session, workspace_id, agent_id)
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_workspace_id", _workspace_id(workspace_id))
        object.__setattr__(self, "_agent_id", _agent_id(agent_id))
        object.__setattr__(self, "_workspace_root", _validate_root(workspace_root))
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "_id_factory", id_factory)

    @classmethod
    def bind(
        cls,
        store: ResidencyStore,
        *,
        workspace_id: WorkspaceId,
        agent_id: AgentId,
        workspace_root: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> AgentResidency:
        """Load the durable session for this binding or create a fresh one."""
        workspace_id = _workspace_id(workspace_id)
        agent_id = _agent_id(agent_id)
        session = store.load(workspace_id, agent_id)
        if session is not None:
            _session_for(session, workspace_id, agent_id)
        if session is None:
            now = _stamp(clock(), "clock")
            session = AgentSession(
                workspace_id=workspace_id,
                agent_id=agent_id,
                session_id=_agent_id(id_factory(), "session_id"),
                created_at=now,
                last_active_at=now,
            )
            session = store.save(session)
        return cls(
            store=store,
            workspace_id=workspace_id,
            agent_id=agent_id,
            workspace_root=workspace_root,
            session=session,
            clock=clock,
            id_factory=id_factory,
        )

    @property
    def workspace_id(self) -> WorkspaceId:
        """Return the one workspace this residency is bound to."""
        return self._workspace_id

    @property
    def agent_id(self) -> AgentId:
        """Return the durable agent identity."""
        return self._agent_id

    @property
    def context_root(self) -> str:
        """Return the workspace context root derived from the binding."""
        return self._workspace_root

    @property
    def working_state_dir(self) -> str:
        """Return the agent's working-state directory beneath the root."""
        return f"{_WORKING_STATE_ROOT}/{self._agent_id}"

    @property
    def audit_scope(self) -> str:
        """Return the audit scope identifier derived from the binding."""
        return f"workspace:{self._workspace_id}"

    @property
    def session(self) -> AgentSession:
        """Return the current durable session snapshot."""
        return self._session

    def now(self) -> datetime:
        """Return the current injected clock stamp."""
        return self._clock()

    def new_id(self) -> str:
        """Return a fresh identifier from the injected id factory."""
        return self._id_factory()

    def working_state_path(self) -> Path:
        """Return the working-state directory under the context root."""
        return _resolve_beneath(Path(self._workspace_root), self.working_state_dir)

    def resolve_path(self, relative: str | Path) -> Path:
        """Resolve a path only when it stays inside the bound context root."""
        return _resolve_beneath(Path(self._workspace_root), relative)

    def assert_same_workspace(self, workspace_id: WorkspaceId) -> None:
        """Raise when an act references a workspace outside the residency."""
        if workspace_id != self._workspace_id:
            raise ResidencyError(
                f"act references workspace {_workspace_id(workspace_id)!r} "
                f"outside residency {self._workspace_id!r}"
            )

    def checkpoint(
        self,
        *,
        transcript_cursor: int | None = None,
        working_state_refs: tuple[str, ...] | None = None,
    ) -> AgentSession:
        """Persist the next session snapshot and adopt it as current state."""
        next_session = self._session.advance(
            transcript_cursor=transcript_cursor,
            working_state_refs=working_state_refs,
            at=self._clock(),
        )
        next_session = self._store.save(next_session)
        object.__setattr__(self, "_session", next_session)
        return next_session

    def record_audit(
        self,
        *,
        operation_id: str,
        operation: OperationKind,
        status: OperationState,
        inputs_digest: str,
        created_at: datetime,
        reason: str | None = None,
        route_ref: str | None = None,
        detail_ref: str | None = None,
    ) -> OperationAuditRecord:
        """Append one immutable audit record for this workspace residency."""
        if self._session.workspace_id != self._workspace_id:
            raise ResidencyError("session and residency workspaces disagree")
        record = OperationAuditRecord(
            operation_id=_agent_id(operation_id, "operation_id"),
            workspace_id=self._workspace_id,
            operation=operation,
            status=status,
            inputs_digest=inputs_digest,
            created_at=_stamp(created_at, "created_at"),
            recorded_at=self._clock(),
            session_id=self._session.session_id,
            reason=reason,
            route_ref=route_ref,
            detail_ref=detail_ref,
        )
        return self._store.append_audit(record)

    def audit_trail(self) -> tuple[OperationAuditRecord, ...]:
        """Return the workspace audit records in store order."""
        return self._store.list_audit(self._workspace_id)


def resume(
    store: ResidencyStore,
    workspace_id: WorkspaceId,
    agent_id: AgentId,
    *,
    workspace_root: str | Path | None = None,
    clock: Callable[[], datetime] = _utc_now,
    id_factory: Callable[[], str] = _new_id,
) -> AgentResidency:
    """Restore the last durable session for a restart, or create fresh.

    Matching the execution stream's cursor/resume semantics, the restored
    residency carries the stored transcript cursor and working-state
    references.  Without an explicit filesystem root the residency binds to
    the logical workspace root, delegating storage to the workspace
    persistence contracts.
    """
    workspace_id = _workspace_id(workspace_id)
    root = (
        _LOGICAL_ROOT.format(workspace_id=workspace_id)
        if workspace_root is None
        else workspace_root
    )
    return AgentResidency.bind(
        store,
        workspace_id=workspace_id,
        agent_id=agent_id,
        workspace_root=root,
        clock=clock,
        id_factory=id_factory,
    )


__all__ = [
    "AgentId",
    "AgentResidency",
    "AgentSession",
    "OperationAuditRecord",
    "OperationKind",
    "OperationState",
    "ResidencyError",
    "ResidencyStore",
    "SessionId",
    "WorkspaceId",
    "resume",
    "validate_reference",
]
