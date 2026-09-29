"""Persistence boundary for screener definitions and imported results.

The store is keyed by workspace, mirroring the other workspace data stores.
Definitions are content-addressed, so saving the same definition twice is
idempotent; listing returns definitions ordered by id and records ordered by
result id, so every read is deterministic regardless of insertion order.
"""

from __future__ import annotations

from typing import Protocol

from thesis.screening.definitions import ScreenerDefinition
from thesis.screening.runs import ScreeningRecord


class ScreenerStore(Protocol):
    """Read/write boundary for workspace screener artifacts."""

    def save_definition(
        self, workspace_id: str, definition: ScreenerDefinition
    ) -> ScreenerDefinition:
        """Persist and return one definition (idempotent by content address)."""
        ...

    def get_definition(
        self, workspace_id: str, definition_id: str
    ) -> ScreenerDefinition | None:
        """Return one definition, or ``None`` when absent."""
        ...

    def list_definitions(self, workspace_id: str) -> tuple[ScreenerDefinition, ...]:
        """Return every definition for the workspace, ordered by id."""
        ...

    def add_record(self, workspace_id: str, record: ScreeningRecord) -> ScreeningRecord:
        """Persist and return one screening result record."""
        ...

    def get_record(self, workspace_id: str, result_id: str) -> ScreeningRecord | None:
        """Return one record, or ``None`` when absent."""
        ...

    def list_records(self, workspace_id: str) -> tuple[ScreeningRecord, ...]:
        """Return every record for the workspace, ordered by result id."""
        ...


class InMemoryScreenerStore:
    """Local in-memory screener store."""

    def __init__(self) -> None:
        self._definitions: dict[str, dict[str, ScreenerDefinition]] = {}
        self._records: dict[str, dict[str, ScreeningRecord]] = {}

    def save_definition(
        self, workspace_id: str, definition: ScreenerDefinition
    ) -> ScreenerDefinition:
        self._definitions.setdefault(workspace_id, {})[definition.definition_id] = (
            definition
        )
        return definition

    def get_definition(
        self, workspace_id: str, definition_id: str
    ) -> ScreenerDefinition | None:
        return self._definitions.get(workspace_id, {}).get(definition_id)

    def list_definitions(self, workspace_id: str) -> tuple[ScreenerDefinition, ...]:
        return tuple(
            sorted(
                self._definitions.get(workspace_id, {}).values(),
                key=lambda definition: definition.definition_id,
            )
        )

    def add_record(self, workspace_id: str, record: ScreeningRecord) -> ScreeningRecord:
        self._records.setdefault(workspace_id, {})[record.result_id] = record
        return record

    def get_record(self, workspace_id: str, result_id: str) -> ScreeningRecord | None:
        return self._records.get(workspace_id, {}).get(result_id)

    def list_records(self, workspace_id: str) -> tuple[ScreeningRecord, ...]:
        return tuple(
            sorted(
                self._records.get(workspace_id, {}).values(),
                key=lambda record: record.result_id,
            )
        )


__all__ = [
    "InMemoryScreenerStore",
    "ScreenerStore",
]
