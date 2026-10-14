"""Cosmos DB persistence for the Thesis conversation timeline."""

from thesis_runtime.adapters.cosmos.client import (
    CATALOG_CONTAINER,
    DEFAULT_DATABASE,
    DEFAULT_ENDPOINT,
    EMULATOR_KEY,
    CosmosSettings,
    ensure_database,
    open_cosmos_client,
)
from thesis_runtime.adapters.cosmos.events import CosmosExecutionEventStore
from thesis_runtime.adapters.cosmos.execution_store import CosmosExecutionStore
from thesis_runtime.adapters.cosmos.records import CosmosChatPersistence
from thesis_runtime.adapters.cosmos.timeline import (
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
    "CosmosExecutionStore",
    "CosmosSettings",
    "CosmosTimeline",
    "DEFAULT_DATABASE",
    "DEFAULT_ENDPOINT",
    "EMULATOR_KEY",
    "KIND_COUNTER",
    "KIND_EVENT",
    "KIND_EXECUTION",
    "KIND_MESSAGE",
    "ROOT_THREAD",
    "ensure_database",
    "open_cosmos_client",
]
