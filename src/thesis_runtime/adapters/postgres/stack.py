"""Compose the native PostgreSQL runtime over one shared transaction boundary."""

from dataclasses import dataclass

from thesis_runtime.conversations.service import ChatService

from .events import PostgresExecutionEventStore
from .execution_store import PostgresExecutionStore
from .notifications import PostgresNotifications
from .ownership import PostgresOwnershipStore
from .records import PostgresChatPersistence
from .store import PostgresStore
from .timeline import PostgresTimeline
from .work import PostgresWorkStore


@dataclass(frozen=True)
class PostgresRuntimePersistence:
    chat: ChatService
    timeline: PostgresTimeline
    executions: PostgresExecutionStore
    ownership: PostgresOwnershipStore
    work: PostgresWorkStore


def open_postgres_runtime(store: PostgresStore) -> PostgresRuntimePersistence:
    notifications = PostgresNotifications(store)
    timeline = PostgresTimeline(store, notifications)
    catalog = PostgresChatPersistence(store, timeline)
    ownership = PostgresOwnershipStore(store)
    events = PostgresExecutionEventStore(
        timeline, scope_for_execution=catalog.scope_for_execution
    )
    return PostgresRuntimePersistence(
        ChatService(
            persistence=catalog,
            events=events,
            ownership=ownership,
            single_process=False,
            timeline=timeline,
        ),
        timeline,
        PostgresExecutionStore(store),
        ownership,
        PostgresWorkStore(store, notifications),
    )
