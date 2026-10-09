"""Research-mandate derivation from the persisted partition, enforcement, and hashes.

The mandate is derived from an execution's launch request: ``createdAt`` is the
cut-off and ``partition`` holds the day counts. These tests use literal
persisted partitions, so they prove that derivation reads the stored values and
never the launch defaults. Missing or malformed inputs must fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from thesis.experiments import (
    DataWindowPolicy,
    LaunchPartition,
    MandateError,
    PartitionRole,
    ResearchMandate,
    datetime_ns,
    declared_launch_partition,
    derive_launch_mandate,
    derive_mandate,
    mandate_section,
    rfc3339_ns,
)
from thesis.experiments.mandate import rfc3339

WORKSPACE = "workspace-one"
EXECUTION = "execution-1"
CREATED_AT = "2026-09-04T12:00:00Z"
PARTITION: dict[str, object] = {
    "historyDays": 1825,
    "forwardDays": 90,
    "purgeDays": 1,
    "embargoDays": 0,
    "provenance": "launch-v2",
}

DAY_NS = 86_400_000_000_000
HOUR_NS = 3_600_000_000_000
CUTOFF = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CUTOFF_NS = 1_788_523_200_000_000_000
FORWARD_START_NS = 1_780_747_200_000_000_000  # 2026-06-06T12:00:00Z
DEVELOPMENT_START_NS = 1_630_843_200_000_000_000  # 2021-09-05T12:00:00Z
DEVELOPMENT_END_NS = FORWARD_START_NS - DAY_NS  # 2026-06-05T12:00:00Z

FROZEN_SHA256 = "db7e0db4a8fca4b1d68c76111fa894d86da9834608d457e4393c9f801e710027"


def _launch_request(**partition_changes: object) -> dict[str, object]:
    """A launch request carrying the persisted partition with the given changes."""
    return {"createdAt": CREATED_AT, "partition": {**PARTITION, **partition_changes}}


def _mandate(launch_request: dict[str, object] | None = None) -> ResearchMandate:
    return derive_mandate(WORKSPACE, EXECUTION, launch_request or _launch_request())


def _store(launch_request: object) -> SimpleNamespace:
    return SimpleNamespace(
        get=lambda execution_id: SimpleNamespace(launch_request=launch_request)
    )


def _absent_key(execution_id: str) -> object:
    raise KeyError(execution_id)


def _absent_value(execution_id: str) -> object:
    raise ValueError(f"execution {execution_id} was not found")


def test_launch_declares_partition_and_dependency_horizons() -> None:
    declared = declared_launch_partition(
        {
            "historyDays": 365,
            "forwardDays": 60,
            "informationHorizonDays": 3,
            "positionHorizonDays": 7,
            "liquidationDays": 2,
        }
    )
    assert declared == LaunchPartition(365, 60, 7, 2)
    with pytest.raises(MandateError, match="explicitly declare"):
        declared_launch_partition({"historyDays": 365})


def test_partitions_are_derived_from_the_persisted_partition() -> None:
    mandate = _mandate()
    assert mandate.cutoff_ns == CUTOFF_NS
    assert mandate.provenance == "launch-v2"
    development, forward = mandate.partitions
    assert development.role is PartitionRole.DEVELOPMENT
    assert development.start_ns == DEVELOPMENT_START_NS
    assert development.end_ns == DEVELOPMENT_END_NS
    assert development.purged_ns == DAY_NS
    assert development.embargoed_ns == 0
    assert forward.role is PartitionRole.HOLDOUT
    assert forward.start_ns == FORWARD_START_NS
    assert forward.end_ns == CUTOFF_NS
    assert forward.purged_ns == 0
    assert forward.embargoed_ns == 0
    assert mandate.development == development
    assert mandate.forward == forward
    assert mandate.default_window == DataWindowPolicy(
        DEVELOPMENT_START_NS, DEVELOPMENT_END_NS, "development"
    )


def test_derivation_reads_the_persisted_partition_not_the_launch_defaults() -> None:
    changed = _launch_request(
        historyDays=400, forwardDays=30, purgeDays=3, embargoDays=2
    )
    mandate = _mandate(changed)
    development, forward = mandate.partitions
    assert development.start_ns == datetime_ns(CUTOFF - timedelta(days=400))
    assert development.end_ns == datetime_ns(CUTOFF - timedelta(days=33))
    assert development.purged_ns == 3 * DAY_NS
    assert forward.start_ns == datetime_ns(CUTOFF - timedelta(days=30))
    assert forward.end_ns == datetime_ns(CUTOFF - timedelta(days=2))
    assert forward.embargoed_ns == 2 * DAY_NS
    assert mandate.purge_days == 3
    assert mandate.embargo_days == 2


def test_forward_and_development_policies_enforce_both_bounds() -> None:
    mandate = _mandate()
    development = mandate.default_window
    assert development.override is False
    assert development.permits(DEVELOPMENT_START_NS, DEVELOPMENT_END_NS)
    assert not development.permits(DEVELOPMENT_START_NS - 1, DEVELOPMENT_END_NS)
    assert not development.permits(DEVELOPMENT_START_NS, DEVELOPMENT_END_NS + 1)
    assert not development.permits(DEVELOPMENT_START_NS, DEVELOPMENT_START_NS)
    forward = mandate.forward_window
    assert forward == DataWindowPolicy(FORWARD_START_NS, CUTOFF_NS, "forward", True)
    assert forward.override is True
    assert forward.permits(FORWARD_START_NS, CUTOFF_NS)
    assert not forward.permits(FORWARD_START_NS - 1, CUTOFF_NS)
    assert not forward.permits(FORWARD_START_NS, CUTOFF_NS + 1)


def test_development_start_bound_rejects_windows_before_the_history_start() -> None:
    mandate = _mandate()
    assert mandate.window_violation(DEVELOPMENT_START_NS, DEVELOPMENT_END_NS) is None
    violation = mandate.window_violation(
        DEVELOPMENT_START_NS - DAY_NS, DEVELOPMENT_END_NS
    )
    assert violation is not None
    assert rfc3339(DEVELOPMENT_START_NS) in violation
    assert rfc3339(FORWARD_START_NS) in violation


def test_purge_gap_is_excluded_from_development_and_forward() -> None:
    mandate = _mandate()
    gap_inside = DEVELOPMENT_END_NS + 12 * HOUR_NS
    assert mandate.window_violation(DEVELOPMENT_START_NS, gap_inside) is not None
    assert mandate.window_violation(DEVELOPMENT_START_NS, DEVELOPMENT_END_NS) is None
    assert not mandate.forward_window.permits(gap_inside, CUTOFF_NS)
    assert not mandate.default_window.permits(DEVELOPMENT_START_NS, FORWARD_START_NS)


def test_embargo_tail_is_excluded_from_the_forward_window() -> None:
    mandate = _mandate(_launch_request(embargoDays=2))
    embargo_start = CUTOFF_NS - 2 * DAY_NS
    forward = mandate.forward_window
    assert forward.permits(FORWARD_START_NS, embargo_start)
    assert not forward.permits(FORWARD_START_NS, embargo_start + HOUR_NS)
    assert mandate.forward.embargoed_ns == 2 * DAY_NS


def test_override_must_be_a_boolean() -> None:
    with pytest.raises(MandateError, match="override"):
        DataWindowPolicy(
            DEVELOPMENT_START_NS, DEVELOPMENT_END_NS, "development", cast(Any, "yes")
        )


@pytest.mark.parametrize(
    "launch_request",
    [
        {"partition": PARTITION},
        {"createdAt": None, "partition": PARTITION},
        {"createdAt": "", "partition": PARTITION},
        {"createdAt": "not-a-date", "partition": PARTITION},
        {"createdAt": "2026-09-04T12:00:00", "partition": PARTITION},
    ],
    ids=["absent", "null", "empty", "unparseable", "naive"],
)
def test_missing_or_unparseable_created_at_fails_closed(
    launch_request: dict[str, object],
) -> None:
    with pytest.raises(MandateError):
        derive_mandate(WORKSPACE, EXECUTION, launch_request)


def test_a_launch_request_that_is_not_an_object_fails_closed() -> None:
    with pytest.raises(MandateError):
        derive_mandate(WORKSPACE, EXECUTION, cast(Any, None))


@pytest.mark.parametrize(
    "launch_request",
    [
        {"createdAt": CREATED_AT},
        {"createdAt": CREATED_AT, "partition": None},
        {"createdAt": CREATED_AT, "partition": "launch-v2"},
        {
            "createdAt": CREATED_AT,
            "partition": {k: v for k, v in PARTITION.items() if k != "purgeDays"},
        },
        _launch_request(extra=1),
        _launch_request(historyDays=True),
        _launch_request(historyDays="1825"),
        _launch_request(forwardDays=0),
        _launch_request(purgeDays=-1),
        _launch_request(embargoDays=-1),
        _launch_request(embargoDays=90),
        _launch_request(historyDays=91),
        _launch_request(historyDays=36_501),
        _launch_request(provenance="launch-v1"),
    ],
    ids=[
        "absent",
        "null",
        "not-an-object",
        "missing-key",
        "extra-key",
        "boolean-day-count",
        "string-day-count",
        "zero-forward",
        "negative-purge",
        "negative-embargo",
        "embargo-not-shorter-than-forward",
        "history-not-beyond-forward-and-purge",
        "history-beyond-supported-range",
        "unsupported-provenance",
    ],
)
def test_missing_or_malformed_partition_fails_closed(
    launch_request: dict[str, object],
) -> None:
    with pytest.raises(MandateError):
        derive_mandate(WORKSPACE, EXECUTION, launch_request)


@pytest.mark.parametrize(
    "store",
    [
        None,
        SimpleNamespace(get=_absent_key),
        SimpleNamespace(get=_absent_value),
        SimpleNamespace(get=lambda execution_id: SimpleNamespace()),
        SimpleNamespace(
            get=lambda execution_id: SimpleNamespace(
                launch_request={"createdAt": CREATED_AT}
            )
        ),
    ],
    ids=[
        "no-store",
        "key-error",
        "value-error",
        "document-without-launch-request",
        "launch-request-without-partition",
    ],
)
def test_a_missing_execution_document_fails_closed(store: object) -> None:
    with pytest.raises(MandateError):
        derive_launch_mandate(store, workspace_id=WORKSPACE, execution_id=EXECUTION)


def test_derive_launch_mandate_reads_the_persisted_launch_request() -> None:
    store = _store(_launch_request())
    assert (
        derive_launch_mandate(store, workspace_id=WORKSPACE, execution_id=EXECUTION)
        == _mandate()
    )


def test_content_hash_is_stable_and_round_trips() -> None:
    mandate = _mandate()
    assert mandate.content_sha256 == FROZEN_SHA256
    assert mandate.as_dict() == mandate.canonical_document
    restored = ResearchMandate.from_dict(mandate.as_dict())
    assert restored == mandate
    assert restored.content_sha256 == mandate.content_sha256
    other = derive_mandate("workspace-two", EXECUTION, _launch_request())
    assert other.content_sha256 != mandate.content_sha256


def test_purge_and_embargo_values_round_trip_through_the_document() -> None:
    mandate = _mandate(_launch_request(purgeDays=3, embargoDays=2))
    document = mandate.as_dict()
    assert document["purgeDays"] == 3
    assert document["embargoDays"] == 2
    assert ResearchMandate.from_dict(document) == mandate


def _mutate(key: str, value: object) -> Any:
    def apply(document: dict[str, Any]) -> dict[str, Any]:
        return {**document, key: value}

    return apply


def _mutate_partition(index: int, key: str, value: object) -> Any:
    def apply(document: dict[str, Any]) -> dict[str, Any]:
        partitions = [dict(item) for item in document["partitions"]]
        partitions[index][key] = value
        return {**document, "partitions": partitions}

    return apply


@pytest.mark.parametrize(
    "mutate",
    [
        _mutate("purgeDays", 3),
        _mutate("embargoDays", 2),
        _mutate("cutoffNs", CUTOFF_NS + 1),
        _mutate_partition(0, "purgedNs", 0),
        _mutate_partition(1, "embargoedNs", 2 * DAY_NS),
        _mutate("extra", "field"),
        _mutate("schema", "fca-thesis-research-mandate-v1"),
        _mutate("partitions", []),
        _mutate_partition(0, "role", "unknown-role"),
    ],
    ids=[
        "purge-days-changed",
        "embargo-days-changed",
        "cut-off-changed",
        "purge-allowance-changed",
        "embargo-allowance-changed",
        "undeclared-field",
        "schema-changed",
        "partitions-removed",
        "partition-role-unknown",
    ],
)
def test_from_dict_rejects_tampered_or_inconsistent_documents(mutate: Any) -> None:
    document = _mandate().as_dict()
    with pytest.raises(MandateError):
        ResearchMandate.from_dict(mutate(dict(document)))


def test_window_helpers_round_trip_and_reject_naive_or_malformed() -> None:
    for moment in (
        CUTOFF,
        datetime(2021, 9, 5, 12, tzinfo=UTC),
        datetime(1926, 1, 1, 0, 0, 0, 250_000, tzinfo=UTC),
    ):
        assert rfc3339_ns(rfc3339(datetime_ns(moment))) == datetime_ns(moment)
    assert rfc3339(datetime_ns(datetime(1926, 1, 1, tzinfo=UTC))) == (
        "1926-01-01T00:00:00Z"
    )
    with pytest.raises(MandateError):
        datetime_ns(datetime(2026, 9, 4))
    with pytest.raises(MandateError):
        rfc3339_ns("not-a-date")


def test_mandate_section_renders_the_partition_boundaries() -> None:
    section = mandate_section(_mandate())
    assert section.startswith("## Research mandate")
    assert rfc3339(CUTOFF_NS) in section
    assert rfc3339(FORWARD_START_NS) in section
    assert rfc3339(DEVELOPMENT_END_NS) in section
    assert _mandate().content_sha256[:16] in section
