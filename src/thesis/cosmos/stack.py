"""Compose the Cosmos-backed chat + timeline stack from environment settings."""

from __future__ import annotations

from thesis.chat.service import ChatService
from thesis.cosmos.chunks import ChunkedPayloadStore
from thesis.cosmos.client import (
    CATALOG_CONTAINER,
    EXECUTIONS_CONTAINER,
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

    One authoritative catalog container holds records, messages, events, and
    the per-chat sequence counters. The returned ``CosmosTimeline`` is the
    query facade over that container, served by the unified timeline stream.
    """
    client = open_cosmos_client(settings)
    database = ensure_database(client, settings)
    catalog_container = database.get_container_client(CATALOG_CONTAINER)
    chunks = ChunkedPayloadStore(catalog_container)
    timeline = CosmosTimeline(catalog_container, chunks=chunks)
    catalog = CosmosChatPersistence(catalog_container, timeline=timeline)
    events = CosmosExecutionEventStore(
        timeline,
        scope_for_execution=catalog.scope_for_execution,
        require_active_conversation=catalog.require_active_conversation,
    )
    return ChatService(persistence=catalog, events=events), timeline


def open_execution_store(
    settings: CosmosSettings | None = None,
) -> CosmosExecutionStore:
    """Open the Cosmos-backed execution store over the executions container.

    One durable document per workflow execution (REQ-B5); the container is
    provisioned by the existing local/deployed stack bootstrap.
    """
    client = open_cosmos_client(settings)
    database = ensure_database(client, settings)
    return CosmosExecutionStore(database.get_container_client(EXECUTIONS_CONTAINER))


__all__ = ["open_cosmos_chat_stack", "open_execution_store"]
