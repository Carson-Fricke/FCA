"""StateStore snapshot and restore behavior tests.

The tests prove the acceptance behaviors of the runtime state store: periodic,
session-end, and stop snapshots round-trip to deep-equal runtime state;
interrupted writes cannot replace the last valid snapshot; and a snapshot
continuation reaches exactly the state of an uninterrupted run, verified
through the repository determinism manifest conventions.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Any, cast

import pytest

from harness.configuration import (
    StrategyInstanceConfig,
    TraderConfig,
    load_example,
)
from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.symbols import Symbol
from harness.core.types import (
    Fill,
    OrderType,
    QuoteEvent,
    Side,
    SingleLegOrder,
    TimeInForce,
    TradeEvent,
)
from harness.data import LakeReplayFeed
from harness.fills import AssetClass, RemainderPolicy
from harness.portfolio import PortfolioSnapshot
from harness.runtime.backtest.errors import (
    AbortPolicy,
    FillModel,
    StrategyHaltPolicy,
    TeardownPolicy,
)
from harness.runtime.backtest.fills import FillModelOptions
from harness.runtime.paper import (
    PaperOptions,
    PaperSessionPolicy,
    PaperShell,
    ReplayPaperFeed,
)
from harness.runtime.state_store import (
    FORMAT_VERSION,
    RandomState,
    RuntimeSnapshot,
    RuntimeStateStore,
    SnapshotSchedule,
    SnapshotTrigger,
    StateSnapshotIntegrityError,
    StateSnapshotMissingError,
    StateSnapshotRestoreError,
    StateSnapshotVersionError,
    StateSnapshotWriteError,
    StateStoreError,
    StoredSnapshot,
    StrategyState,
)
from harness.strategy import Strategy, StrategyContext
from harness.testing.determinism import (
    ArtifactCanonicalizer,
    DeterminismManifest,
    assert_manifest_equal,
)
from lake.contracts import ExchangeSession
from lake.storage.parquet import LocalFilesystemStore

RUN_ID = "paper-state-store-1"
CASH = Decimal("100000")
INSTANCE_ID = "solo"
SESSION_DATE = date(2024, 1, 2)
OPEN = datetime(2024, 1, 2, 13, 30, tzinfo=UTC)
CLOSE = datetime(2024, 1, 2, 16, 30, tzinfo=UTC)
BACKTEST_END = datetime(2024, 1, 2, 20, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Test strategy, loader, storage adapters, and builders
# --------------------------------------------------------------------------


class StatefulMarketStrategy(Strategy):
    """Submit one deterministic order every ``every`` quotes while writing
    strategy state and advancing the seeded random source."""

    def __init__(self, **parameters: object) -> None:
        self.quantity = Decimal(str(parameters.get("quantity", "10")))
        self.every = int(str(parameters.get("every", "2")))
        self.quotes = 0
        self.context: StrategyContext | None = None

    def on_start(self, context: StrategyContext) -> None:
        self.context = context
        context.state["started"] = True
        context.state["window"] = {"lookback": Decimal("1.5")}

    def on_quote(self, event: QuoteEvent) -> None:
        assert self.context is not None
        self.quotes += 1
        mid = (event.bid_price + event.ask_price) / 2
        self.context.state["quotes"] = self.quotes
        self.context.state["last_mid"] = mid
        self.context.random.random()
        if self.quotes % self.every:
            return
        side = Side.BUY if (self.quotes // self.every) % 2 else Side.SELL
        order_id = f"{self.context.book_id}-state-order-{self.quotes}"
        self.context.orders.submit(
            SingleLegOrder(
                event_id=order_id,
                timestamp=event.timestamp,
                sequence=event.sequence,
                order_id=order_id,
                symbol=event.symbol,
                side=side,
                quantity=self.quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
        )

    def on_fill(self, event: Fill) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        return None


STRATEGY_MODULE = "state_store_test"


def _register_strategy_module() -> None:
    """Expose the test strategy under the configured implementation module."""
    module = types.ModuleType(STRATEGY_MODULE)
    for name, value in (("StatefulMarketStrategy", StatefulMarketStrategy),):
        setattr(module, name, value)
    sys.modules[STRATEGY_MODULE] = module


_register_strategy_module()


def _loader(registry: dict[str, StatefulMarketStrategy]) -> Any:
    """Build a loader that registers every strategy instance it binds."""

    def load(instance: Any) -> type[Strategy]:
        class Registered(StatefulMarketStrategy):
            def __init__(self, **parameters: object) -> None:
                super().__init__(**parameters)
                registry[instance.instance_id] = self

        return Registered

    return load


class MemorySnapshotStore:
    """In-memory adapter for the snapshot object-store port."""

    __slots__ = ("_objects",)

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._objects[relative_path] = bytes(payload)

    def read(self, relative_path: str) -> bytes:
        try:
            return self._objects[relative_path]
        except KeyError:
            raise KeyError(relative_path) from None


class TornWriteStore:
    """Adapter simulating an interrupted write by truncating object writes."""

    __slots__ = ("_inner", "target")

    def __init__(self, inner: MemorySnapshotStore) -> None:
        self._inner = inner
        self.target: str | None = None

    def replace(self, relative_path: str, payload: bytes) -> None:
        torn = self.target is not None and (
            (self.target == "payload" and "/snapshots/" in relative_path)
            or (self.target == "pointer" and relative_path.endswith("/latest.json"))
        )
        if torn:
            self._inner.replace(relative_path, payload[: max(len(payload) // 2, 1)])
            return
        self._inner.replace(relative_path, payload)

    def read(self, relative_path: str) -> bytes:
        return self._inner.read(relative_path)


class _FrozenClock:
    """Deterministic wall-clock adapter returning one fixed instant."""

    __slots__ = ("_moment",)

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def _events() -> list[QuoteEvent | TradeEvent]:
    """Build one deterministic three-hour quote and trade stream."""
    events: list[QuoteEvent | TradeEvent] = []
    sequence = 0
    for index in range(61):
        moment = OPEN + timedelta(minutes=3 * index)
        mid = Decimal("450.00") + Decimal("0.01") * (index % 40)
        sequence += 1
        events.append(
            QuoteEvent(
                event_id=f"quote-{index}",
                timestamp=moment,
                sequence=sequence,
                symbol=Symbol("SPY"),
                bid_price=mid - Decimal("0.01"),
                bid_quantity=Decimal("100"),
                ask_price=mid + Decimal("0.01"),
                ask_quantity=Decimal("100"),
                venue="XNYS",
            )
        )
        if index % 3 == 0:
            sequence += 1
            events.append(
                TradeEvent(
                    event_id=f"trade-{index}",
                    timestamp=moment + timedelta(seconds=90),
                    sequence=sequence,
                    symbol=Symbol("SPY"),
                    price=mid,
                    quantity=Decimal("50"),
                    venue="XNYS",
                    trade_id=str(1_000_000 + index),
                )
            )
    return events


def _replay_feed(events: Iterable[QuoteEvent | TradeEvent]) -> ReplayPaperFeed:
    return ReplayPaperFeed(
        LakeReplayFeed(
            sessions=(ExchangeSession(SESSION_DATE, OPEN, BACKTEST_END),),
            streams={"SPY": list(events)},
        )
    )


def _instance(
    instance_id: str = INSTANCE_ID,
    *,
    capital_fraction: Decimal = Decimal("1.0"),
) -> StrategyInstanceConfig:
    return StrategyInstanceConfig(
        instance_id=instance_id,
        implementation="state_store_test:StatefulMarketStrategy",
        bundle_hash="c" * 64,
        capital_fraction=capital_fraction,
        symbols=("SPY",),
        parameters={"every": "2"},
    )


def _trader_config(**overrides: object) -> TraderConfig:
    example = load_example("trader")
    assert isinstance(example, TraderConfig)
    updates: dict[str, object] = {
        "mode": "paper",
        "seed": 7,
        "strategy_instances": (_instance(),),
    }
    updates.update(overrides)
    return example.model_copy(update=updates)


def _options(**overrides: object) -> PaperOptions:
    values: dict[str, object] = {"run_id": RUN_ID, "initial_cash": CASH}
    values.update(overrides)
    return PaperOptions(**cast(dict[str, Any], values))


def _state_store(
    store: MemorySnapshotStore | None = None,
    *,
    mirror: LocalFilesystemStore | None = None,
) -> RuntimeStateStore:
    return RuntimeStateStore(
        store if store is not None else MemorySnapshotStore(),
        run_id=RUN_ID,
        mirror=mirror,
    )


def _advance(shell: PaperShell, events: Iterable[QuoteEvent | TradeEvent]) -> None:
    for event in events:
        shell.step(event)


def _capture(
    shell: PaperShell,
    store: RuntimeStateStore,
    options: PaperOptions,
    registry: dict[str, StatefulMarketStrategy],
    trigger: SnapshotTrigger,
    sequence: int = 0,
    order_owners: dict[str, str] | None = None,
) -> RuntimeSnapshot:
    strategy_state: dict[str, dict[str, object]] = {}
    random_sources: dict[str, Random] = {}
    for instance_id, strategy in registry.items():
        context = strategy.context
        if context is not None:
            strategy_state[instance_id] = dict(context.state.snapshot)
            random_sources[instance_id] = context.random
    return store.capture(
        shell,
        options=options,
        strategy_ids=tuple(registry),
        trigger=trigger,
        sequence=sequence,
        order_owners=order_owners,
        strategy_state=strategy_state,
        random_sources=random_sources,
    )


def _payload_key(snapshot_id: str) -> str:
    return f"runs/{RUN_ID}/snapshots/{snapshot_id}.json"


def _pointer_key() -> str:
    return f"runs/{RUN_ID}/latest.json"


def _final_state_manifest(
    portfolio: PortfolioSnapshot, fills: tuple[Fill, ...]
) -> DeterminismManifest:
    artifacts = ArtifactCanonicalizer.artifacts(
        {
            "final/fills": list(fills),
            "final/portfolio": portfolio,
        }
    )
    return ArtifactCanonicalizer.manifest(
        {"comparison": "restore-equivalence", "run_id": RUN_ID}, artifacts
    )


# --------------------------------------------------------------------------
# Round-trip: periodic, session-end, and stop snapshots
# --------------------------------------------------------------------------


def test_periodic_snapshot_round_trips_to_deep_equal_state() -> None:
    events = _events()[:24]
    options = _options(start=OPEN)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    _advance(shell, events)
    store = _state_store()
    snapshot = _capture(
        shell, store, options, registry, SnapshotTrigger.PERIODIC, sequence=24
    )

    stored = store.save(snapshot)
    assert stored.snapshot_id == snapshot.snapshot_id
    assert store.latest().snapshot_id == snapshot.snapshot_id

    loaded = store.load()
    assert loaded == snapshot
    assert loaded.snapshot_id == snapshot.snapshot_id

    hub = store.restore_hub(loaded)
    assert hub.snapshot == shell.hub.snapshot
    for book_state in loaded.books:
        book = hub.book(book_state.book_id)
        assert book.cash == book_state.cash
        assert book.fill_ids == book_state.fill_ids
        assert book.fill_count == len(book_state.fills)
    assert loaded.order_counts == {INSTANCE_ID: shell.order_count(INSTANCE_ID)}
    assert loaded.prices == dict(shell.prices)
    assert loaded.risk.daily_losses == {INSTANCE_ID: shell.daily_loss(INSTANCE_ID)}
    context = registry[INSTANCE_ID].context
    assert context is not None
    assert loaded.strategy_state[0].values == dict(context.state.snapshot)
    restored_random = store.restore_randoms(loaded)[INSTANCE_ID]
    assert restored_random.random() == context.random.random()


def test_session_end_snapshot_round_trips_after_a_completed_session() -> None:
    events = _events()
    options = _options(start=OPEN)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    result = shell.run(_replay_feed(events))
    assert result.completed

    store = _state_store()
    snapshot = _capture(shell, store, options, registry, SnapshotTrigger.SESSION_END)
    store.save(snapshot)

    loaded = store.load()
    assert loaded == snapshot
    assert loaded.trigger is SnapshotTrigger.SESSION_END
    assert loaded.market_event_count == len(events)
    hub = store.restore_hub(loaded)
    assert hub.snapshot == shell.hub.snapshot


def test_stop_snapshot_round_trips_after_stop_is_requested() -> None:
    events = _events()[:10]
    options = _options(start=OPEN)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    _advance(shell, events)
    shell.stop("operator stop")

    store = _state_store()
    snapshot = _capture(shell, store, options, registry, SnapshotTrigger.STOP)
    store.save(snapshot)

    loaded = store.load()
    assert loaded == snapshot
    assert loaded.trigger is SnapshotTrigger.STOP
    hub = store.restore_hub(loaded)
    assert hub.snapshot == shell.hub.snapshot


# --------------------------------------------------------------------------
# Determinism of snapshot identity
# --------------------------------------------------------------------------


def test_snapshot_identity_is_deterministic_and_wall_clock_free() -> None:
    events = _events()[:12]
    options = _options(start=OPEN)
    identities: list[tuple[str, bytes]] = []
    for moment in (OPEN, CLOSE):
        registry: dict[str, StatefulMarketStrategy] = {}
        shell = PaperShell(
            _trader_config(),
            options=options,
            strategy_loader=_loader(registry),
            wall_clock=_FrozenClock(moment),
        )
        _advance(shell, events)
        store = _state_store()
        snapshot = _capture(
            shell, store, options, registry, SnapshotTrigger.SESSION_END
        )
        identities.append((snapshot.snapshot_id, snapshot.payload_bytes()))
    assert identities[0][0] == identities[1][0]
    assert identities[0][1] == identities[1][1]


# --------------------------------------------------------------------------
# Atomic save: interrupted writes cannot replace the last valid snapshot
# --------------------------------------------------------------------------


def test_interrupted_payload_write_cannot_replace_last_valid_snapshot() -> None:
    torn = TornWriteStore(MemorySnapshotStore())
    store = RuntimeStateStore(torn, run_id=RUN_ID)
    options = _options(start=OPEN)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    _advance(shell, _events()[:12])
    snapshot = _capture(shell, store, options, registry, SnapshotTrigger.PERIODIC)
    store.save(snapshot)

    _advance(shell, _events()[12:24])
    advanced = _capture(shell, store, options, registry, SnapshotTrigger.PERIODIC)
    assert advanced.snapshot_id != snapshot.snapshot_id

    torn.target = "payload"
    pointer_bytes = torn.read(_pointer_key())
    with pytest.raises(StateSnapshotWriteError):
        store.save(advanced)

    assert torn.read(_pointer_key()) == pointer_bytes
    assert store.latest().snapshot_id == snapshot.snapshot_id
    assert store.load() == snapshot

    torn.target = None
    store.save(advanced)
    assert store.load() == advanced


def test_interrupted_first_write_leaves_no_snapshot() -> None:
    torn = TornWriteStore(MemorySnapshotStore())
    torn.target = "payload"
    store = RuntimeStateStore(torn, run_id=RUN_ID)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(),
        options=_options(start=OPEN),
        strategy_loader=_loader(registry),
    )
    _advance(shell, _events()[:6])
    snapshot = _capture(
        shell, store, _options(start=OPEN), registry, SnapshotTrigger.PERIODIC
    )

    with pytest.raises(StateSnapshotWriteError):
        store.save(snapshot)
    with pytest.raises(StateSnapshotMissingError):
        store.load()


def test_torn_pointer_write_falls_back_to_the_mirror(tmp_path) -> None:
    torn = TornWriteStore(MemorySnapshotStore())
    mirror = LocalFilesystemStore(tmp_path)
    store = RuntimeStateStore(torn, run_id=RUN_ID, mirror=mirror)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(),
        options=_options(start=OPEN),
        strategy_loader=_loader(registry),
    )
    _advance(shell, _events()[:12])
    snapshot = _capture(
        shell, store, _options(start=OPEN), registry, SnapshotTrigger.PERIODIC
    )
    store.save(snapshot)

    torn.target = "pointer"
    with pytest.raises(StateSnapshotWriteError):
        store.save(snapshot)
    torn.target = None
    assert store.load() == snapshot


def test_tampered_payload_fails_the_integrity_check() -> None:
    primary = MemorySnapshotStore()
    store = _state_store(primary)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(),
        options=_options(start=OPEN),
        strategy_loader=_loader(registry),
    )
    _advance(shell, _events()[:12])
    snapshot = _capture(
        shell, store, _options(start=OPEN), registry, SnapshotTrigger.PERIODIC
    )
    store.save(snapshot)

    payload_key = _payload_key(snapshot.snapshot_id)
    payload = bytearray(primary.read(payload_key))
    payload[0] ^= 0xFF
    primary.replace(payload_key, bytes(payload))
    with pytest.raises(StateSnapshotIntegrityError):
        store.load()


def test_unknown_snapshot_format_version_is_rejected() -> None:
    primary = MemorySnapshotStore()
    store = _state_store(primary)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(),
        options=_options(start=OPEN),
        strategy_loader=_loader(registry),
    )
    _advance(shell, _events()[:12])
    snapshot = _capture(
        shell, store, _options(start=OPEN), registry, SnapshotTrigger.PERIODIC
    )
    store.save(snapshot)

    data = dict(snapshot.to_json())
    data["format_version"] = 99
    payload = canonical_configuration_bytes(
        {"snapshot_id": snapshot.snapshot_id, "snapshot": data}
    )
    primary.replace(_payload_key(snapshot.snapshot_id), payload)
    pointer = StoredSnapshot(
        format_version=FORMAT_VERSION,
        run_id=RUN_ID,
        snapshot_id=snapshot.snapshot_id,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        byte_length=len(payload),
    )
    primary.replace(_pointer_key(), canonical_configuration_bytes(pointer.to_json()))
    with pytest.raises(StateSnapshotVersionError):
        store.load()
    with pytest.raises(StateSnapshotVersionError):
        StoredSnapshot(
            format_version=99,
            run_id=RUN_ID,
            snapshot_id=snapshot.snapshot_id,
            payload_sha256="0" * 64,
            byte_length=8,
        )


def test_load_falls_back_to_the_local_mirror(tmp_path) -> None:
    mirror = LocalFilesystemStore(tmp_path)
    registry: dict[str, StatefulMarketStrategy] = {}
    options = _options(start=OPEN)
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    _advance(shell, _events()[:12])
    store = _state_store(mirror=mirror)
    snapshot = _capture(shell, store, options, registry, SnapshotTrigger.PERIODIC)
    store.save(snapshot)

    recovered = RuntimeStateStore(MemorySnapshotStore(), run_id=RUN_ID, mirror=mirror)
    assert recovered.load() == snapshot


def test_missing_snapshot_is_reported() -> None:
    store = _state_store()
    with pytest.raises(StateSnapshotMissingError):
        store.load()


# --------------------------------------------------------------------------
# Restore equivalence: snapshot, continue, compare with the uninterrupted run
# --------------------------------------------------------------------------


def test_restore_continuation_matches_the_uninterrupted_run() -> None:
    events = _events()
    split = 40
    options = _options(start=OPEN)
    config = _trader_config()

    registry_a: dict[str, StatefulMarketStrategy] = {}
    shell_a = PaperShell(config, options=options, strategy_loader=_loader(registry_a))
    _advance(shell_a, events)
    final_a = shell_a.result()

    store = _state_store()
    registry_b: dict[str, StatefulMarketStrategy] = {}
    shell_b = PaperShell(config, options=options, strategy_loader=_loader(registry_b))
    _advance(shell_b, events[:split])
    context_b = registry_b[INSTANCE_ID].context
    assert context_b is not None
    snapshot = _capture(
        shell_b,
        store,
        options,
        registry_b,
        SnapshotTrigger.PERIODIC,
        sequence=events[split - 1].sequence,
    )
    assert store.save(snapshot).snapshot_id == snapshot.snapshot_id
    reference_draws = [context_b.random.random() for _ in range(4)]
    _advance(shell_b, events[split:])
    final_b = shell_b.result()

    loaded = store.load()
    assert loaded == snapshot
    assert loaded.snapshot_id == snapshot.snapshot_id

    restored_random = store.restore_randoms(loaded)[INSTANCE_ID]
    for expected in reference_draws:
        assert restored_random.random() == expected

    hub = store.restore_hub(loaded)
    captured = {fill_id for book in loaded.books for fill_id in book.fill_ids}
    for fill in final_b.fills:
        if fill.fill_id not in captured:
            hub.apply_fill(INSTANCE_ID, fill)
    for symbol in sorted(shell_b.prices):
        hub.mark(symbol, shell_b.prices[symbol])

    assert hub.snapshot == final_a.portfolio
    restored_fills = tuple(
        [fill for book in loaded.books for fill in book.fills]
        + [fill for fill in final_b.fills if fill.fill_id not in captured]
    )
    assert_manifest_equal(
        _final_state_manifest(final_a.portfolio, final_a.fills),
        _final_state_manifest(hub.snapshot, restored_fills),
    )


# --------------------------------------------------------------------------
# Shell and multi-book restoration
# --------------------------------------------------------------------------


def test_restore_shell_reproduces_supervision_state() -> None:
    events = _events()[:30]
    options = _options(start=OPEN)
    config = _trader_config()
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(config, options=options, strategy_loader=_loader(registry))
    _advance(shell, events)
    store = _state_store()
    snapshot = _capture(
        shell,
        store,
        options,
        registry,
        SnapshotTrigger.PERIODIC,
        order_owners={"state-order-2": INSTANCE_ID, "state-order-4": INSTANCE_ID},
    )

    restored = store.restore_shell(snapshot, config)

    assert restored.hub.snapshot == shell.hub.snapshot
    assert restored.order_count(INSTANCE_ID) == shell.order_count(INSTANCE_ID)
    assert restored.daily_loss(INSTANCE_ID) == shell.daily_loss(INSTANCE_ID)
    assert restored.run_id == snapshot.run_id


def test_multi_book_capture_restores_exact_fill_routing() -> None:
    events = _events()[:30]
    options = _options(start=OPEN)
    config = _trader_config(
        strategy_instances=(
            _instance("alpha", capital_fraction=Decimal("0.6")),
            _instance("beta", capital_fraction=Decimal("0.4")),
        )
    )
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(config, options=options, strategy_loader=_loader(registry))
    _advance(shell, events)
    store = _state_store()
    snapshot = store.capture(
        shell,
        options=options,
        strategy_ids=("alpha", "beta"),
        trigger=SnapshotTrigger.SESSION_END,
    )
    store.save(snapshot)

    loaded = store.load()
    assert loaded == snapshot
    hub = store.restore_hub(loaded)
    assert hub.snapshot == shell.hub.snapshot
    assert {book.book_id for book in loaded.books} == {"alpha", "beta"}
    for book_state in loaded.books:
        book = hub.book(book_state.book_id)
        assert book.fill_ids == book_state.fill_ids
        assert book.cash == book_state.cash
        assert len(book_state.fills) > 0


def test_restore_shell_rejects_mismatched_configuration() -> None:
    events = _events()[:12]
    options = _options(start=OPEN)
    config = _trader_config(
        strategy_instances=(
            _instance("alpha", capital_fraction=Decimal("0.6")),
            _instance("beta", capital_fraction=Decimal("0.4")),
        )
    )
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(config, options=options, strategy_loader=_loader(registry))
    _advance(shell, events)
    store = _state_store()
    snapshot = store.capture(
        shell,
        options=options,
        strategy_ids=("alpha", "beta"),
        trigger=SnapshotTrigger.PERIODIC,
    )
    store.save(snapshot)

    with pytest.raises(StateSnapshotRestoreError):
        store.restore_shell(snapshot, _trader_config())


def test_capture_rejects_run_mismatch() -> None:
    options = _options(start=OPEN)
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(), options=options, strategy_loader=_loader(registry)
    )
    store = RuntimeStateStore(MemorySnapshotStore(), run_id="other-run")
    with pytest.raises(StateStoreError, match="does not match store run"):
        store.capture(
            shell,
            options=options,
            strategy_ids=(INSTANCE_ID,),
            trigger=SnapshotTrigger.PERIODIC,
        )


# --------------------------------------------------------------------------
# Random state, strategy state, and runtime options round-trips
# --------------------------------------------------------------------------


def test_random_state_round_trips_exactly_through_canonical_json() -> None:
    original = Random(11)
    for _ in range(9):
        original.random()
    original.gauss(0.0, 1.0)
    record = RandomState.from_random(INSTANCE_ID, original)

    restored = RandomState.from_json(json.loads(json.dumps(record.to_json())))
    assert restored == record

    fresh = Random(0)
    record.apply(fresh)
    for _ in range(6):
        assert fresh.random() == original.random()
        assert fresh.gauss(0.0, 1.0) == original.gauss(0.0, 1.0)


def test_strategy_state_round_trips_through_canonical_json() -> None:
    context_state: dict[str, object] = {
        "started": True,
        "quotes": 12,
        "last_mid": Decimal("450.015"),
        "window": {"lookback": Decimal("1.5"), "tags": ("a", "b")},
        "nested": {"enabled": False, "level": 3},
    }
    record = StrategyState(instance_id=INSTANCE_ID, values=dict(context_state))

    parsed = json.loads(canonical_configuration_bytes(record.to_json()).decode("utf-8"))
    restored = StrategyState.from_json(parsed)
    assert restored == record


def test_paper_options_round_trip_and_rebuild() -> None:
    session_options = PaperOptions(
        run_id=RUN_ID,
        initial_cash=CASH,
        fill_model=FillModel.LATENCY_SIZE,
        fill_options=FillModelOptions(
            response_latency=timedelta(milliseconds=25),
            remainder_policy=RemainderPolicy.CANCEL,
            require_trade_through=True,
            minimum_trade_through=Decimal("0.01"),
            strict=True,
        ),
        fee_precision=Decimal("0.0001"),
        asset_class=AssetClass.EQUITY,
        history_capacity=512,
        abort_policy=AbortPolicy.STOP,
        strategy_halt_policy=StrategyHaltPolicy.HALT_STRATEGY,
        teardown_policy=TeardownPolicy.ALWAYS,
        extra_latency=timedelta(milliseconds=5),
        session_policy=PaperSessionPolicy.STOP_AT_CLOSE,
        session=ExchangeSession(SESSION_DATE, OPEN, CLOSE),
    )
    registry: dict[str, StatefulMarketStrategy] = {}
    shell = PaperShell(
        _trader_config(),
        options=session_options,
        strategy_loader=_loader(registry),
    )
    _advance(shell, _events()[:6])
    store = _state_store()
    snapshot = _capture(
        shell, store, session_options, registry, SnapshotTrigger.PERIODIC
    )

    rebuilt = snapshot.paper_options()
    assert rebuilt == session_options
    rebuilt_session = rebuilt.session
    assert rebuilt_session is not None
    assert rebuilt_session.open_time == OPEN
    assert rebuilt_session.close_time == CLOSE


def test_snapshot_schedule_periodic_cadence() -> None:
    schedule = SnapshotSchedule(every_events=5)
    assert schedule.due(0) is False
    assert schedule.due(4) is False
    assert schedule.due(5) is True
    assert schedule.due(7) is False
    assert schedule.due(10) is True
    with pytest.raises(ValueError):
        SnapshotSchedule(every_events=0)
