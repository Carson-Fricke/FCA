"""Cosmos DB composition for durable Thesis conversation state.

Local development uses the Cosmos DB emulator; workspace deployments use a real
Cosmos account.  Both are addressed with the same client, so the persistence
code has no environment branch of its own beyond the connection settings below.

Connection settings come from the environment:

``THESIS_COSMOS_ENDPOINT``
    Account endpoint. Defaults to the emulator (``http://127.0.0.1:8081``).
``THESIS_COSMOS_KEY``
    Account key.  Optional.  A loopback endpoint without a key uses the
    well-known emulator key; a deployed endpoint without a key authenticates
    with ``DefaultAzureCredential`` — the container app's user-assigned managed
    identity, resolved through ``AZURE_CLIENT_ID`` — so the account can keep
    local (key) auth disabled (``infra/modules/thesis-cosmos.bicep``).
``THESIS_COSMOS_DATABASE``
    Database name.  Defaults to ``thesis``.

The local emulator uses loopback HTTP to bypass its TLS buffering defect.
Explicit HTTPS loopback endpoints also accept the emulator's self-signed
certificate; deployed endpoints retain certificate verification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient, DatabaseProxy, PartitionKey
from azure.cosmos.documents import ConnectionMode
from azure.identity import DefaultAzureCredential

#: The public, non-secret key the Cosmos DB emulator always uses.
EMULATOR_KEY = (
    "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4b8mGG"
    "yPMbIZnqyMsEcaGQy67XIw/Jw=="
)
DEFAULT_ENDPOINT = "http://127.0.0.1:8081"
DEFAULT_DATABASE = "thesis"

#: One container holds every workspace-scoped conversation document; messages
#: and events carry the indexed ``chatId``/``seq`` fields that form the
#: unified per-chat timeline.
CATALOG_CONTAINER = "catalog"


def _is_loopback(endpoint: str) -> bool:
    return "localhost" in endpoint or "127.0.0.1" in endpoint


@dataclass(frozen=True, slots=True)
class CosmosSettings:
    """Resolved Cosmos DB connection settings."""

    endpoint: str = DEFAULT_ENDPOINT
    #: ``None`` selects ``DefaultAzureCredential``; a loopback endpoint
    #: therefore defaults to the public emulator key instead.
    key: str | None = EMULATOR_KEY
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
        resolved_endpoint = endpoint or DEFAULT_ENDPOINT
        return cls(
            endpoint=resolved_endpoint,
            key=key or (EMULATOR_KEY if _is_loopback(resolved_endpoint) else None),
            database=database or DEFAULT_DATABASE,
        )


def open_cosmos_client(settings: CosmosSettings | None = None) -> CosmosClient:
    """Open a Cosmos client for the emulator, a keyed account, or Entra ID.

    A configured key authenticates the emulator and key-enabled accounts.
    Without one the deployed runtime authenticates with the ambient
    ``DefaultAzureCredential``, so an account that disables local auth needs no
    stored secret; the identity must hold the Cosmos data-plane contributor
    role that ``infra/modules/thesis-cosmos.bicep`` assigns.
    """
    resolved = settings or CosmosSettings.from_environment()
    credential: str | TokenCredential = (
        resolved.key if resolved.key is not None else DefaultAzureCredential()
    )
    return CosmosClient(
        resolved.endpoint,
        credential=credential,
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
        id=CATALOG_CONTAINER, partition_key=PartitionKey(path="/workspaceId")
    )
    return database


__all__ = [
    "CATALOG_CONTAINER",
    "DEFAULT_DATABASE",
    "DEFAULT_ENDPOINT",
    "EMULATOR_KEY",
    "CosmosSettings",
    "ensure_database",
    "open_cosmos_client",
]
