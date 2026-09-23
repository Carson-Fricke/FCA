"""Compose the Cosmos-backed chat + timeline stack from environment settings."""

from __future__ import annotations

from thesis.chat.service import ChatService
from thesis.cosmos.client import (
    CATALOG_CONTAINER,
    EXECUTIONS_CONTAINER,
    TIMELINE_CONTAINER,
    CosmosSettings,
    ensure_database,
    open_cosmos_client,
)
from thesis.cosmos.events import CosmosExecutionEventStore
from thesis.cosmos.execution_store import CosmosExecutionStore
from thesis.cosmos.records import CosmosChatPersistence
from thesis.cosmos.timeline import CosmosTimeline


def open_cosmos_chat_stack(
    settings: CosmosSettings | None = None,
) -> tuple[ChatService, CosmosTimeline]:
    """Open the durable chat service and its unified per-chat timeline.

    The returned ``ChatService`` persists every record in Cosmos and writes
    execution events to the shared timeline; the returned ``CosmosTimeline`` is
    served by the unified timeline stream.
    """
    client = open_cosmos_client(settings)
    database = ensure_database(client, settings)
    timeline = CosmosTimeline(database.get_container_client(TIMELINE_CONTAINER))
    catalog = CosmosChatPersistence(
        database.get_container_client(CATALOG_CONTAINER), timeline=timeline
    )
    events = CosmosExecutionEventStore(
        timeline, chat_for_execution=catalog.chat_id_for_execution
    )
    return ChatService(persistence=catalog, events=events), timeline


def open_execution_store(
    settings: CosmosSettings | None = None,
) -> CosmosExecutionStore:
    """Open the Cosmos-backed execution store over the executions container.

    One durable document per workflow execution (REQ-B5); the container is
    provisioned by :func:`ensure_database` alongside the chat stack.
    """
    client = open_cosmos_client(settings)
    database = ensure_database(client, settings)
    return CosmosExecutionStore(database.get_container_client(EXECUTIONS_CONTAINER))


__all__ = ["open_cosmos_chat_stack", "open_execution_store"]
