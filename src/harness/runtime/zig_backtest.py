"""Zig-core-driven backtest runner with native kernel parity arbitration.

This module wires the local backtest runner to the native core
(``src/harness/native``, consumed exclusively through :mod:`harness._native`)
so one local run executes the Zig core end to end. The composition mirrors
:func:`harness.runtime.backtest.engine.run_backtest` for the equities
single-leg path and reuses its seams: the fixture, preflight, fill plugin,
fee schedule, risk pipeline, strategy loading, and manifest planning are the
engine's own, and the engine loop is the engine's loop with one additional
dispatch recorder. The runner is local mode only: no Azure, no network, no
wall-clock reads.

Kernel routing for one run:

- Native through the stable C ABI v1: symbol canonicalization, deterministic
  identifier derivation, canonical fixed-point decimal text, canonical
  trade-row serialization, and the pinned decimal128(38, 18) batch through
  the Arrow C Data Interface bridge. Every routed operation must agree with
  the Python kernel exactly or the run fails with :class:`ZigParityError`.
- Python, natively arbitrated: the ``SimClock`` advance, the queue and bus
  ordering, the execution-report state machine (``harness.oms``, the direct
  twin of ``oms_state.zig``), and ``VirtualBook`` accounting (the twin of
  ``books.zig``). The dispatch digest is re-derived with the native bus
  port's byte-exact identity frame (``bus.zig recordDispatch``) and must
  equal the engine's digest; the parity oracle additionally reproduces the
  native bus port's pinned 10,000-event golden vector through the Python
  bus. The Zig clock, bus, OMS, and book kernels are compiled into the
  native library but are not exported by ``ffi.zig`` ABI v1, so driving
  them through the C boundary requires an ABI v2 addition (tracked as an
  integration finding, not implemented here). The venue, fills, fees, risk,
  and strategy wiring stays Python, as in the engine.

Manifest policy: the Zig-driven path produces the same manifest shape and
version. Every field is pinned byte-identical to :func:`run_backtest`'s
manifest except ``engine_id`` — the only legitimate difference, because it
identifies the driving kernel composition (``harness.backtest.zig`` versus
``harness.backtest``). ``engine_version``, ``code_fingerprint``, and all
configuration, data, fill, fee, risk, and strategy fingerprints are
byte-identical because both paths execute the same deterministic Python
engine code with the same inputs; the native core arbitrates them rather
than substituting them (its identity is pinned by the ABI version validated
at load time). Consequently :meth:`BacktestResult.outcome_digest` values —
fills, fees, portfolio ledgers, dispatch hash, and counts — are byte-ident
across engines, and the only byte difference in ``artifact_json()`` is the
manifest ``engine_id`` field.

The runner never reads wall time, consumes unseeded randomness, or opens a
network connection, and every money or quantity value remains an exact
:class:`~decimal.Decimal`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from harness._native import (
    DECIMAL_SCALE,
    NativeCore,
    load_native_core,
)
from harness.configuration import BacktestConfig
from harness.core.events import Event
from harness.core.ids import deterministic_id
from harness.core.symbols import canonical_symbol
from harness.core.types import (
    ExecutionReport,
    Fill,
    QuoteEvent,
    SingleLegOrder,
    TradeEvent,
)
from harness.fills import FeeSchedule
from harness.oms import ReplaceError, SingleLegOMS
from harness.portfolio import VirtualBook
from harness.portfolio.book import BookSnapshot, PositionSnapshot
from harness.portfolio.hub import AggregatePosition, PortfolioSnapshot
from harness.risk import RiskPipeline
from harness.runtime.backtest.engine import (
    BacktestOptions,
    _BacktestEngine,
    build_manifest,
)
from harness.runtime.backtest.errors import (
    BacktestConfigurationError,
    BacktestError,
)
from harness.runtime.backtest.fees import fee_schedule_from_config
from harness.runtime.backtest.fills import (
    FillPlugin,
    build_fill_model,
)
from harness.runtime.backtest.fixture import DataPreflight, ReplayFixture
from harness.runtime.backtest.manifest import BacktestManifest, BacktestResult
from harness.runtime.backtest.preflight import (
    MarketDataSource,
    preflight_data,
    resolve_data_source,
)
from harness.runtime.backtest.strategies import (
    StrategyLoader,
    default_strategy_loader,
)

ZIG_ENGINE_ID: Final = "harness.backtest.zig"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO: Final = Decimal("0")

_IDENTIFIER_KINDS: Final = ("event", "order", "fill", "report", "engine")
_IDENTIFIER_ORDINALS: Final = 8

ENGINE_ID_FIELD: Final = "engine_id"


class ZigParityError(BacktestError):
    """The native core diverged from the Python kernel during a driven run."""


@dataclass(frozen=True, slots=True)
class ZigKernelEvidence:
    """Canonical record of one Zig-driven run's native kernel arbitration."""

    abi_version: int
    identity_checks: int
    symbol_checks: int
    decimal_checks: int
    decimal_skips: int
    trade_row_checks: int
    dispatch_digest: str
    dispatched_count: int
    oms_orders: int
    oms_skips: int
    ledger_books: int
    arrow_batch_length: int


