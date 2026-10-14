"""Compose all runtime adapters over one workspace-partitioned Cosmos catalog."""

from __future__ import annotations

from dataclasses import dataclass

from thesis_runtime.adapters.cosmos.chunks import ChunkedPayloadStore
from thesis_runtime.adapters.cosmos.client import (
    CATALOG_CONTAINER,
    CosmosSettings,
    ensure_database,
    open_cosmos_client,
)
from thesis_runtime.adapters.cosmos.events import CosmosExecutionEventStore
from thesis_runtime.adapters.cosmos.execution_store import CosmosExecutionStore
from thesis_runtime.adapters.cosmos.notifications import CosmosTimelineNotifications
from thesis_runtime.adapters.cosmos.ownership import CosmosOwnershipStore
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.timeline import CosmosTimeline
from thesis_runtime.adapters.cosmos.work import CosmosWorkStore
from thesis_runtime.conversations.service import ChatService


@dataclass(frozen=True)
class CosmosRuntimePersistence:
    chat: ChatService
    timeline: CosmosTimeline
    executions: CosmosExecutionStore
    ownership: CosmosOwnershipStore
    work: CosmosWorkStore


def open_runtime_persistence(
    settings: CosmosSettings | None = None,
) -> CosmosRuntimePersistence:
    """Share the exact container instance so every mutation can fence its owner."""
    database = ensure_database(open_cosmos_client(settings), settings)
    container = database.get_container_client(CATALOG_CONTAINER)
    work = CosmosWorkStore(container)
    timeline = CosmosTimeline(
        container,
        chunks=ChunkedPayloadStore(container),
        work_notify=work.notify,
        notifier=CosmosTimelineNotifications(container),
    )
    catalog = CosmosChatPersistence(container, timeline=timeline)
    events = CosmosExecutionEventStore(
        timeline,
        scope_for_execution=catalog.scope_for_execution,
        require_active_conversation=catalog.require_active_conversation,
    )
    ownership = CosmosOwnershipStore(container)
    return CosmosRuntimePersistence(
        ChatService(
            persistence=catalog,
            events=events,
            ownership=ownership,
            single_process=False,
            timeline=timeline,
        ),
        timeline,
        CosmosExecutionStore(container),
        ownership,
        work,
    )


def open_cosmos_chat_stack(
    settings: CosmosSettings | None = None,
) -> tuple[ChatService, CosmosTimeline]:
    stack = open_runtime_persistence(settings)
    return stack.chat, stack.timeline


def open_execution_store(
    settings: CosmosSettings | None = None,
) -> CosmosExecutionStore:
    return open_runtime_persistence(settings).executions
