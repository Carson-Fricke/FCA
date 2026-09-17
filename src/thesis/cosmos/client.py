"""Cosmos DB composition for durable Thesis conversation state.

Local development uses the Cosmos DB emulator; workspace deployments use a real
Cosmos account.  Both are addressed with the same client, so the persistence
code has no environment branch of its own beyond the connection settings below.

Connection settings come from the environment:

``THESIS_COSMOS_ENDPOINT``
    Account endpoint.  Defaults to the emulator (``https://localhost:8081``).
``THESIS_COSMOS_KEY``
    Account key.  Defaults to the well-known emulator key.
``THESIS_COSMOS_DATABASE``
    Database name.  Defaults to ``thesis``.

When the endpoint is loopback the emulator's self-signed certificate is not
verified, matching the emulator's own guidance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from azure.cosmos import CosmosClient, DatabaseProxy, PartitionKey
from azure.cosmos.documents import ConnectionMode

#: The public, non-secret key the Cosmos DB emulator always uses.
EMULATOR_KEY = (
    "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGG"
    "yPMbIZnqyMsEcaGQy67XIw/Jw=="
)
DEFAULT_ENDPOINT = "https://localhost:8081"
DEFAULT_DATABASE = "thesis"

#: One container holds every chat-scoped timeline item (messages + events).
TIMELINE_CONTAINER = "timeline"
#: One container holds the workspace-scoped records (chats, executions, ...).
CATALOG_CONTAINER = "catalog"


def _is_loopback(endpoint: str) -> bool:
    return "localhost" in endpoint or "127.0.0.1" in endpoint


@dataclass(frozen=True, slots=True)
class CosmosSettings:
    """Resolved Cosmos DB connection settings."""

    endpoint: str = DEFAULT_ENDPOINT
    key: str = EMULATOR_KEY
    database: str = DEFAULT_DATABASE

    @property
    def connection_verify(self) -> bool:
        """Verify TLS unless the endpoint is the local emulator."""
        return not _is_loopback(self.endpoint)

    @classmethod
    def from_environment(cls) -> CosmosSettings:
        """Read settings from the environment, defaulting to the emulator."""
        endpoint = (os.environ.get("THESIS_COSMOS_ENDPOINT") or "").strip()
        key = (os.environ.get("THESIS_COSMOS_KEY") or "").strip()
        database = (os.environ.get("THESIS_COSMOS_DATABASE") or "").strip()
        return cls(
            endpoint=endpoint or DEFAULT_ENDPOINT,
            key=key or EMULATOR_KEY,
            database=database or DEFAULT_DATABASE,
        )


def open_cosmos_client(settings: CosmosSettings | None = None) -> CosmosClient:
    """Open a Cosmos client for the emulator or a real account."""
    resolved = settings or CosmosSettings.from_environment()
    return CosmosClient(
        resolved.endpoint,
        credential=resolved.key,
        connection_verify=resolved.connection_verify,
        # The emulator advertises an internal container IP; gateway mode keeps
        # the client on the configured loopback endpoint.
        connection_mode=ConnectionMode.Gateway,
    )


def ensure_database(
    client: CosmosClient, settings: CosmosSettings | None = None
) -> DatabaseProxy:
    """Create the database and both containers if they do not exist yet."""
    resolved = settings or CosmosSettings.from_environment()
    database = client.create_database_if_not_exists(resolved.database)
    database.create_container_if_not_exists(
        id=TIMELINE_CONTAINER, partition_key=PartitionKey(path="/chatId")
    )
    database.create_container_if_not_exists(
        id=CATALOG_CONTAINER, partition_key=PartitionKey(path="/workspaceId")
    )
    return database


__all__ = [
    "CATALOG_CONTAINER",
    "DEFAULT_DATABASE",
    "DEFAULT_ENDPOINT",
    "EMULATOR_KEY",
    "TIMELINE_CONTAINER",
    "CosmosSettings",
    "ensure_database",
    "open_cosmos_client",
]