# --------------------------------------------------------------------------
# Native bus dispatch digest (bus.zig recordDispatch, byte-exact mirror)
# --------------------------------------------------------------------------


def _native_dispatch_digest(events: Sequence[Event]) -> str:
    """Re-derive the native bus port's dispatch digest over one stream.

    The identity frame follows ``src/harness/native/bus.zig recordDispatch``
    exactly: per dispatched event, the big-endian u64 identity length followed
    by ``"<python type name>\0<event_type value>\0<isoformat microseconds>\0
    <sequence>\0<event_id>"``. The Zig port pins byte-identity with the Python
    bus through its own unit tests; this mirror lets a driven run re-derive
    the same digest from its recorded dispatch order.
    """
    digest = hashlib.sha256()
    for event in events:
        identity = "\0".join(
            (
                f"{type(event).__module__}.{type(event).__qualname__}",
                event.event_type.value,
                event.timestamp.isoformat(timespec="microseconds"),
                str(event.sequence),
                event.event_id,
            )
        ).encode("utf-8")
        digest.update(len(identity).to_bytes(8, "big"))
        digest.update(identity)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Zig-driven engine: the engine loop with dispatch capture
# --------------------------------------------------------------------------


class _ZigDrivenEngine(_BacktestEngine):
    """The engine composition with native-kernel arbitration inputs captured.

    The engine loop, venue, fills, fees, risk pipeline, strategies, clock, bus,
    and portfolio are exactly :class:`harness.runtime.backtest.engine.
    _BacktestEngine`. The subclass only records the bus-ordered dispatched
    stream and the order-ownership map so the native kernel arbiter can
    re-derive and arbitrate the run's kernel outputs after the run completes.
    """

    __slots__ = ("_core", "_dispatched", "_owners")

    def __init__(
        self,
        configuration: BacktestConfig,
        fixture: ReplayFixture,
        preflight: DataPreflight,
        manifest: BacktestManifest,
        *,
        options: BacktestOptions,
        fee_schedule: FeeSchedule,
        plugin: FillPlugin,
        pipeline: RiskPipeline,
        strategy_loader: StrategyLoader,
        core: NativeCore,
    ) -> None:
        super().__init__(
            configuration,
            fixture,
            preflight,
            manifest,
            options=options,
            fee_schedule=fee_schedule,
            plugin=plugin,
            pipeline=pipeline,
            strategy_loader=strategy_loader,
        )
        self._core = core
        self._dispatched: list[Event] = []
        self._owners: dict[str, str] = {}

    @property
    def dispatched(self) -> tuple[Event, ...]:
        """Return every dispatched event in native bus order."""
        return tuple(self._dispatched)

    @property
    def order_owners(self) -> Mapping[str, str]:
        """Return the accepted order-to-strategy attribution of the run."""
        return dict(self._owners)

    def _drain(self) -> None:
        while True:
            dispatched = self._bus.dispatch_next()
            if dispatched is None:
                return
            self._dispatched.append(dispatched)
            self._settle()

    def own_order(self, order_id: str, instance_id: str) -> None:
        self._owners[order_id] = instance_id
        super().own_order(order_id, instance_id)


