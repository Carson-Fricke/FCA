"""FCA investigation records and application-owned research persistence."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from thesis_runtime.contracts.records import (
    InvalidLifecycleTransition,
    RecordLinks,
    RecordNotFound,
    WorkspaceRecord,
    _validate_id,
    _validate_text,
    _validate_timestamp,
)
from thesis_runtime.conversations.service import ChatService


class InvestigationState(StrEnum):
    OPEN = "open"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Investigation(WorkspaceRecord):
    """A durable research objective spanning chats and research artifacts."""

    investigation_id: str
    title: str
    objective: str
    owner_actor_id: str
    state: InvestigationState = InvestigationState.OPEN
    links: RecordLinks = field(default_factory=RecordLinks)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.investigation_id, "investigation_id")
        _validate_text(self.title, "title", max_length=256)
        _validate_text(self.objective, "objective", max_length=8_192)
        _validate_id(self.owner_actor_id, "owner_actor_id")
        if self.updated_at is not None:
            _validate_timestamp(self.updated_at, "updated_at")
        WorkspaceRecord.__post_init__(self)

    @property
    def chat_ids(self) -> tuple[str, ...]:
        return self.links.chat_ids

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return self.links.references.get("fca.strategy", ())

    @property
    def experiment_ids(self) -> tuple[str, ...]:
        return self.links.references.get("fca.experiment", ())

    @property
    def execution_ids(self) -> tuple[str, ...]:
        return self.links.workflow_execution_ids

    @property
    def job_ids(self) -> tuple[str, ...]:
        return self.links.job_ids


class InvestigationStore(Protocol):
    def save(self, record: Investigation) -> Investigation: ...
    def get(self, workspace_id: str, investigation_id: str) -> Investigation | None: ...
    def list(self, workspace_id: str) -> tuple[Investigation, ...]: ...


class MemoryInvestigationStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], Investigation] = {}

    def save(self, record: Investigation) -> Investigation:
        self._records[record.workspace_id, record.investigation_id] = record
        return record

    def get(self, workspace_id: str, investigation_id: str) -> Investigation | None:
        return self._records.get((workspace_id, investigation_id))

    def list(self, workspace_id: str) -> tuple[Investigation, ...]:
        return tuple(r for (ws, _), r in self._records.items() if ws == workspace_id)


class Investigations:
    """Research lifecycle lives outside the conversation host and its catalog."""

    def __init__(
        self,
        chat: ChatService,
        store: InvestigationStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[str], str] = lambda prefix: (
            f"{prefix}-{uuid.uuid4().hex}"
        ),
    ) -> None:
        self._chat = chat
        self._store = store
        self._clock = clock
        self._id_factory = id_factory

    def create(
        self,
        workspace_id: str,
        *,
        title: str,
        objective: str,
        owner_actor_id: str,
        chat_ids: tuple[str, ...] = (),
        strategy_ids: tuple[str, ...] = (),
        experiment_ids: tuple[str, ...] = (),
    ) -> Investigation:
        for chat_id in chat_ids:
            self._chat.get_chat(workspace_id, chat_id)
        now = self._clock()
        record = Investigation(
            workspace_id=workspace_id,
            created_at=now,
            created_by=owner_actor_id,
            investigation_id=self._id_factory("investigation"),
            title=title,
            objective=objective,
            owner_actor_id=owner_actor_id,
            links=RecordLinks(
                chat_ids=chat_ids,
                references={
                    "fca.strategy": strategy_ids,
                    "fca.experiment": experiment_ids,
                },
            ),
            updated_at=now,
        )
        self._store.save(record)
        for chat_id in chat_ids:
            self._chat.link_resources(
                workspace_id, chat_id, {"fca.investigation": (record.investigation_id,)}
            )
        return record

    def get(self, workspace_id: str, investigation_id: str) -> Investigation:
        record = self._store.get(workspace_id, investigation_id)
        if record is None:
            raise RecordNotFound(f"investigation {investigation_id!r} was not found")
        return record

    def list(self, workspace_id: str) -> tuple[Investigation, ...]:
        return self._store.list(workspace_id)

    def set_state(
        self, workspace_id: str, investigation_id: str, state: InvestigationState
    ) -> Investigation:
        record = self.get(workspace_id, investigation_id)
        if record.state is state:
            return record
        allowed = {
            InvestigationState.OPEN: {
                InvestigationState.PAUSED,
                InvestigationState.COMPLETED,
                InvestigationState.ARCHIVED,
            },
            InvestigationState.PAUSED: {
                InvestigationState.OPEN,
                InvestigationState.ARCHIVED,
            },
            InvestigationState.COMPLETED: {InvestigationState.ARCHIVED},
            InvestigationState.ARCHIVED: set(),
        }
        if state not in allowed[record.state]:
            raise InvalidLifecycleTransition(
                f"investigation cannot move from {record.state.value} to {state.value}"
            )
        return self._store.save(replace(record, state=state, updated_at=self._clock()))
