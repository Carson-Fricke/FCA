"""Durable timeline ports, independent of databases and HTTP transports."""

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable


class TimelineNotifications(Protocol):
    def subscribe(
        self,
        workspace_id: str,
        chat_id: str,
        on_commit: Callable[[], None] | None = None,
    ) -> Callable[[], None]: ...
    def version(self, workspace_id: str, chat_id: str) -> int: ...
    def wait(
        self, workspace_id: str, chat_id: str, version: int, timeout: float
    ) -> int: ...
    def close(self) -> None: ...


@runtime_checkable
class TimelineReader(Protocol):
    @property
    def commits(self) -> TimelineNotifications: ...
    def latest_sequence(self, *, workspace_id: str, chat_id: str) -> int: ...
    def read_after(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        after_seq: int = 0,
        limit: int | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]: ...
    def read_before(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        before_seq: int,
        limit: int,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]: ...

    def read_receipts(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        after_seq: int,
        through_seq: int,
        limit: int,
        result_types: tuple[str, ...],
        structured_kinds: tuple[str, ...],
        metadata_fields: tuple[str, ...],
        metadata_arrays: tuple[tuple[str, str], ...],
    ) -> list[dict[str, object]]:
        """Read headers of root turns/requests and selected result types.

        Selection is scoped and ordered in storage, before paging. Callers own
        result-type policy, selected metadata and presentation. Full text, result
        content and reasoning are excluded; this port never writes or executes.
        """
        ...


class TimelineWriter(Protocol):
    def append(
        self,
        *,
        chat_id: str,
        workspace_id: str,
        kind: str,
        payload: Mapping[str, object],
        item_id: str,
        execution_id: str | None = None,
        thread_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, object]: ...


@runtime_checkable
class TimelineOutputPrefixes(Protocol):
    """Optional history optimization retaining open output without moving page cursors."""

    def output_prefixes(
        self, *, workspace_id: str, chat_id: str, items: list[dict[str, object]]
    ) -> list[dict[str, object]]: ...


class Timeline(TimelineReader, TimelineWriter, Protocol):
    """A composed timeline used by an application that also executes agents."""
