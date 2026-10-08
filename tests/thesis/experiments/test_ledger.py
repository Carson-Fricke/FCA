"""Attempt-record serialization, ledger replay, and hash-chain verification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest

from thesis.experiments.ledger import (
    AttemptConflictError,
    AttemptKind,
    AttemptLedger,
    AttemptRecord,
    LedgerError,
)

OBSERVED_AT = "2026-09-04T12:00:00.000000Z"

PAYLOAD: Mapping[str, object] = {
    "changed": True,
    "dataTypes": ["bars"],
    "jobId": "run-momentum",
    "studyId": "study-momentum",
}


def _record(
    attempt_id: str = "attempt-execution-1-0001",
    kind: AttemptKind = AttemptKind.CANDIDATE,
    description: str = "discovery attempt study-momentum recorded",
    payload: Mapping[str, object] = PAYLOAD,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=attempt_id,
        contract_sha256="a" * 64,
        kind=kind,
        description=description,
        actor="thesis-workflow",
        observed_at=OBSERVED_AT,
        payload=payload,
    )


def test_record_round_trips_through_its_canonical_document() -> None:
    record = _record()
    restored = AttemptRecord.from_dict(record.canonical_document)
    assert restored == record
    assert restored.content_sha256 == record.content_sha256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {**document, "kind": "not-a-kind"},
        lambda document: {**document, "kind": 7},
        lambda document: {**document, "schema": "unknown"},
        lambda document: {**document, "payload": ["not", "a", "mapping"]},
        lambda document: {**document, "payload": {"nested": object()}},
        lambda document: {**document, "extra": True},
        lambda document: {**document, "attemptId": "not a safe id"},
        lambda document: {**document, "contractSha256": "short"},
        lambda document: {**document, "observedAt": "2026-09-04T12:00:00"},
    ],
)
def test_from_dict_rejects_malformed_or_non_canonical_documents(
    mutate: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    with pytest.raises(LedgerError):
        AttemptRecord.from_dict(mutate(_record().canonical_document))


def test_load_replays_ordered_records_into_the_identical_chain() -> None:
    records = (
        _record(
            attempt_id="attempt-execution-1-a",
            description="discovery attempt study-momentum recorded",
        ),
        _record(
            attempt_id="attempt-execution-1-b",
            kind=AttemptKind.HYPOTHESIS,
            description="model-creation hypothesis momentum-breakout recorded",
        ),
    )
    direct = AttemptLedger()
    for record in records:
        direct.append(record)
    loaded = AttemptLedger.load(records)
    assert loaded.entries() == direct.entries()
    loaded.verify()
    from_documents = AttemptLedger.load(
        [record.canonical_document for record in records]
    )
    assert from_documents.entries() == direct.entries()


def test_load_is_idempotent_and_rejects_conflicting_content() -> None:
    record = _record()
    assert len(AttemptLedger.load((record, record)).entries()) == 1
    with pytest.raises(AttemptConflictError):
        AttemptLedger.load((record, replace(record, description="changed")))
