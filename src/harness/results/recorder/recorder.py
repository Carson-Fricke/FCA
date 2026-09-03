"""The streaming :class:`RunRecorder`.

A :class:`RunRecorder` turns one deterministic run into referentially linked
Parquet artifacts.  Every artifact is written through a bounded row batch, so a
run of any length keeps at most ``batch_size`` pending rows per artifact in
memory.  Nothing in this module reads wall time, opens a network connection, or
uses unseeded randomness: timestamps come from the caller or an injected
:class:`~harness.core.clock.Clock`, and every generated identifier comes from
:class:`~harness.core.ids.EngineIdSource`.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Self

import pyarrow as pa
import pyarrow.parquet as pq

from harness.core.events import EventBus
from harness.core.ids import EngineIdSource
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types.accounting import (
    AccountSnapshot,
    ExecutionReport,
    Fill,
    Position,
)
from harness.core.types.common import (
    SCHEMA_VERSION,
    decimal_text,
    exact_decimal,
    text,
    timestamp,
)
from harness.core.types.events import Event, EventType
from harness.core.types.orders import (
    MultiLegOrder,
    OrderRequest,
    Side,
    SingleLegOrder,
)
from harness.portfolio import (
    BookSnapshot,
    PortfolioSnapshot,
    PositionMarker,
    PositionSnapshot,
)
from harness.results.recorder.artifacts import artifact_path
from harness.results.recorder.config import (
    ActionKind,
    MarkerScope,
    RecorderConfig,
    TransitionReason,
)
from harness.results.recorder.manifest import (
    RunManifest,
    _file_digest,
    _seed_text,
    _truth_text,
)
from harness.results.recorder.schemas import (
    MANIFEST_SCHEMA,
    RECORDER_SCHEMAS,
    STREAM_ARTIFACTS,
    Artifact,
)
from harness.results.recorder.writer import (
    _ArtifactWriter,
    _enum_value,
    _json_text,
    _optional_text,
)
from harness.risk.models import RiskAudit, RiskDecision

_ZERO = Decimal("0")


def _sum_market_value(positions: Sequence[Position]) -> Decimal | None:
    total = _ZERO
    for position in positions:
        if position.market_price is None:
            return None
        total += position.quantity * position.market_price
    return total


def _sum_unrealized_pnl(positions: Sequence[Position]) -> Decimal | None:
    total = _ZERO
    for position in positions:
        if position.unrealized_pnl is None:
            return None
        total += position.unrealized_pnl
    return total


class RunRecorder:
    """Stream one deterministic run into linked, bounded-memory artifacts."""

    __slots__ = (
        "_closed",
        "_config",
        "_field_names_by_type",
        "_ids",
        "_manifest",
        "_sequence",
        "_type_names",
        "_writers",
    )

    def __init__(self, config: RecorderConfig) -> None:
        if not isinstance(config, RecorderConfig):
            raise TypeError("config must be a RecorderConfig")
        self._config = config
        self._ids = EngineIdSource(config.seed)
        self._sequence = 0
        self._closed = False
        self._manifest: RunManifest | None = None
        self._type_names: dict[type[Event], str] = {}
        self._field_names_by_type: dict[type[Event], tuple[str, ...]] = {}
        directory = Path(config.directory)
        directory.mkdir(parents=True, exist_ok=True)
        self._writers: Mapping[Artifact, _ArtifactWriter] = MappingProxyType(
            {
                artifact: _ArtifactWriter(
                    artifact_path(directory, artifact),
                    RECORDER_SCHEMAS[artifact],
                    batch_size=config.batch_size,
                    compression=config.compression,
                    version=config.parquet_version,
                )
                for artifact in STREAM_ARTIFACTS
            }
        )

    @property
    def config(self) -> RecorderConfig:
        """Return the recorder configuration."""
        return self._config

    @property
    def closed(self) -> bool:
        """Whether the run has been finalized."""
        return self._closed

    @property
    def buffered_rows(self) -> int:
        """Return the largest pending row count across every artifact."""
        return max(writer.pending_rows for writer in self._writers.values())

    @property
    def row_counts(self) -> Mapping[str, int]:
        """Return rows flushed so far per artifact name."""
        return MappingProxyType(
            {
                artifact.value: writer.row_count + writer.pending_rows
                for artifact, writer in self._writers.items()
            }
        )

    @property
    def manifest(self) -> RunManifest:
        """Return the run manifest, which only exists after :meth:`close`."""
        if self._manifest is None:
            raise RuntimeError("the run manifest is only available after close()")
        return self._manifest

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def reserve_id(self, kind: str) -> str:
        """Issue the next deterministic identifier for forward linking."""
        return self._ids.next_id(kind)

    def record_action(
        self,
        kind: ActionKind | str,
        *,
        strategy_id: str | None = None,
        book_id: str | None = None,
        symbol: str | Symbol | None = None,
        side: Side | str | None = None,
        quantity: Decimal | int | str | None = None,
        order_id: str | None = None,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one strategy intent and return its ``action_id``."""
        action_id = self._ids.next_id("action")
        self._write(
            Artifact.ACTIONS,
            {
                "action_id": action_id,
                "sequence": self._next_sequence(),
                "timestamp": self._resolve_timestamp(at),
                "strategy_id": self._strategy_id(strategy_id),
                "book_id": self._book_id(book_id),
                "kind": _enum_value(kind, ActionKind, "kind"),
                "symbol": None if symbol is None else str(canonical_symbol(symbol)),
                "side": None if side is None else Side(side).value,
                "quantity": (
                    None
                    if quantity is None
                    else str(exact_decimal(quantity, "quantity"))
                ),
                "order_id": _optional_text(order_id, "order_id"),
                "reason": _optional_text(reason, "reason"),
                "detail": None
                if detail is None
                else _json_text(dict(detail), "detail"),
            },
        )
        return action_id

    def record_order(
        self,
        order: OrderRequest,
        *,
        action_id: str | None = None,
        strategy_id: str | None = None,
        book_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one single- or multi-leg order and return its ``order_id``."""
        if not isinstance(order, (SingleLegOrder, MultiLegOrder)):
            raise TypeError("order must be a SingleLegOrder or MultiLegOrder")
        if isinstance(order, MultiLegOrder):
            symbol: str | None = None
            side: str | None = None
            quantity: str | None = None
            stop_price: str | None = None
            kind = "multi"
            legs = _json_text(
                [
                    {
                        "symbol": str(leg.symbol),
                        "side": leg.side.value,
                        "quantity": str(leg.quantity),
                        "ratio": str(leg.ratio),
                        "leg_id": leg.leg_id,
                    }
                    for leg in order.legs
                ],
                "legs",
            )
        else:
            symbol = str(order.symbol)
            side = order.side.value
            quantity = str(order.quantity)
            stop_price = decimal_text(order.stop_price)
            kind = "single"
            legs = "[]"
        self._write(
            Artifact.ORDERS,
            {
                "order_id": order.order_id,
                "sequence": order.sequence,
                "timestamp": order.timestamp
                if at is None
                else self._resolve_timestamp(at),
                "event_id": order.event_id,
                "action_id": _optional_text(action_id, "action_id"),
                "strategy_id": self._strategy_id(strategy_id),
                "book_id": self._book_id(book_id),
                "kind": kind,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "order_type": order.order_type.value,
                "time_in_force": order.time_in_force.value,
                "limit_price": decimal_text(order.limit_price),
                "stop_price": stop_price,
                "client_order_id": order.client_order_id,
                "legs": legs,
            },
        )
        return str(order.order_id)

    def record_risk_decision(
        self,
        decision: RiskDecision,
        *,
        action_id: str | None = None,
        strategy_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one risk decision and every rule audit that produced it."""
        if not isinstance(decision, RiskDecision):
            raise TypeError("decision must be a RiskDecision")
        decision_id = self._ids.next_id("risk")
        stamp = self._resolve_timestamp(at)
        decision_sequence = self._next_sequence()
        self._write(
            Artifact.RISK_DECISIONS,
            {
                "decision_id": decision_id,
                "sequence": decision_sequence,
                "timestamp": stamp,
                "strategy_id": self._strategy_id(
                    strategy_id if strategy_id is not None else decision.strategy_id
                ),
                "action_id": _optional_text(action_id, "action_id"),
                "order_id": decision.final_order.order_id,
                "action": decision.action.value,
                "approved": decision.approved,
                "halted": decision.halted,
                "reason": decision.reason,
                "reasons": _json_text(list(decision.reasons), "reasons"),
                "audit_count": len(decision.audits),
            },
        )
        for audit in decision.audits:
            self._record_risk_audit(audit, decision_id, stamp)
        return decision_id

    def record_fill(
        self,
        fill: Fill,
        *,
        action_id: str | None = None,
        risk_decision_id: str | None = None,
        position_transition_id: str | None = None,
        equity_snapshot_id: str | None = None,
    ) -> str:
        """Record one fill with every link known at recording time."""
        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        self._write(
            Artifact.FILLS,
            {
                "fill_id": fill.fill_id,
                "sequence": fill.sequence,
                "timestamp": fill.timestamp,
                "event_id": fill.event_id,
                "order_id": fill.order_id,
                "action_id": _optional_text(action_id, "action_id"),
                "risk_decision_id": _optional_text(
                    risk_decision_id, "risk_decision_id"
                ),
                "position_transition_id": _optional_text(
                    position_transition_id, "position_transition_id"
                ),
                "equity_snapshot_id": _optional_text(
                    equity_snapshot_id, "equity_snapshot_id"
                ),
                "symbol": str(fill.symbol),
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "liquidity": fill.liquidity,
            },
        )
        return str(fill.fill_id)

    def record_execution_report(self, report: ExecutionReport) -> str:
        """Record one OMS execution report using the verbatim core schema."""
        if not isinstance(report, ExecutionReport):
            raise TypeError("report must be an ExecutionReport")
        self._write(
            Artifact.EXECUTION_REPORTS,
            {
                "event_id": report.event_id,
                "timestamp": report.timestamp,
                "sequence": report.sequence,
                "report_id": report.report_id,
                "order_id": report.order_id,
                "status": report.status.value,
                "execution_type": report.execution_type.value,
                "order_quantity": str(report.order_quantity),
                "filled_quantity": str(report.filled_quantity),
                "remaining_quantity": str(report.remaining_quantity),
                "average_fill_price": decimal_text(report.average_fill_price),
                "last_fill_quantity": str(report.last_fill_quantity),
                "last_fill_price": decimal_text(report.last_fill_price),
                "text": report.text,
            },
        )
        return str(report.report_id)

    def record_position_transition(
        self,
        after: PositionSnapshot,
        *,
        before: PositionSnapshot | None = None,
        account_id: str | None = None,
        book_id: str | None = None,
        reason: TransitionReason | str = TransitionReason.FILL,
        fill_id: str | None = None,
        transition_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one position transition and return its ``transition_id``.

        Pass the identifier returned by :meth:`reserve_id` when a fill already
        recorded a link to this transition.
        """
        if not isinstance(after, PositionSnapshot):
            raise TypeError("after must be a PositionSnapshot")
        if before is not None and not isinstance(before, PositionSnapshot):
            raise TypeError("before must be a PositionSnapshot")
        transition_id = (
            self._ids.next_id("position")
            if transition_id is None
            else text(transition_id, "transition_id")
        )
        self._write(
            Artifact.POSITION_TRANSITIONS,
            {
                "transition_id": transition_id,
                "sequence": self._next_sequence(),
                "timestamp": self._resolve_timestamp(at),
                "account_id": self._account_id(account_id),
                "book_id": self._book_id(book_id),
                "symbol": str(after.symbol),
                "reason": _enum_value(reason, TransitionReason, "reason"),
                "fill_id": _optional_text(fill_id, "fill_id"),
                "quantity_before": str(_ZERO if before is None else before.quantity),
                "quantity_after": str(after.quantity),
                "cost_basis": str(after.cost_basis),
                "cash_flow": str(after.cash_flow),
                "realized_pnl": str(after.realized_pnl),
                "mark_price": decimal_text(after.mark_price),
                "market_value": decimal_text(after.market_value),
                "unrealized_pnl": decimal_text(after.unrealized_pnl),
            },
        )
        return transition_id

    def record_equity_snapshot(
        self,
        snapshot: PortfolioSnapshot | BookSnapshot | AccountSnapshot,
        *,
        account_id: str | None = None,
        book_id: str | None = None,
        snapshot_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one book, portfolio, or account valuation snapshot.

        Aggregate portfolio and account snapshots are recorded without a book,
        while a book snapshot carries its own identifier.  Pass the identifier
        returned by :meth:`reserve_id` when another record already links here.
        """
        if isinstance(snapshot, PortfolioSnapshot):
            source = "portfolio"
            resolved_book_id = None
            resolved_account_id = snapshot.account_id
            cash = snapshot.cash
            equity = snapshot.equity
            market_value = snapshot.market_value
            realized_pnl = snapshot.realized_pnl
            unrealized_pnl = snapshot.unrealized_pnl
            total_pnl = snapshot.total_pnl
            position_count = len(snapshot.positions)
            fill_count = snapshot.fill_count
        elif isinstance(snapshot, BookSnapshot):
            source = "book"
            resolved_book_id = snapshot.book_id
            resolved_account_id = None
            cash = snapshot.cash
            equity = snapshot.equity
            market_value = snapshot.market_value
            realized_pnl = snapshot.realized_pnl
            unrealized_pnl = snapshot.unrealized_pnl
            total_pnl = snapshot.total_pnl
            position_count = len(snapshot.positions)
            fill_count = snapshot.fill_count
        elif isinstance(snapshot, AccountSnapshot):
            source = "account"
            resolved_book_id = None
            resolved_account_id = snapshot.account_id
            cash = snapshot.cash
            equity = snapshot.equity
            market_value = _sum_market_value(snapshot.positions)
            realized_pnl = sum(
                (position.realized_pnl for position in snapshot.positions),
                start=_ZERO,
            )
            unrealized_pnl = _sum_unrealized_pnl(snapshot.positions)
            total_pnl = None
            position_count = len(snapshot.positions)
            fill_count = 0
        else:
            raise TypeError(
                "snapshot must be a PortfolioSnapshot, BookSnapshot, or AccountSnapshot"
            )
        resolved_snapshot_id = (
            self._ids.next_id("equity")
            if snapshot_id is None
            else text(snapshot_id, "snapshot_id")
        )
        self._write(
            Artifact.EQUITY,
            {
                "snapshot_id": resolved_snapshot_id,
                "sequence": self._next_sequence(),
                "timestamp": self._resolve_timestamp(at),
                "account_id": self._account_id(
                    account_id if account_id is not None else resolved_account_id
                ),
                "book_id": (
                    text(book_id, "book_id")
                    if book_id is not None
                    else resolved_book_id
                ),
                "source": source,
                "cash": str(cash),
                "equity": decimal_text(equity),
                "market_value": decimal_text(market_value),
                "realized_pnl": str(realized_pnl),
                "unrealized_pnl": decimal_text(unrealized_pnl),
                "total_pnl": decimal_text(total_pnl),
                "position_count": position_count,
                "fill_count": fill_count,
            },
        )
        return resolved_snapshot_id

    def record_emission(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        *,
        strategy_id: str | None = None,
        book_id: str | None = None,
        at: datetime | None = None,
        **values: object,
    ) -> str:
        """Record one structured emission and return its ``emission_id``."""
        combined: dict[str, object] = {} if fields is None else dict(fields)
        combined.update(values)
        return self._write_emission(
            combined,
            name=name,
            strategy_id=strategy_id,
            book_id=book_id,
            at=at,
        )

    def emit(
        self,
        name: str,
        fields: Mapping[str, object] | None = None,
        **values: object,
    ) -> None:
        """Implement the strategy :class:`Emitter` protocol."""
        combined: dict[str, object] = {} if fields is None else dict(fields)
        combined.update(values)
        self._write_emission(
            combined, name=name, strategy_id=None, book_id=None, at=None
        )

    def _write_emission(
        self,
        fields: Mapping[str, object],
        *,
        name: str,
        strategy_id: str | None,
        book_id: str | None,
        at: datetime | None,
    ) -> str:
        emission_id = self._ids.next_id("emission")
        self._write(
            Artifact.EMISSIONS,
            {
                "emission_id": emission_id,
                "sequence": self._next_sequence(),
                "timestamp": self._resolve_timestamp(at),
                "strategy_id": self._strategy_id(strategy_id),
                "book_id": self._book_id(book_id),
                "name": text(name, "name"),
                "fields": _json_text(dict(fields), "fields"),
            },
        )
        return emission_id

    def record_marker(
        self,
        key: str,
        value: str | None = None,
        *,
        scope: MarkerScope | str = MarkerScope.STATE,
        strategy_id: str | None = None,
        detail: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one state or run marker and return its ``marker_id``."""
        marker_id = self._ids.next_id("marker")
        self._write(
            Artifact.MARKERS,
            {
                "marker_id": marker_id,
                "sequence": self._next_sequence(),
                "timestamp": self._resolve_timestamp(at),
                "strategy_id": self._strategy_id(strategy_id),
                "scope": _enum_value(scope, MarkerScope, "scope"),
                "key": text(key, "key"),
                "value": _optional_text(value, "value"),
                "detail": None
                if detail is None
                else _json_text(dict(detail), "detail"),
            },
        )
        return marker_id

    def record_position_marker(
        self,
        marker: PositionMarker,
        *,
        strategy_id: str | None = None,
        book_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        """Record one explicit position mark and return its ``marker_id``."""
        if not isinstance(marker, PositionMarker):
            raise TypeError("marker must be a PositionMarker")
        return self.record_marker(
            str(marker.symbol),
            str(marker.price),
            scope=MarkerScope.POSITION,
            strategy_id=strategy_id,
            detail={"book_id": self._book_id(book_id)},
            at=at,
        )

    def record_event(self, event: Event) -> None:
        """Record one event in the optional full event stream."""
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        symbol = getattr(event, "symbol", None)
        payload: str | None = None
        if self._config.record_payloads:
            payload = _json_text(
                {name: getattr(event, name) for name in self._field_names(type(event))},
                "payload",
            )
        self._write(
            Artifact.EVENTS,
            {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "event_type": event.event_type.value,
                "type_name": self._type_name(type(event)),
                "symbol": None if symbol is None else str(symbol),
                "payload": payload,
            },
        )

    def stream_events(self, events: Iterable[Event]) -> int:
        """Consume events one at a time and return how many were recorded."""
        count = 0
        for event in events:
            self.record_event(event)
            count += 1
        return count

    def attach_to_bus(self, bus: EventBus) -> tuple[EventType, ...]:
        """Subscribe the optional full event stream to a deterministic bus."""
        if not isinstance(bus, EventBus):
            raise TypeError("bus must be an EventBus")
        if not self._config.record_events:
            raise ValueError("record_events must be enabled to attach to a bus")
        for event_type in EventType:
            bus.subscribe(event_type, self.record_event)
        return tuple(EventType)

    def close(self) -> RunManifest:
        """Finalize every artifact and return the run manifest."""
        if self._closed:
            assert self._manifest is not None
            return self._manifest
        digests: dict[str, str] = {}
        row_counts: dict[str, int] = {}
        for artifact in STREAM_ARTIFACTS:
            writer = self._writers[artifact]
            writer.close()
            row_counts[artifact.value] = writer.row_count
            digests[artifact.value] = _file_digest(
                artifact_path(self._config.directory, artifact)
            )
        fingerprint = hashlib.sha256()
        for artifact in STREAM_ARTIFACTS:
            name = artifact.value
            fingerprint.update(
                f"{name}\0{row_counts[name]}\0{digests[name]}\n".encode()
            )
        rows = self._manifest_rows(row_counts, digests, fingerprint.hexdigest())
        row_counts[Artifact.MANIFEST.value] = len(rows)
        self._write_manifest(rows)
        self._closed = True
        self._manifest = RunManifest(
            run_id=self._config.run_id,
            directory=Path(self._config.directory),
            row_counts=MappingProxyType(dict(sorted(row_counts.items()))),
            digests=MappingProxyType(digests),
            run_digest=fingerprint.hexdigest(),
            metadata=MappingProxyType(dict(self._config.metadata)),
        )
        return self._manifest

    def _record_risk_audit(
        self, audit: RiskAudit, decision_id: str, stamp: datetime
    ) -> None:
        self._write(
            Artifact.RISK_AUDITS,
            {
                "audit_id": self._ids.next_id("audit"),
                "decision_id": decision_id,
                "sequence": self._next_sequence(),
                "timestamp": stamp,
                "rule_id": audit.rule_id,
                "pass_number": audit.pass_number,
                "audit_sequence": audit.sequence,
                "action": audit.action.value,
                "recheck": audit.recheck,
                "halt": audit.halt,
                "order_id": audit.order_after.order_id,
                "reason": audit.reason,
                "metrics": _json_text(dict(audit.metrics), "metrics"),
            },
        )

    def _manifest_rows(
        self,
        row_counts: Mapping[str, int],
        digests: Mapping[str, str],
        run_digest: str,
    ) -> tuple[dict[str, str], ...]:
        config = self._config
        entries: list[tuple[str, str]] = [
            ("run_id", config.run_id),
            ("schema_version", SCHEMA_VERSION),
            ("seed", str(_seed_text(config.seed))),
            ("batch_size", str(config.batch_size)),
            ("compression", config.compression),
            ("parquet_version", config.parquet_version),
            ("record_events", _truth_text(config.record_events)),
            ("record_payloads", _truth_text(config.record_payloads)),
            ("strategy_id", config.strategy_id or ""),
            ("book_id", config.book_id or ""),
            ("account_id", config.account_id or ""),
            *(
                (f"metadata:{key}", value)
                for key, value in sorted(config.metadata.items())
            ),
        ]
        for artifact in STREAM_ARTIFACTS:
            name = artifact.value
            entries.append((f"artifact:{name}", str(row_counts[name])))
            entries.append((f"digest:{name}", digests[name]))
        entries.append(("run_digest", run_digest))
        entries.append(("row_count", str(len(entries) + 1)))
        return tuple({"key": key, "value": value} for key, value in entries)

    def _write_manifest(self, rows: Sequence[Mapping[str, str]]) -> None:
        table = pa.Table.from_pylist(
            [dict(row) for row in rows], schema=MANIFEST_SCHEMA
        )
        path = artifact_path(self._config.directory, Artifact.MANIFEST)
        with pq.ParquetWriter(
            path,
            MANIFEST_SCHEMA,
            compression=self._config.compression,
            version=self._config.parquet_version,
        ) as writer:
            writer.write_table(table)

    def _write(self, artifact: Artifact, row: Mapping[str, object]) -> None:
        if self._closed:
            raise RuntimeError("the recorder is closed")
        self._writers[artifact].append(row)

    def _type_name(self, event_type: type[Event]) -> str:
        name = self._type_names.get(event_type)
        if name is None:
            name = f"{event_type.__module__}.{event_type.__qualname__}"
            self._type_names[event_type] = name
        return name

    def _field_names(self, event_type: type[Event]) -> tuple[str, ...]:
        names = self._field_names_by_type.get(event_type)
        if names is None:
            names = tuple(item.name for item in dataclasses.fields(event_type))
            self._field_names_by_type[event_type] = names
        return names

    def _next_sequence(self) -> int:
        sequence = self._sequence
        self._sequence = sequence + 1
        return sequence

    def _resolve_timestamp(self, at: datetime | None) -> datetime:
        if at is not None:
            return timestamp(at)
        if self._config.clock is not None:
            return timestamp(self._config.clock.now())
        raise ValueError(
            "timestamp is required when the recorder has no configured clock"
        )

    def _strategy_id(self, strategy_id: str | None) -> str:
        if strategy_id is not None:
            return text(strategy_id, "strategy_id")
        return self._config.strategy_id or ""

    def _book_id(self, book_id: str | None) -> str | None:
        if book_id is not None:
            return text(book_id, "book_id")
        return self._config.book_id

    def _account_id(self, account_id: str | None) -> str:
        if account_id is not None:
            return text(account_id, "account_id")
        configured = self._config.account_id
        if configured is None:
            raise ValueError("account_id is required for this record")
        return configured
