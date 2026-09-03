"""Streaming run recorder and bounded-memory audit trail for harness runs.

A :class:`RunRecorder` turns one deterministic run into referentially linked
Parquet artifacts.  Every artifact is written through a bounded row batch, so a
run of any length keeps at most ``batch_size`` pending rows per artifact in
memory.  Nothing in this module reads wall time, opens a network connection, or
uses unseeded randomness: timestamps come from the caller or an injected
:class:`~harness.core.clock.Clock`, and every generated identifier comes from
:class:`~harness.core.ids.EngineIdSource`.

Artifacts
---------
``actions.parquet``
    Strategy intents that may produce an order.
``orders.parquet``
    Normalized single- and multi-leg orders carrying ``action_id``.
``risk_decisions.parquet`` / ``risk_audits.parquet``
    One row per pipeline decision plus one row per rule invocation.
``fills.parquet``
    Fills extended with their action, decision, position, and equity links.
``execution_reports.parquet``
    OMS reports written with the verbatim core ``REPORT_SCHEMA``.
``position_transitions.parquet``
    Position state before and after each accounting transition.
``equity.parquet``
    Book, portfolio, and account equity snapshots.
``emissions.parquet``
    Structured strategy emissions.
``markers.parquet``
    Position marks and strategy state markers.
``events.parquet``
    Optional full event stream, captured explicitly or from an event bus.
``manifest.parquet``
    Run metadata, per-artifact row counts, and per-artifact byte digests.

The ``orders`` and ``fills`` schemas extend the core ``ORDER_SCHEMA`` and
``FILL_SCHEMA`` with link columns, so they carry the harness schema metadata but
are not byte-equal to the core schemas.  ``execution_reports.parquet`` is the
one artifact that is a verbatim core schema and therefore round-trips through
:class:`~harness.core.types.codecs.ParquetCodec`.

Feeding the recorder
--------------------
A runner records the linked path explicitly::

    action_id = recorder.record_action(ActionKind.SUBMIT, symbol="SPY")
    recorder.record_order(order, action_id=action_id)
    decision_id = recorder.record_risk_decision(decision, action_id=action_id)
    transition_id = recorder.reserve_id("position")
    recorder.record_fill(fill, action_id=action_id, risk_decision_id=decision_id,
                         position_transition_id=transition_id)
    recorder.record_position_transition(position, before=previous,
                                        fill_id=fill.fill_id,
                                        transition_id=transition_id)

Emissions can be routed without glue code because the recorder implements the
strategy :class:`~harness.strategy.context.Emitter` protocol::

    context = StrategyContext(clock, portfolio=hub, emissions=recorder)

The optional full-event stream can be attached to the deterministic bus with
:meth:`RunRecorder.attach_to_bus`, and bulk event iterables can be consumed
without materialization through :meth:`RunRecorder.stream_events`.
"""

from harness.results.recorder.artifacts import artifact_path, read_artifact, read_rows
from harness.results.recorder.config import (
    ActionKind,
    MarkerScope,
    RecorderConfig,
    TransitionReason,
)
from harness.results.recorder.manifest import RunManifest
from harness.results.recorder.recorder import RunRecorder
from harness.results.recorder.schemas import (
    ACTIONS_SCHEMA,
    ARTIFACT_FILES,
    EMISSIONS_SCHEMA,
    EQUITY_SCHEMA,
    EVENTS_SCHEMA,
    FILLS_SCHEMA,
    MANIFEST_SCHEMA,
    MARKERS_SCHEMA,
    ORDERS_SCHEMA,
    POSITION_TRANSITIONS_SCHEMA,
    RECORDER_SCHEMAS,
    RISK_AUDITS_SCHEMA,
    RISK_DECISIONS_SCHEMA,
    STREAM_ARTIFACTS,
    Artifact,
)

__all__ = [
    "ACTIONS_SCHEMA",
    "ARTIFACT_FILES",
    "ActionKind",
    "Artifact",
    "EMISSIONS_SCHEMA",
    "EQUITY_SCHEMA",
    "EVENTS_SCHEMA",
    "FILLS_SCHEMA",
    "MANIFEST_SCHEMA",
    "MARKERS_SCHEMA",
    "MarkerScope",
    "ORDERS_SCHEMA",
    "POSITION_TRANSITIONS_SCHEMA",
    "RECORDER_SCHEMAS",
    "RISK_AUDITS_SCHEMA",
    "RISK_DECISIONS_SCHEMA",
    "RunManifest",
    "RunRecorder",
    "RecorderConfig",
    "STREAM_ARTIFACTS",
    "TransitionReason",
    "artifact_path",
    "read_artifact",
    "read_rows",
]
