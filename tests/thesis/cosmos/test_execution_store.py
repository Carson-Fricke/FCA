"""Tests for the Cosmos-backed execution-store adapter.

There is no live Cosmos dependency here: the adapter is exercised against a
small in-memory stand-in for ``ContainerProxy`` that honors the same point
read/replace semantics (including ETag preconditions) the emulator would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from thesis.cosmos.execution_store import CosmosExecutionStore
from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStoreConflict,
    ExecutionStoreError,
    ExecutionStoreNotFound,
    StoreTransition,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class _FakeContainer:
    """In-memory stand-in for a ContainerProxy partitioned by /executionId."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, object]] = {}
        self._tags = 0

    def _tag(self) -> str:
        self._tags += 1
        return f"etag-{self._tags}"

    def create_item(self, document: dict[str, object]) -> None:
        key = (str(document["executionId"]), str(document["id"]))
        if key in self._items:
            raise CosmosResourceExistsError(message="conflict")
        stored = dict(document)
        stored["_etag"] = self._tag()
        self._items[key] = stored

    def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
        try:
            return dict(self._items[(partition_key, item)])
        except KeyError as error:
            raise CosmosResourceNotFoundError(message="missing") from error

    def replace_item(
        self,
        *,
        item: str,
        body: dict[str, object],
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
    ) -> None:
        key = (str(body["executionId"]), item)
        stored = self._items.get(key)
        if stored is None:
            raise CosmosResourceNotFoundError(message="missing")
        if (
            match_condition is MatchConditions.IfNotModified
            and stored.get("_etag") != etag
        ):
            raise CosmosAccessConditionFailedError(message="etag mismatch")
        replacement = dict(body)
        replacement["_etag"] = self._tag()
        self._items[key] = replacement


def _identity(execution_id: str = "execution-1") -> ExecutionIdentity:
    return ExecutionIdentity(
        execution_id=execution_id,
        workspace_id="workspace-demo",
        chat_id="chat-1",
        workflow_id="hypothesis-development",
        workflow_version="1.0.0",
        requested_by="operator-1",
    )


def _transition(sequence: int) -> StoreTransition:
    return StoreTransition(
        sequence=sequence,
        from_phase="hypothesis_creation",
        to_phase="falsification_trials",
        occurred_at=NOW + timedelta(minutes=sequence),
        intent_epoch=0,
        reason="accepted contract",
        completion={"hypothesis": {"claim": "momentum"}},
    )


def _store() -> tuple[CosmosExecutionStore, _FakeContainer]:
    """Build an adapter over a fresh fake container, typed as a proxy."""
    container = _FakeContainer()
    return CosmosExecutionStore(cast("ContainerProxy", container)), container


def test_create_and_get_round_trip_one_document_per_execution() -> None:
    store, _ = _store()

    created = store.create(_identity(), launch_request={"ideas": "momentum in SPY"})
    stored = store.get("execution-1")

    assert stored == created
    assert stored.launch_request == {"ideas": "momentum in SPY"}
    assert stored.identity.workspace_id == "workspace-demo"


def test_get_raises_for_a_missing_execution() -> None:
    store, _ = _store()

    with pytest.raises(ExecutionStoreNotFound):
        store.get("missing")


def test_create_conflicts_on_a_duplicate_execution() -> None:
    store, _ = _store()
    store.create(_identity())

    with pytest.raises(ExecutionStoreConflict):
        store.create(_identity())


def test_transitions_and_phase_results_persist_across_instances() -> None:
    _, container = _store()
    first = CosmosExecutionStore(cast("ContainerProxy", container))
    first.create(_identity())
    first.append_transition("execution-1", _transition(1))
    first.put_phase_result("execution-1", "falsification_trials", {"trials": []})

    # A fresh adapter over the same container reads the durable state back.
    second = CosmosExecutionStore(cast("ContainerProxy", container))
    stored = second.get("execution-1")

    assert [item.sequence for item in stored.transitions] == [1]
    assert stored.transitions[0].completion == {"hypothesis": {"claim": "momentum"}}
    assert stored.phase_results == {"falsification_trials": {"trials": []}}
    assert stored.revision == 3


def test_expected_revision_conflict_raises() -> None:
    store, _ = _store()
    store.create(_identity())

    with pytest.raises(ExecutionStoreConflict):
        store.append_transition("execution-1", _transition(1), expected_revision=7)


def test_concurrent_etag_mismatch_raises_a_conflict() -> None:
    store, container = _store()
    store.create(_identity())

    # A concurrent writer moves the document between this reader's read and
    # its write: the pinned ETag no longer matches, so the pinned write is
    # refused instead of clobbering.
    stale = container.read_item(
        item="execution-store:execution-1", partition_key="execution-1"
    )
    store.put_phase_result("execution-1", "verdict", {"outcome": "supported"})

    payload = stale["payload"]
    assert isinstance(payload, dict)
    document = ExecutionDocument.from_dict(payload)
    with pytest.raises(ExecutionStoreConflict):
        store._write(stale, document)  # noqa: SLF001 - simulates the interleaving


def test_out_of_sequence_transition_is_rejected_before_any_write() -> None:
    store, _ = _store()
    store.create(_identity())

    with pytest.raises(ExecutionStoreError):
        store.append_transition("execution-1", _transition(3))
