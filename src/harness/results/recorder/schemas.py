"""Artifact names, Parquet schemas, and core-schema extension helpers.

Every recorded artifact schema carries harness metadata naming the recorder
schema (``recorder.<artifact>``), the harness schema version, and the
string-exact decimal encoding.  The ``orders`` and ``fills`` schemas extend the
core ``ORDER_SCHEMA`` and ``FILL_SCHEMA`` with link columns, so they carry the
harness schema metadata but are not byte-equal to the core schemas.
``execution_reports`` is the one artifact whose schema is the verbatim core
``REPORT_SCHEMA``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import pyarrow as pa

from harness.core.types.common import SCHEMA_VERSION
from harness.core.types.schemas import FILL_SCHEMA, ORDER_SCHEMA, REPORT_SCHEMA


class Artifact(StrEnum):
    """Named Parquet artifacts produced by one recorded run."""

    ACTIONS = "actions"
    ORDERS = "orders"
    RISK_DECISIONS = "risk_decisions"
    RISK_AUDITS = "risk_audits"
    FILLS = "fills"
    EXECUTION_REPORTS = "execution_reports"
    POSITION_TRANSITIONS = "position_transitions"
    EQUITY = "equity"
    EMISSIONS = "emissions"
    MARKERS = "markers"
    EVENTS = "events"
    MANIFEST = "manifest"


STREAM_ARTIFACTS: Final[tuple[Artifact, ...]] = tuple(
    artifact for artifact in Artifact if artifact is not Artifact.MANIFEST
)
ARTIFACT_FILES: Final[Mapping[Artifact, str]] = MappingProxyType(
    {artifact: f"{artifact.value}.parquet" for artifact in Artifact}
)


def _field(name: str, data_type: pa.DataType, *, nullable: bool = False) -> pa.Field:
    return pa.field(name, data_type, nullable=nullable)


def _schema(name: str, fields: Sequence[pa.Field]) -> pa.Schema:
    return pa.schema(
        fields,
        metadata={
            b"harness.schema": f"recorder.{name}".encode("ascii"),
            b"harness.schema.version": SCHEMA_VERSION.encode("ascii"),
            b"harness.decimal.encoding": b"string-exact",
        },
    )


def _core_field(
    schema: pa.Schema, name: str, *, nullable: bool | None = None
) -> pa.Field:
    """Reuse one field definition from an existing core schema."""
    found = schema.field(name)
    return found if nullable is None else found.with_nullable(nullable)


_DECIMAL: Final = pa.string()
_SEQUENCE: Final = pa.uint64()
_STAMP: Final = pa.timestamp("ns", tz="UTC")
_COUNT: Final = pa.uint32()

ACTIONS_SCHEMA: Final = _schema(
    "actions",
    (
        _field("action_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("strategy_id", pa.string()),
        _field("book_id", pa.string(), nullable=True),
        _field("kind", pa.string()),
        _field("symbol", pa.string(), nullable=True),
        _field("side", pa.string(), nullable=True),
        _field("quantity", _DECIMAL, nullable=True),
        _field("order_id", pa.string(), nullable=True),
        _field("reason", pa.string(), nullable=True),
        _field("detail", pa.string(), nullable=True),
    ),
)
ORDERS_SCHEMA: Final = _schema(
    "orders",
    (
        _field("order_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("event_id", pa.string()),
        _field("action_id", pa.string(), nullable=True),
        _field("strategy_id", pa.string()),
        _field("book_id", pa.string(), nullable=True),
        _field("kind", pa.string()),
        _core_field(ORDER_SCHEMA, "symbol", nullable=True),
        _core_field(ORDER_SCHEMA, "side", nullable=True),
        _core_field(ORDER_SCHEMA, "quantity", nullable=True),
        _core_field(ORDER_SCHEMA, "order_type"),
        _core_field(ORDER_SCHEMA, "time_in_force"),
        _core_field(ORDER_SCHEMA, "limit_price"),
        _core_field(ORDER_SCHEMA, "stop_price"),
        _core_field(ORDER_SCHEMA, "client_order_id"),
        _field("legs", pa.string()),
    ),
)
RISK_DECISIONS_SCHEMA: Final = _schema(
    "risk_decisions",
    (
        _field("decision_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("strategy_id", pa.string()),
        _field("action_id", pa.string(), nullable=True),
        _field("order_id", pa.string()),
        _field("action", pa.string()),
        _field("approved", pa.bool_()),
        _field("halted", pa.bool_()),
        _field("reason", pa.string(), nullable=True),
        _field("reasons", pa.string()),
        _field("audit_count", _COUNT),
    ),
)
RISK_AUDITS_SCHEMA: Final = _schema(
    "risk_audits",
    (
        _field("audit_id", pa.string()),
        _field("decision_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("rule_id", pa.string()),
        _field("pass_number", _COUNT),
        _field("audit_sequence", _SEQUENCE),
        _field("action", pa.string()),
        _field("recheck", pa.bool_()),
        _field("halt", pa.bool_()),
        _field("order_id", pa.string()),
        _field("reason", pa.string(), nullable=True),
        _field("metrics", pa.string()),
    ),
)
FILLS_SCHEMA: Final = _schema(
    "fills",
    (
        _field("fill_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _core_field(FILL_SCHEMA, "event_id"),
        _core_field(FILL_SCHEMA, "order_id"),
        _field("action_id", pa.string(), nullable=True),
        _field("risk_decision_id", pa.string(), nullable=True),
        _field("position_transition_id", pa.string(), nullable=True),
        _field("equity_snapshot_id", pa.string(), nullable=True),
        _core_field(FILL_SCHEMA, "symbol"),
        _core_field(FILL_SCHEMA, "side"),
        _core_field(FILL_SCHEMA, "quantity"),
        _core_field(FILL_SCHEMA, "price"),
        _core_field(FILL_SCHEMA, "fee"),
        _core_field(FILL_SCHEMA, "liquidity"),
    ),
)
POSITION_TRANSITIONS_SCHEMA: Final = _schema(
    "position_transitions",
    (
        _field("transition_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("account_id", pa.string()),
        _field("book_id", pa.string(), nullable=True),
        _field("symbol", pa.string()),
        _field("reason", pa.string()),
        _field("fill_id", pa.string(), nullable=True),
        _field("quantity_before", _DECIMAL),
        _field("quantity_after", _DECIMAL),
        _field("cost_basis", _DECIMAL),
        _field("cash_flow", _DECIMAL),
        _field("realized_pnl", _DECIMAL),
        _field("mark_price", _DECIMAL, nullable=True),
        _field("market_value", _DECIMAL, nullable=True),
        _field("unrealized_pnl", _DECIMAL, nullable=True),
    ),
)
EQUITY_SCHEMA: Final = _schema(
    "equity",
    (
        _field("snapshot_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("account_id", pa.string()),
        _field("book_id", pa.string(), nullable=True),
        _field("source", pa.string()),
        _field("cash", _DECIMAL),
        _field("equity", _DECIMAL, nullable=True),
        _field("market_value", _DECIMAL, nullable=True),
        _field("realized_pnl", _DECIMAL),
        _field("unrealized_pnl", _DECIMAL, nullable=True),
        _field("total_pnl", _DECIMAL, nullable=True),
        _field("position_count", _COUNT),
        _field("fill_count", _SEQUENCE),
    ),
)
EMISSIONS_SCHEMA: Final = _schema(
    "emissions",
    (
        _field("emission_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("strategy_id", pa.string()),
        _field("book_id", pa.string(), nullable=True),
        _field("name", pa.string()),
        _field("fields", pa.string()),
    ),
)
MARKERS_SCHEMA: Final = _schema(
    "markers",
    (
        _field("marker_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("strategy_id", pa.string()),
        _field("scope", pa.string()),
        _field("key", pa.string()),
        _field("value", pa.string(), nullable=True),
        _field("detail", pa.string(), nullable=True),
    ),
)
EVENTS_SCHEMA: Final = _schema(
    "events",
    (
        _field("event_id", pa.string()),
        _field("sequence", _SEQUENCE),
        _field("timestamp", _STAMP),
        _field("event_type", pa.string()),
        _field("type_name", pa.string()),
        _field("symbol", pa.string(), nullable=True),
        _field("payload", pa.string(), nullable=True),
    ),
)
MANIFEST_SCHEMA: Final = _schema(
    "manifest",
    (_field("key", pa.string()), _field("value", pa.string())),
)

RECORDER_SCHEMAS: Final[Mapping[Artifact, pa.Schema]] = MappingProxyType(
    {
        Artifact.ACTIONS: ACTIONS_SCHEMA,
        Artifact.ORDERS: ORDERS_SCHEMA,
        Artifact.RISK_DECISIONS: RISK_DECISIONS_SCHEMA,
        Artifact.RISK_AUDITS: RISK_AUDITS_SCHEMA,
        Artifact.FILLS: FILLS_SCHEMA,
        Artifact.EXECUTION_REPORTS: REPORT_SCHEMA,
        Artifact.POSITION_TRANSITIONS: POSITION_TRANSITIONS_SCHEMA,
        Artifact.EQUITY: EQUITY_SCHEMA,
        Artifact.EMISSIONS: EMISSIONS_SCHEMA,
        Artifact.MARKERS: MARKERS_SCHEMA,
        Artifact.EVENTS: EVENTS_SCHEMA,
        Artifact.MANIFEST: MANIFEST_SCHEMA,
    }
)
