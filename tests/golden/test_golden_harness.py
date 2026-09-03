from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harness.core.symbols import Symbol
from harness.core.types import QuoteEvent
from harness.testing.golden import (
    ArtifactCanonicalizer,
    DeterminismError,
    GoldenScenario,
    SyntheticFeed,
    ToyStrategy,
    assert_manifest_equal,
    assert_reproducible,
    compare_manifests,
    run_scenario,
)
from harness.venues import ScriptedStep, ScriptedVenue, VenueCall, VenueOperation

STAMP = datetime(2024, 1, 19, 14, 30, tzinfo=UTC)


def quote(event_id: str, offset: int = 0) -> QuoteEvent:
    return QuoteEvent(
        event_id=event_id,
        timestamp=STAMP + timedelta(microseconds=offset),
        sequence=offset,
        symbol=Symbol("AAPL"),
        bid_price=Decimal("100"),
        bid_quantity=Decimal("10"),
        ask_price=Decimal("101"),
        ask_quantity=Decimal("10"),
        venue="golden",
    )


def scenario(
    *,
    strategy_class: type[ToyStrategy] = ToyStrategy,
    strategy_parameters: dict[str, object] | None = None,
    feed: tuple[QuoteEvent, ...] = (quote("quote-1"),),
) -> GoldenScenario:
    parameters = {
        "order_id": "toy-order",
        "quantity": Decimal("2"),
        **(strategy_parameters or {}),
    }
    return GoldenScenario(
        feed=SyntheticFeed(feed),
        venue_steps=(
            ScriptedStep(
                VenueCall(
                    VenueOperation.SUBMIT,
                    order_id=str(parameters["order_id"]),
                ),
            ),
        ),
        strategy_class=strategy_class,
        strategy_parameters=parameters,
        configuration={"symbols": ["AAPL"], "mode": "golden"},
        start=STAMP,
    )


def test_repeated_run_has_byte_identical_artifacts_and_manifest() -> None:
    first, second = assert_reproducible(scenario())

    assert first.manifest_hash == second.manifest_hash
    assert first.artifact_hashes == second.artifact_hashes
    assert first.dispatch_hash == second.dispatch_hash
    assert first.final_clock == STAMP
    assert first.dispatched_count == 1


def test_synthetic_feed_preserves_explicit_event_order_and_clock() -> None:
    feed = SyntheticFeed((quote("late", 1), quote("early")))

    assert tuple(feed) == feed.events
    assert feed.start == STAMP
    with pytest.raises(ValueError, match="cannot precede"):
        GoldenScenario(
            feed=feed, venue_steps=(), start=STAMP + timedelta(microseconds=1)
        )


def test_canonicalizer_rejects_unordered_set_and_uses_parquet_for_events() -> None:
    with pytest.raises(TypeError, match="unordered set"):
        ArtifactCanonicalizer.json_bytes({"values": {"a", "b"}})

    artifacts = ArtifactCanonicalizer.event_artifacts("feed", (quote("quote"),))

    assert list(artifacts) == ["feed/harness.core.types.events.QuoteEvent.parquet"]
    assert artifacts[next(iter(artifacts))].startswith(b"PAR1")


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        pytest.param(
            lambda values: values.update({"values": {"a", "b"}}),
            "unordered set",
            id="set-order",
        ),
        pytest.param(
            lambda values: values.update(
                {"timestamp": STAMP + timedelta(microseconds=1)}
            ),
            None,
            id="clock",
        ),
        pytest.param(
            lambda values: values.update({"random": 2}),
            None,
            id="random",
        ),
        pytest.param(
            lambda values: values.update({"event_id": "different-id"}),
            None,
            id="id",
        ),
    ],
)
def test_canonicalizer_or_manifest_detects_intentional_nondeterminism(
    mutator, expected_error: str | None
) -> None:
    values: dict[str, object] = {
        "event_id": "stable-id",
        "random": 1,
        "timestamp": STAMP,
    }

    if expected_error is not None:
        mutator(values)
        with pytest.raises(TypeError, match=expected_error):
            ArtifactCanonicalizer.artifacts({"output.json": values})
        return

    first = ArtifactCanonicalizer.artifacts({"output.json": values})
    mutator(values)
    second = ArtifactCanonicalizer.artifacts({"output.json": values})
    first_manifest = ArtifactCanonicalizer.manifest({"seed": 1}, first)
    second_manifest = ArtifactCanonicalizer.manifest({"seed": 1}, second)

    assert not compare_manifests(first_manifest, second_manifest).equal
    with pytest.raises(DeterminismError):
        assert_manifest_equal(first_manifest, second_manifest)


def test_strategy_id_nondeterminism_changes_run_artifact() -> None:
    stable = run_scenario(scenario())
    changed = run_scenario(
        scenario(strategy_parameters={"order_id": "different-order"})
    )

    comparison = compare_manifests(stable, changed)

    assert "artifact:strategy/output.json:sha256" in comparison.differences
    with pytest.raises(DeterminismError):
        assert_manifest_equal(stable, changed)


def test_scripted_venue_is_constructed_for_every_run() -> None:
    first = run_scenario(scenario())
    second = run_scenario(scenario())

    assert isinstance(first.binding.context.venue, ScriptedVenue)
    assert first.binding.context.venue is not second.binding.context.venue
