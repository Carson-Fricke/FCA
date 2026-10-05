"""Cosmos DB persistence for the Thesis conversation timeline."""

from thesis.cosmos.client import (
    CATALOG_CONTAINER,
    DEFAULT_DATABASE,
    DEFAULT_ENDPOINT,
    EMULATOR_KEY,
    EXECUTIONS_CONTAINER,
    CosmosSettings,
    ensure_database,
    open_cosmos_client,
)
from thesis.cosmos.events import CosmosExecutionEventStore
from thesis.cosmos.execution_store import CosmosExecutionStore
from thesis.cosmos.records import CosmosChatPersistence
from thesis.cosmos.timeline import (
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
    "EXECUTIONS_CONTAINER",
    "KIND_COUNTER",
    "KIND_EVENT",
    "KIND_EXECUTION",
    "KIND_MESSAGE",
    "ROOT_THREAD",
    "ensure_database",
    "open_cosmos_client",
]
