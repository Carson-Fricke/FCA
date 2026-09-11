"""Cross-language parity oracle: pure-Python engine versus the Zig core.

The oracle builds one canonical parity scenario — the deterministic synthetic
benchmark feed written as canonical Parquet lake partitions and replayed
through :class:`~harness.data.replay.LakeReplayFeed`, with a reference
strategy submitting orders — and runs it through:

1. the pure-Python engine (:func:`harness.runtime.backtest.run_backtest`), and
2. the Zig-core-driven runner
   (:func:`harness.runtime.zig_backtest.run_zig_backtest`), which loads the
   native core, drives the same engine composition, and arbitrates every
   kernel operation the stable C ABI v1 exposes — symbol canonicalization,
   deterministic identifier derivation, canonical decimal text, canonical
   trade-row serialization, the Arrow C Data Interface bridge, the native bus
   dispatch-digest identity frame, the execution-report state machine, and
   ``VirtualBook`` ledger accounting.

The oracle asserts byte-identical fills, ledgers, and dispatch hashes between
the engines, pins every manifest field except the engine identity, and
arbitrates the Zig-driven path through the determinism harness machinery
(``ArtifactCanonicalizer`` manifests plus ``assert_manifest_equal``).

When the native library has not been built the native tests skip with the
loader's explicit reason (see ``conftest.py``); the pure determinism-harness
and dispatch-digest tests always run, so the default ``pytest`` stays green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from harness._native import NATIVE_ABI_VERSION, NativeCore, NativeCoreUnavailable
from harness.configuration import BacktestConfig, load_example
from harness.core.clock import SimClock
from harness.core.events import EventBus
from harness.core.symbols import Symbol
from harness.core.types import Fill, QuoteEvent, Side, TradeEvent
from harness.runtime.backtest import (
    BacktestManifest,
    BacktestResult,
    ReplayFixture,
    run_backtest,
    write_synthetic_lake,
)
from harness.runtime.backtest.manifest import ENGINE_ID
from harness.runtime.zig_backtest import (
    ZIG_ENGINE_ID,
    _native_dispatch_digest,
    run_zig_backtest,
    run_zig_backtest_with_evidence,
)
from harness.testing.determinism import (
    ArtifactCanonicalizer,
    DeterminismManifest,
    DeterminismScenario,
    SyntheticFeed,
    assert_manifest_equal,
    assert_reproducible,
)
from harness.venues import ScriptedStep, VenueCall, VenueOperation

_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)

# Native-pinned golden vector from src/harness/native/bus.zig ("dispatch
# digest is byte-identical to the Python bus"): 10,000 quote/trade/fill
# events, timestamps staggered by ``index % 29`` microseconds, sequences
# ``index % 17``, identifiers ``event-{index}``.
_GOLDEN_STAMP_NS: int = 1_705_674_600_000_000_000
_GOLDEN_DISPATCH_DIGEST: str = (
    "949b0dd3fbf95bcb1b93baec06c20e8e5c66ad74e342df01b1e21d9eff769df2"
)


def _configuration() -> BacktestConfig:
    configuration = load_example("backtest")
    assert isinstance(configuration, BacktestConfig)
    return configuration


def _lake_fixture(configuration: BacktestConfig, root: Path) -> ReplayFixture:
    """Write the canonical parity feed as local Parquet lake partitions."""
    return write_synthetic_lake(configuration, root / "parity-lake")


# --------------------------------------------------------------------------
# Native bus dispatch digest golden vector
# --------------------------------------------------------------------------


def test_python_bus_reproduces_the_native_dispatch_golden_vector() -> None:
    base = _EPOCH + timedelta(microseconds=_GOLDEN_STAMP_NS // 1_000)
    bus = EventBus(SimClock(base))
    events = []
    for index in range(10_000):
        stamp = base + timedelta(microseconds=index % 29)
        sequence = index % 17
        event_id = f"event-{index}"
        kind = index % 3
        if kind == 0:
            event: QuoteEvent | TradeEvent | Fill = QuoteEvent(
                event_id=event_id,
                timestamp=stamp,
                sequence=sequence,
                symbol=Symbol("AAPL"),
                bid_price=Decimal("100"),
                bid_quantity=Decimal("1"),
                ask_price=Decimal("101"),
                ask_quantity=Decimal("1"),
            )
        elif kind == 1:
            event = TradeEvent(
                event_id=event_id,
                timestamp=stamp,
                sequence=sequence,
                symbol=Symbol("AAPL"),
                price=Decimal("100"),
                quantity=Decimal("1"),
            )
        else:
            event = Fill(
                event_id=event_id,
                timestamp=stamp,
                sequence=sequence,
                fill_id=event_id,
                order_id=event_id,
                symbol=Symbol("AAPL"),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("100"),
            )
        events.append(event)

    bus.publish_many(events)
    dispatched = bus.drain()

    assert bus.dispatched_count == 10_000
    assert bus.dispatch_hash == _GOLDEN_DISPATCH_DIGEST
    assert _native_dispatch_digest(dispatched) == _GOLDEN_DISPATCH_DIGEST


# --------------------------------------------------------------------------
# Cross-language parity: pure-Python engine versus Zig-driven runner
# --------------------------------------------------------------------------


def _normalized_artifact_json(run: BacktestResult) -> str:
    """Return the run artifact bytes with the engine identity normalized."""
    return run.artifact_json().replace(
        f'"engine_id":"{ZIG_ENGINE_ID}"', f'"engine_id":"{ENGINE_ID}"'
    )


def _assert_manifests_pinned(
    python_manifest: BacktestManifest, zig_manifest: BacktestManifest
) -> None:
    """Pin every manifest field except the engine identity."""
    left = python_manifest.to_json()
    right = zig_manifest.to_json()
    differing = sorted(key for key in left if left[key] != right[key])
    assert differing == ["engine_id"]
    assert right["engine_id"] == ZIG_ENGINE_ID


def test_python_and_zig_engines_agree_byte_for_byte(
    tmp_path: Path, native_core: NativeCore
) -> None:
    configuration = _configuration()
    fixture = _lake_fixture(configuration, tmp_path)
    python_run = run_backtest(configuration, data_source=fixture)
    zig_run, evidence = run_zig_backtest_with_evidence(
        configuration, data_source=fixture
    )

    assert python_run.completed and zig_run.completed
    assert zig_run.dispatch_hash == python_run.dispatch_hash
    assert zig_run.dispatched_count == python_run.dispatched_count
    assert zig_run.fills == python_run.fills
    assert zig_run.outcome_digest() == python_run.outcome_digest()
    assert _normalized_artifact_json(zig_run) == python_run.artifact_json()
    _assert_manifests_pinned(python_run.manifest, zig_run.manifest)
    assert python_run.preflight.to_json() == zig_run.preflight.to_json()

    assert evidence.abi_version == NATIVE_ABI_VERSION
    assert evidence.dispatch_digest == python_run.dispatch_hash
    assert evidence.dispatched_count == python_run.dispatched_count
    assert evidence.trade_row_checks == python_run.preflight.trade_count
    assert evidence.ledger_books == len(configuration.strategy_instances)
    assert evidence.arrow_batch_length > 0
    assert evidence.decimal_checks > 0
    assert evidence.identity_checks > 0
    assert evidence.symbol_checks > 0


def _pinned_manifest(manifest: BacktestManifest) -> dict[str, object]:
    values = dict(manifest.to_json())
    values.pop("engine_id")
    return values


def _parity_artifacts(
    run: BacktestResult,
) -> tuple[DeterminismManifest, dict[str, bytes]]:
    """Canonicalize one Zig-driven run through the determinism machinery."""
    artifacts = dict(ArtifactCanonicalizer.event_artifacts("fills", run.fills))
    artifacts["ledger/portfolio.json"] = ArtifactCanonicalizer.json_bytes(run.portfolio)
    artifacts["ledger/fees.json"] = ArtifactCanonicalizer.json_bytes(run.fee_totals)
    artifacts["ledger/dispatch.json"] = ArtifactCanonicalizer.json_bytes(
        {
            "dispatch_hash": run.dispatch_hash,
            "dispatched_count": run.dispatched_count,
            "market_event_count": run.market_event_count,
            "order_count": run.order_count,
            "status": run.status.value,
        }
    )
    canonical = ArtifactCanonicalizer.artifacts(artifacts)
    manifest = ArtifactCanonicalizer.manifest(
        {
            "manifest": _pinned_manifest(run.manifest),
            "preflight": run.preflight.to_json(),
        },
        canonical,
    )
    return manifest, {artifact.name: artifact.payload for artifact in canonical}


def test_zig_driven_path_is_reproducible_through_the_determinism_machinery(
    tmp_path: Path, native_core: NativeCore
) -> None:
    configuration = _configuration()
    fixture = _lake_fixture(configuration, tmp_path)
    first, _ = run_zig_backtest_with_evidence(configuration, data_source=fixture)
    second = run_zig_backtest(configuration, data_source=fixture)

    first_manifest, first_artifacts = _parity_artifacts(first)
    second_manifest, second_artifacts = _parity_artifacts(second)

    assert_manifest_equal(first_manifest, second_manifest)
    for name in sorted(first_artifacts):
        assert first_artifacts[name] == second_artifacts[name], name
    assert first.dispatch_hash == second.dispatch_hash


# --------------------------------------------------------------------------
# The shared parity scenario through the determinism harness
# --------------------------------------------------------------------------


def test_parity_scenario_is_reproducible_in_the_determinism_harness(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    fixture = _lake_fixture(configuration, tmp_path)
    events, _ = fixture.materialize()
    scenario = DeterminismScenario(
        feed=SyntheticFeed(events),
        venue_steps=(
            ScriptedStep(VenueCall(VenueOperation.SUBMIT, order_id="toy-order")),
        ),
        strategy_parameters={
            "order_id": "toy-order",
            "symbol": "SPY",
            "quantity": Decimal("20"),
        },
        configuration={"symbols": ["SPY"], "mode": "parity"},
        start=configuration.start,
    )

    first, second = assert_reproducible(scenario)

    assert first.manifest_hash == second.manifest_hash
    assert first.dispatch_hash == second.dispatch_hash
    assert first.final_clock == second.final_clock


# --------------------------------------------------------------------------
# Skip and absence behavior
# --------------------------------------------------------------------------


def test_zig_runner_requires_the_built_native_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "HARNESS_NATIVE_LIBRARY", str(tmp_path / "missing" / "harness_native.dll")
    )
    with pytest.raises(NativeCoreUnavailable, match="cargo build"):
        run_zig_backtest(_configuration())
