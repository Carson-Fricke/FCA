from __future__ import annotations

import dataclasses

import pytest

from thesis.experiments import (
    AttemptConflictError,
    AttemptKind,
    AttemptLedger,
    AttemptRecord,
    InfluenceLedger,
    InfluenceModifier,
    InfluenceRecord,
    InfluenceSource,
    LedgerError,
    LedgerIntegrityError,
    classify_influence,
    verify_chain,
)

NOW = "2026-01-01T00:00:00.000000Z"
CONTRACT = "a" * 64


def _attempt(
    attempt_id: str,
    *,
    description: str = "Generated candidate from the first sweep.",
    kind: AttemptKind = AttemptKind.CANDIDATE,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=attempt_id,
        contract_sha256=CONTRACT,
        kind=kind,
        description=description,
        actor="agent-1",
        observed_at=NOW,
        payload={"sweep": "s1", "parameters": {"fast": 5}},
    )


def _influence(
    influence_id: str,
    attempt_id: str = "attempt-1",
    *,
    modifier: InfluenceModifier = InfluenceModifier.NONE,
    payload: dict[str, object] | None = None,
) -> InfluenceRecord:
    return InfluenceRecord(
        influence_id=influence_id,
        attempt_id=attempt_id,
        source=InfluenceSource.MODEL,
        modifier=modifier,
        decision="Selected candidate-2 for validation.",
        declared_by="agent-1",
        declared_at=NOW,
        payload=payload if payload is not None else {},
    )


def test_attempt_ledger_appends_a_verifiable_hash_chain() -> None:
    ledger = AttemptLedger()
    first = ledger.append(_attempt("attempt-1"))
    second = ledger.append(_attempt("attempt-2"))
    third = ledger.append(
        _attempt(
            "attempt-3",
            kind=AttemptKind.HOLDOUT_LOOK,
            description="First look at the holdout endpoint.",
        )
    )
    assert [entry.sequence for entry in ledger.entries()] == [1, 2, 3]
    assert first.previous_hash == "0" * 64
    assert second.previous_hash == first.entry_hash
    assert third.previous_hash == second.entry_hash
    ledger.verify()
    verify_chain(ledger.entries())


def test_reappending_identical_content_is_idempotent() -> None:
    ledger = AttemptLedger()
    record = _attempt("attempt-1")
    first = ledger.append(record)
    again = ledger.append(_attempt("attempt-1"))
    assert again is first
    assert len(ledger.entries()) == 1


def test_reusing_an_attempt_id_for_new_content_is_rejected() -> None:
    ledger = AttemptLedger()
    ledger.append(_attempt("attempt-1"))
    with pytest.raises(AttemptConflictError):
        ledger.append(_attempt("attempt-1", description="A different description."))


def test_tampering_with_a_record_breaks_the_chain() -> None:
    ledger = AttemptLedger()
    ledger.append(_attempt("attempt-1"))
    ledger.append(_attempt("attempt-2"))
    tampered = list(ledger.entries())
    rewritten = dataclasses.replace(
        tampered[0].record, description="Silently rewritten history."
    )
    tampered[0] = dataclasses.replace(tampered[0], record=rewritten)
    with pytest.raises(LedgerIntegrityError):
        verify_chain(tampered)


def test_tampering_with_the_linkage_breaks_the_chain() -> None:
    ledger = AttemptLedger()
    ledger.append(_attempt("attempt-1"))
    ledger.append(_attempt("attempt-2"))
    tampered = list(ledger.entries())
    tampered[1] = dataclasses.replace(tampered[1], previous_hash="f" * 64)
    with pytest.raises(LedgerIntegrityError):
        verify_chain(tampered)


def test_sequence_gaps_are_detected() -> None:
    ledger = AttemptLedger()
    ledger.append(_attempt("attempt-1"))
    entries = ledger.entries()
    forged = dataclasses.replace(entries[0], sequence=7)
    with pytest.raises(LedgerIntegrityError):
        verify_chain((forged,))


def _force_set(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_entries_are_immutable_snapshots() -> None:
    ledger = AttemptLedger()
    entry = ledger.append(_attempt("attempt-1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        _force_set(entry, "sequence", 9)
    record = entry.record
    assert isinstance(record, AttemptRecord)
    with pytest.raises(dataclasses.FrozenInstanceError):
        _force_set(record, "description", "mutated")


def test_influence_classification_is_deterministic() -> None:
    for source in InfluenceSource:
        assert (
            classify_influence(source, provenance_known=False, post_window=False)
            is InfluenceModifier.UNKNOWN
        )
        assert (
            classify_influence(source, provenance_known=True, post_window=True)
            is InfluenceModifier.EXPOSED
        )
        assert (
            classify_influence(source, provenance_known=True, post_window=False)
            is InfluenceModifier.NONE
        )


def test_unknown_and_exposed_influences_require_a_rationale() -> None:
    with pytest.raises(LedgerError):
        _influence("influence-1", modifier=InfluenceModifier.UNKNOWN)
    with pytest.raises(LedgerError):
        _influence("influence-2", modifier=InfluenceModifier.EXPOSED)
    with pytest.raises(LedgerError):
        _influence(
            "influence-4",
            modifier=InfluenceModifier.UNKNOWN,
            payload={"rationale": "   "},
        )
    record = _influence(
        "influence-3",
        modifier=InfluenceModifier.UNKNOWN,
        payload={"rationale": "Retrieval provenance could not be established."},
    )
    assert record.modifier is InfluenceModifier.UNKNOWN


def test_influence_ledger_chains_and_verifies() -> None:
    ledger = InfluenceLedger()
    first = ledger.append(
        _influence(
            "influence-1",
            modifier=InfluenceModifier.EXPOSED,
            payload={"rationale": "Post-window upload steered the choice."},
        )
    )
    second = ledger.append(_influence("influence-2"))
    assert second.previous_hash == first.entry_hash
    ledger.verify()
    assert ledger.get("influence-1") is first
    assert ledger.get("missing") is None


def test_influence_ledger_rejects_conflicting_reuse() -> None:
    ledger = InfluenceLedger()
    ledger.append(_influence("influence-1"))
    with pytest.raises(AttemptConflictError):
        ledger.append(
            _influence(
                "influence-1",
                attempt_id="attempt-9",
                payload={"rationale": "different decision"},
            )
        )


def test_timestamps_normalize_to_utc() -> None:
    record = AttemptRecord(
        attempt_id="attempt-1",
        contract_sha256=CONTRACT,
        kind=AttemptKind.HYPOTHESIS,
        description="Offset timestamp.",
        actor="agent-1",
        observed_at="2026-01-01T01:00:00+01:00",
        payload={},
    )
    assert record.observed_at == NOW
