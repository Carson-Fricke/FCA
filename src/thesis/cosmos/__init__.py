"""Cosmos DB persistence for the Thesis conversation timeline."""

from thesis.cosmos.client import (
    CATALOG_CONTAINER,
    DEFAULT_DATABASE,
    DEFAULT_ENDPOINT,
    EMULATOR_KEY,
    TIMELINE_CONTAINER,
    CosmosSettings,
    ensure_database,
    open_cosmos_client,
)
from thesis.cosmos.events import CosmosExecutionEventStore
from thesis.cosmos.records import CosmosChatPersistence
from thesis.cosmos.timeline import (
    KIND_CHAT,
    KIND_COUNTER,
    KIND_EVENT,
    KIND_EXECUTION,
    KIND_MESSAGE,
    ROOT_THREAD,
    CosmosTimeline,
)

__all__ = [
    "CATALOG_CONTAINER",
    "CosmosChatPersistence",
    "CosmosExecutionEventStore",
    "CosmosSettings",
    "CosmosTimeline",
    "DEFAULT_DATABASE",
    "DEFAULT_ENDPOINT",
    "EMULATOR_KEY",
    "KIND_CHAT",
    "KIND_COUNTER",
    "KIND_EVENT",
    "KIND_EXECUTION",
    "KIND_MESSAGE",
    "ROOT_THREAD",
    "TIMELINE_CONTAINER",
    "ensure_database",
    "open_cosmos_client",
]
