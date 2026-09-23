"""Contract tests for the execution-store port and its in-memory adapter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from thesis.workflows.core.execution_store import (
    ExecutionDocument,
    ExecutionIdentity,
    ExecutionStoreConflict,
    ExecutionStoreError,
    ExecutionStoreNotFound,
    InMemoryExecutionStore,
    StoreTransition,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _identity(execution_id: str = "execution-1") -> ExecutionIdentity:
    return ExecutionIdentity(
        execution_id=execution_id,
        workspace_id="workspace-demo",
        chat_id="chat-1",
        workflow_id="hypothesis-development",
        workflow_version="1.0.0",
        requested_by="operator-1",
    )


def _transition(sequence: int, **overrides: object) -> StoreTransition:
    values: dict[str, object] = {
        "sequence": sequence,
        "from_phase": "hypothesis_creation",
        "to_phase": "falsification_trials",
        "occurred_at": NOW + timedelta(minutes=sequence),
        "intent_epoch": 0,
        "reason": "accepted contract",
        "completion": {"hypothesis": {"claim": "momentum"}},
    }
    values.update(overrides)
    return StoreTransition(**values)  # type: ignore[arg-type]


def test_create_round_trips_identity_and_launch_request() -> None:
    store = InMemoryExecutionStore()

    created = store.create(
        _identity(),
        launch_request={"ideas": "momentum in SPY", "triggerMessageId": "msg-1"},
    )

    assert created.revision == 1
    assert created.launch_request == {
        "ideas": "momentum in SPY",
        "triggerMessageId": "msg-1",
    }
    stored = store.get("execution-1")
    assert stored == created
    assert stored.identity.workflow_id == "hypothesis-development"
    assert stored.transitions == ()
    assert stored.phase_results == {}


def test_create_rejects_a_duplicate_execution() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity())

    with pytest.raises(ExecutionStoreConflict):
        store.create(_identity())


def test_get_raises_for_an_unknown_execution() -> None:
    store = InMemoryExecutionStore()

    with pytest.raises(ExecutionStoreNotFound):
        store.get("missing")


def test_transitions_append_in_sequence_order_and_bump_the_revision() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity())

    first = store.append_transition("execution-1", _transition(1))
    second = store.append_transition(
        "execution-1",
        _transition(2, from_phase="falsification_trials", to_phase="verdict"),
    )

    assert first.revision == 2
    assert second.revision == 3
    stored = store.get("execution-1")
    assert [item.sequence for item in stored.transitions] == [1, 2]
    assert stored.transitions[0].completion == {"hypothesis": {"claim": "momentum"}}
    assert stored.transitions[1].to_phase == "verdict"


def test_transitions_must_follow_the_stored_sequence() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity())
    store.append_transition("execution-1", _transition(1))

    with pytest.raises(ExecutionStoreError):
        store.append_transition("execution-1", _transition(3))


def test_expected_revision_conflict_raises_instead_of_clobbering() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity())
    current = store.append_transition("execution-1", _transition(1))

    with pytest.raises(ExecutionStoreConflict):
        store.append_transition(
            "execution-1",
            _transition(2),
            expected_revision=current.revision - 1,
        )
    with pytest.raises(ExecutionStoreConflict):
        store.put_phase_result(
            "execution-1",
            "falsification_trials",
            {"trials": []},
            expected_revision=1,
        )

    # The rejected writes changed nothing, and the pinned write still applies.
    assert store.get("execution-1").revision == 2
    updated = store.put_phase_result(
        "execution-1",
        "falsification_trials",
        {"trials": []},
        expected_revision=current.revision,
    )
    assert updated.revision == 3
    assert updated.phase_results == {"falsification_trials": {"trials": []}}


def test_phase_results_are_opaque_replacements() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity())
    store.put_phase_result("execution-1", "falsification_trials", {"trials": [1]})

    replaced = store.put_phase_result(
        "execution-1", "falsification_trials", {"trials": [1, 2]}
    )

    assert replaced.phase_results == {"falsification_trials": {"trials": [1, 2]}}
    with pytest.raises(ExecutionStoreError):
        store.put_phase_result("execution-1", "verdict", "not-a-mapping")  # type: ignore
    with pytest.raises(ExecutionStoreError):
        store.put_phase_result("execution-1", " ", {})


def test_document_serialization_round_trips() -> None:
    store = InMemoryExecutionStore()
    store.create(_identity(), launch_request={"ideas": "momentum"})
    store.append_transition("execution-1", _transition(1))
    store.put_phase_result("execution-1", "falsification_trials", {"trials": []})

    payload = store.get("execution-1").as_dict()
    restored = ExecutionDocument.from_dict(payload)

    assert restored == store.get("execution-1")
    assert restored.transitions[0].occurred_at.utcoffset() == timedelta(0)


def test_transition_records_validate_their_shape() -> None:
    with pytest.raises(ExecutionStoreError):
        _transition(1, reason=" ")
    with pytest.raises(ExecutionStoreError):
        _transition(0)
    with pytest.raises(ExecutionStoreError):
        _transition(1, occurred_at=datetime.now(UTC).replace(tzinfo=None))
    with pytest.raises(ExecutionStoreError):
        _transition(1, completion="not-a-mapping")  # type: ignore[arg-type]
