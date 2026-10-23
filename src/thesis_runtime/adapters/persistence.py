"""Select the configured persistence backend at the composition boundary."""

import os
from functools import lru_cache

from thesis_runtime.adapters.cosmos.stack import CosmosRuntimePersistence
from thesis_runtime.adapters.postgres.stack import PostgresRuntimePersistence
from thesis_runtime.adapters.postgres.store import PostgresStore


def persistence_backend() -> str:
    backend = os.environ.get("THESIS_PERSISTENCE", "cosmos").strip().lower()
    if backend not in {"cosmos", "postgres"}:
        raise ValueError("THESIS_PERSISTENCE must be cosmos or postgres")
    return backend


@lru_cache(maxsize=1)
def postgres_store() -> PostgresStore:
    dsn = os.environ.get("THESIS_POSTGRES_DSN", "")
    if not dsn:
        raise ValueError("THESIS_POSTGRES_DSN is required for PostgreSQL persistence")
    return PostgresStore(dsn)


def open_runtime_persistence() -> CosmosRuntimePersistence | PostgresRuntimePersistence:
    if persistence_backend() == "postgres":
        from thesis_runtime.adapters.postgres.stack import open_postgres_runtime

        return open_postgres_runtime(postgres_store())
    from thesis_runtime.adapters.cosmos.stack import (
        open_runtime_persistence as open_cosmos,
    )

    return open_cosmos()
