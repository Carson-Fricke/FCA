"""Compose FCA storage with the same explicit backend as the runtime."""

from thesis.persistence.submissions import SubmissionStore
from thesis_runtime.adapters.persistence import persistence_backend, postgres_store


def open_submission_store() -> SubmissionStore:
    if persistence_backend() == "postgres":
        from thesis.persistence.postgres import PostgresSubmissionStore

        return PostgresSubmissionStore(postgres_store())
    from thesis.persistence.cosmos_submissions import (
        open_submission_store as open_cosmos,
    )

    return open_cosmos()