# --------------------------------------------------------------------------
# Native kernel arbiter
# --------------------------------------------------------------------------


def _timestamp_ns(stamp: datetime) -> int:
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ZigParityError("event timestamps must be timezone-aware")
    return int((stamp - _EPOCH) // timedelta(microseconds=1)) * 1_000


def _unscaled(value: Decimal) -> int | None:
    """Return the scale-18 unscaled integer, or ``None`` when out of domain."""
    scaled = value.scaleb(DECIMAL_SCALE)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _arbitrate_identifiers(core: NativeCore, configuration: BacktestConfig) -> int:
    """Arbitrate the native deterministic identifier derivation."""
    seed = f"int:{configuration.seed}".encode("ascii")
    checked = 0
    for kind in _IDENTIFIER_KINDS:
        for ordinal in range(8):
            native = core.derive_id(seed, kind, ordinal)
            expected = UUID(deterministic_id(configuration.seed, kind, ordinal)).bytes
            if native != expected:
                raise ZigParityError(
                    f"native id derivation diverged for kind {kind!r} ordinal {ordinal}"
                )
            checked += 1
    return checked


def _arbitrate_symbols(
    core: NativeCore,
    configuration: BacktestConfig,
    dispatched: Sequence[Event],
) -> int:
    """Arbitrate native symbol canonicalization over the run's symbols."""
    texts = {str(symbol) for symbol in configuration.data.symbols}
    for instance in configuration.strategy_instances:
        texts.update(str(symbol) for symbol in instance.symbols)
    for event in dispatched:
        if isinstance(event, QuoteEvent | TradeEvent | Fill):
            texts.add(str(event.symbol))
    checked = 0
    for text in sorted(texts):
        canonical = str(canonical_symbol(text))
        native = core.symbol_canonicalize(text)
        if native != canonical:
            raise ZigParityError(
                f"native symbol canonicalization diverged for {text!r}: "
                f"{native!r} != {canonical!r}"
            )
        checked += 1
    return checked


def _arbitrate_decimals(core: NativeCore, result: BacktestResult) -> tuple[int, int]:
    """Arbitrate native decimal rendering over the run's exact money domain."""
    values: list[Decimal] = []
    for fill in result.fills:
        values.extend((fill.quantity, fill.price, fill.fee))
    for assessment in result.fee_assessments:
        values.append(assessment.total)
        values.extend(component.amount for component in assessment.components)
    for aggregate in (
        *result.fee_totals.by_run,
        *result.fee_totals.by_strategy,
        *result.fee_totals.by_asset_class,
    ):
        values.append(aggregate.amount)
    values.extend(_portfolio_decimals(result.portfolio))
    checked = 0
    skipped = 0
    for value in values:
        unscaled = _unscaled(value)
        if unscaled is None:
            skipped += 1
            continue
        rendered = Decimal(core.decimal_text(unscaled))
        if rendered != value:
            raise ZigParityError(
                f"native decimal rendering diverged: {core.decimal_text(unscaled)!r} "
                f"does not equal {str(value)!r}"
            )
        checked += 1
    return checked, skipped


def _snapshot_decimals(
    position: PositionSnapshot | AggregatePosition,
) -> list[Decimal]:
    values = [
        position.quantity,
        position.cost_basis,
        position.cash_flow,
        position.realized_pnl,
    ]
    for optional in (
        position.average_cost,
        position.mark_price,
        position.market_value,
        position.unrealized_pnl,
    ):
        if optional is not None:
            values.append(optional)
    return values


def _book_decimals(book: BookSnapshot) -> list[Decimal]:
    values = [book.initial_cash, book.cash, book.realized_pnl]
    for optional in (book.unrealized_pnl, book.equity, book.total_pnl):
        if optional is not None:
            values.append(optional)
    for position in book.positions:
        values.extend(_snapshot_decimals(position))
    return values


def _portfolio_decimals(snapshot: PortfolioSnapshot) -> list[Decimal]:
    values = [snapshot.initial_cash, snapshot.cash, snapshot.realized_pnl]
    for optional in (snapshot.unrealized_pnl, snapshot.equity, snapshot.total_pnl):
        if optional is not None:
            values.append(optional)
    for position in snapshot.positions:
        values.extend(_snapshot_decimals(position))
    for book in snapshot.books:
        values.extend(_book_decimals(book))
    return values


def _arbitrate_trade_rows(core: NativeCore, dispatched: Sequence[Event]) -> int:
    """Arbitrate native trade-row serialization against the codecs' row.

    The native serializer renders the pinned decimal128(38, 18) domain, so its
    decimal text is the full pinned-scale form. Replayed lake rows carry
    numerically identical Decimals whose Python ``str`` may be normalized, so
    the arbitration pins the codecs' row layout and every string field
    byte-exactly, and the decimal fields by exact numeric value. Byte-identity
    for scale-18 records is pinned separately by the native core's own golden
    test and ``tests/harness/native/test_canonical_serialization.py``.
    """
    checked = 0
    for event in dispatched:
        if not isinstance(event, TradeEvent):
            continue
        price = _unscaled(event.price)
        quantity = _unscaled(event.quantity)
        if price is None or quantity is None:
            raise ZigParityError(
                f"trade {event.event_id!r} carries a value outside the pinned "
                f"decimal128({38}, {DECIMAL_SCALE}) domain"
            )
        native = core.trade_row_json(
            event_id=event.event_id,
            timestamp_ns=_timestamp_ns(event.timestamp),
            sequence=event.sequence,
            symbol=str(event.symbol),
            price_unscaled=price,
            quantity_unscaled=quantity,
            venue=event.venue,
            trade_id=event.trade_id,
        )
        _assert_trade_row(event, json.loads(native))
        checked += 1
    return checked


_TRADE_ROW_LAYOUT: Final = (
    "event_id",
    "timestamp",
    "sequence",
    "symbol",
    "price",
    "quantity",
    "venue",
    "trade_id",
)


def _assert_trade_row(event: TradeEvent, rendered: dict[str, object]) -> None:
    """Require the codecs' row layout, strings, and exact decimal values."""
    if tuple(rendered) != _TRADE_ROW_LAYOUT:
        raise ZigParityError(f"native trade row layout diverged for {event.event_id!r}")
    if (
        rendered["event_id"] != event.event_id
        or rendered["timestamp"] != event.timestamp.isoformat()
        or rendered["sequence"] != event.sequence
        or rendered["symbol"] != str(event.symbol)
        or rendered["venue"] != event.venue
        or rendered["trade_id"] != event.trade_id
        or Decimal(str(rendered["price"])) != event.price
        or Decimal(str(rendered["quantity"])) != event.quantity
    ):
        raise ZigParityError(
            f"native trade-row serialization diverged for {event.event_id!r}"
        )


def _arbitrate_dispatch(result: BacktestResult, dispatched: Sequence[Event]) -> str:
    """Arbitrate the native bus dispatch digest over the recorded stream."""
    digest = _native_dispatch_digest(dispatched)
    if len(dispatched) != result.dispatched_count:
        raise ZigParityError(
            "dispatched event count diverged from the run result: "
            f"{len(dispatched)} recorded versus {result.dispatched_count} counted"
        )
    if digest != result.dispatch_hash:
        raise ZigParityError("native bus dispatch digest diverged from the run result")
    return digest


def _arbitrate_oms(
    dispatched: Sequence[Event], result: BacktestResult
) -> tuple[int, int]:
    """Arbitrate the execution-report state machine over the run's reports.

    Every accepted order's report chain is replayed through the Python OMS
    state machine (the direct twin of ``oms_state.zig``) and the resulting
    filled quantity must equal the engine's fills for the same order. Report
    chains without fills (expiry, rejection) and venue-side atomic
    replacements, which are venue-owned in the engine, are counted as skips.
    """
    chains: dict[str, list[ExecutionReport]] = {}
    for event in dispatched:
        if isinstance(event, ExecutionReport):
            chains.setdefault(event.order_id, []).append(event)
    fills_by_order: dict[str, list[Fill]] = {}
    for fill in result.fills:
        fills_by_order.setdefault(fill.order_id, []).append(fill)
    checked = 0
    skipped = 0
    for order_id, chain in sorted(chains.items()):
        order_fills = fills_by_order.get(order_id, ())
        if not order_fills:
            skipped += 1
            continue
        first = chain[0]
        order = SingleLegOrder(
            event_id=f"{order_id}-oms-parity",
            timestamp=first.timestamp,
            sequence=first.sequence,
            order_id=order_id,
            symbol=order_fills[0].symbol,
            side=order_fills[0].side,
            quantity=first.order_quantity,
        )
        oms = SingleLegOMS()
        snapshot = oms.submit(order)
        try:
            for report in chain:
                snapshot = oms.apply(report)
        except ReplaceError:
            skipped += 1
            continue
        filled = sum((fill.quantity for fill in order_fills), start=_ZERO)
        if snapshot.filled_quantity != filled:
            raise ZigParityError(
                f"native OMS ledger diverged for order {order_id!r}: "
                f"{snapshot.filled_quantity} versus {filled}"
            )
        checked += 1
    return checked, skipped


def _arbitrate_ledgers(
    configuration: BacktestConfig,
    result: BacktestResult,
    owners: Mapping[str, str],
) -> int:
    """Arbitrate VirtualBook accounting by replaying the run's fills."""
    books = [
        VirtualBook(
            instance.instance_id,
            configuration.initial_cash * instance.capital_fraction,
        )
        for instance in configuration.strategy_instances
    ]
    for fill in result.fills:
        owner = owners.get(fill.order_id)
        if owner is None:
            continue
        book = next((item for item in books if item.book_id == owner), None)
        if book is None:
            raise ZigParityError(
                f"fill {fill.fill_id!r} names unknown strategy {owner!r}"
            )
        book.apply_fill(fill)
    engine_books = {book.book_id: book for book in result.portfolio.books}
    for book in books:
        engine_book = engine_books.get(book.book_id)
        if engine_book is None:
            raise ZigParityError(f"run result has no book {book.book_id!r}")
        _assert_book_ledger(book.snapshot, engine_book)
    return len(books)


def _assert_book_ledger(replayed: BookSnapshot, engine_book: BookSnapshot) -> None:
    """Compare the books-owned accounting domain of two book snapshots."""
    if replayed.initial_cash != engine_book.initial_cash:
        raise ZigParityError(f"book {replayed.book_id!r} initial cash diverged")
    if replayed.cash != engine_book.cash:
        raise ZigParityError(f"book {replayed.book_id!r} cash ledger diverged")
    if replayed.fill_count != engine_book.fill_count:
        raise ZigParityError(f"book {replayed.book_id!r} fill count diverged")
    if replayed.realized_pnl != engine_book.realized_pnl:
        raise ZigParityError(f"book {replayed.book_id!r} realized PnL ledger diverged")
    replayed_positions = {
        str(position.symbol): position for position in replayed.positions
    }
    engine_positions = {
        str(position.symbol): position for position in engine_book.positions
    }
    symbols = sorted(set(replayed_positions) | set(engine_positions))
    for symbol in symbols:
        left = replayed_positions.get(symbol)
        right = engine_positions.get(symbol)
        if left is None or right is None:
            raise ZigParityError(
                f"book {replayed.book_id!r} position {symbol} is missing from "
                "one ledger"
            )
        for name in ("quantity", "cost_basis", "cash_flow", "realized_pnl"):
            if getattr(left, name) != getattr(right, name):
                raise ZigParityError(
                    f"book {replayed.book_id!r} position {symbol} {name} diverged"
                )


class _ArrowSchema(ctypes.Structure):
    """Mirror of the Arrow C Data Interface ``ArrowSchema`` struct."""

    _fields_ = [
        ("format", ctypes.c_char_p),
        ("name", ctypes.c_char_p),
        ("metadata", ctypes.c_void_p),
        ("flags", ctypes.c_int64),
        ("n_children", ctypes.c_int64),
        ("children", ctypes.c_void_p),
        ("dictionary", ctypes.c_void_p),
        ("release", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("private_data", ctypes.c_void_p),
    ]


class _ArrowArray(ctypes.Structure):
    """Mirror of the Arrow C Data Interface ``ArrowArray`` struct."""

    _fields_ = [
        ("length", ctypes.c_int64),
        ("null_count", ctypes.c_int64),
        ("offset", ctypes.c_int64),
        ("n_buffers", ctypes.c_int64),
        ("n_children", ctypes.c_int64),
        ("buffers", ctypes.POINTER(ctypes.c_void_p)),
        ("children", ctypes.c_void_p),
        ("dictionary", ctypes.c_void_p),
        ("release", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
        ("private_data", ctypes.c_void_p),
    ]


def _release_if_owned(struct: _ArrowSchema | _ArrowArray) -> None:
    """Release the C struct only if the consumer still owns it."""
    callback = struct.release
    if callback:
        callback(ctypes.addressof(struct))


def _arbitrate_arrow_bridge(core: NativeCore) -> int:
    """Consume the pinned decimal batch through the Arrow C Data Interface."""
    import pyarrow as pa

    schema = _ArrowSchema()
    array = _ArrowArray()
    core.arrow_decimal_batch(ctypes.addressof(schema), ctypes.addressof(array))
    try:
        values = pa.Array._import_from_c(  # noqa: SLF001 - pyarrow bridge API
            ctypes.addressof(array), ctypes.addressof(schema)
        )
    except Exception:
        _release_if_owned(schema)
        _release_if_owned(array)
        raise
    expected = [
        Decimal(value).scaleb(-DECIMAL_SCALE) for value in core.decimal_batch_values()
    ]
    if values.to_pylist() != expected:
        raise ZigParityError(
            "Arrow bridge import diverged from the pinned native decimal batch"
        )
    return len(expected)


def _arbitrate_native_kernel(
    core: NativeCore,
    configuration: BacktestConfig,
    result: BacktestResult,
    dispatched: Sequence[Event],
    owners: Mapping[str, str],
) -> ZigKernelEvidence:
    """Arbitrate every ABI v1 kernel operation against the run's outputs."""
    identity_checks = _arbitrate_identifiers(core, configuration)
    symbol_checks = _arbitrate_symbols(core, configuration, dispatched)
    decimal_checks, decimal_skips = _arbitrate_decimals(core, result)
    trade_row_checks = _arbitrate_trade_rows(core, dispatched)
    dispatch_digest = _arbitrate_dispatch(result, dispatched)
    oms_orders, oms_skips = _arbitrate_oms(dispatched, result)
    ledger_books = _arbitrate_ledgers(configuration, result, owners)
    arrow_batch_length = _arbitrate_arrow_bridge(core)
    return ZigKernelEvidence(
        abi_version=core.abi_version,
        identity_checks=identity_checks,
        symbol_checks=symbol_checks,
        decimal_checks=decimal_checks,
        decimal_skips=decimal_skips,
        trade_row_checks=trade_row_checks,
        dispatch_digest=dispatch_digest,
        dispatched_count=len(dispatched),
        oms_orders=oms_orders,
        oms_skips=oms_skips,
        ledger_books=ledger_books,
        arrow_batch_length=arrow_batch_length,
    )


# --------------------------------------------------------------------------
# Public runner entrypoints
# --------------------------------------------------------------------------


def run_zig_backtest(
    configuration: BacktestConfig,
    *,
    data_source: MarketDataSource | None = None,
    strategy_loader: StrategyLoader | None = None,
    fee_schedule: FeeSchedule | None = None,
    risk_pipeline: RiskPipeline | None = None,
    options: BacktestOptions | None = None,
) -> BacktestResult:
    """Run one deterministic backtest driven through the native core.

    The composition and result are :func:`run_backtest`'s; the manifest
    carries the Zig-driven engine identity, and every native-core-reachable
    kernel operation is arbitrated against the run (raising
    :class:`ZigParityError` on any divergence). The native library is a hard
    requirement: a missing, unloadable, or ABI-incompatible library raises
    :class:`~harness._native.NativeCoreUnavailable` with build instructions.
    """
    result, _ = run_zig_backtest_with_evidence(
        configuration,
        data_source=data_source,
        strategy_loader=strategy_loader,
        fee_schedule=fee_schedule,
        risk_pipeline=risk_pipeline,
        options=options,
    )
    return result


def run_zig_backtest_with_evidence(
    configuration: BacktestConfig,
    *,
    data_source: MarketDataSource | None = None,
    strategy_loader: StrategyLoader | None = None,
    fee_schedule: FeeSchedule | None = None,
    risk_pipeline: RiskPipeline | None = None,
    options: BacktestOptions | None = None,
) -> tuple[BacktestResult, ZigKernelEvidence]:
    """Run one Zig-driven backtest and return its result plus kernel evidence."""
    core = load_native_core()
    chosen = BacktestOptions() if options is None else options
    if not isinstance(chosen, BacktestOptions):
        raise BacktestConfigurationError("options must be a BacktestOptions")
    fixture = resolve_data_source(
        configuration, data_source, options=chosen.synthetic_options
    )
    preflight = preflight_data(configuration, fixture)
    plugin = build_fill_model(chosen.fill_model, chosen.fill_options)
    schedule = (
        fee_schedule_from_config(configuration.fees, precision=chosen.fee_precision)
        if fee_schedule is None
        else fee_schedule
    )
    if not isinstance(schedule, FeeSchedule):
        raise BacktestConfigurationError("fee_schedule must be a FeeSchedule")
    pipeline = (
        RiskPipeline(configuration.risk_pipeline)
        if risk_pipeline is None
        else risk_pipeline
    )
    if not isinstance(pipeline, RiskPipeline):
        raise BacktestConfigurationError("risk_pipeline must be a RiskPipeline")
    loader = default_strategy_loader if strategy_loader is None else strategy_loader
    manifest = replace(
        build_manifest(
            configuration,
            fixture=fixture,
            preflight=preflight,
            plugin=plugin,
            schedule=schedule,
            pipeline=pipeline,
            options=chosen,
        ),
        engine_id=ZIG_ENGINE_ID,
    )
    engine = _ZigDrivenEngine(
        configuration,
        fixture,
        preflight,
        manifest,
        options=chosen,
        fee_schedule=schedule,
        plugin=plugin,
        pipeline=pipeline,
        strategy_loader=loader,
        core=core,
    )
    result = engine.run()
    evidence = _arbitrate_native_kernel(
        core, configuration, result, engine.dispatched, engine.order_owners
    )
    return result, evidence


__all__ = [
    "ZIG_ENGINE_ID",
    "ZigKernelEvidence",
    "ZigParityError",
    "run_zig_backtest",
    "run_zig_backtest_with_evidence",
]
