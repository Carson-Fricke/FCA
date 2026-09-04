"""Engine identity, reproducibility manifest, and canonical run artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Final

from harness.configuration import BacktestConfig, canonical_hash
from harness.core.types import Fill
from harness.fills import FeeAggregate, FeeAggregation, FeeAssessment
from harness.portfolio import PortfolioSnapshot
from harness.risk import RiskDecision
from harness.runtime.backtest.errors import BacktestStatus
from harness.runtime.backtest.fixture import DataPreflight

MANIFEST_VERSION: Final = "1"
ENGINE_ID: Final = "harness.backtest"
ENGINE_VERSION: Final = "1"


# --------------------------------------------------------------------------
# Code and engine identity
# --------------------------------------------------------------------------

_ENGINE_MODULES: Final = (
    "harness.core.clock",
    "harness.core.events",
    "harness.data.replay",
    "harness.fills.fees",
    "harness.fills.latency_size",
    "harness.fills.simple_touch",
    "harness.fills.trade_through",
    "harness.portfolio.book",
    "harness.portfolio.hub",
    "harness.risk.pipeline",
    "harness.strategy.binding",
    "harness.strategy.context",
    "harness.venues.sim",
    "harness.runtime.backtest",
    "harness.runtime.backtest.errors",
    "harness.runtime.backtest.fills",
    "harness.runtime.backtest.fees",
    "harness.runtime.backtest.fixture",
    "harness.runtime.backtest.manifest",
    "harness.runtime.backtest.preflight",
    "harness.runtime.backtest.strategies",
    "harness.runtime.backtest.engine",
)


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Return a stable SHA-256 fingerprint of the deterministic engine code.

    Source bytes are newline-normalized so the same commit produces the same
    fingerprint on every machine and checkout style.
    """
    digest = hashlib.sha256()
    digest.update(b"fca-backtest-code-v1\0")
    for name in _ENGINE_MODULES:
        module = import_module(name)
        source = getattr(module, "__file__", None)
        payload = (
            b"" if source is None else Path(source).read_bytes().replace(b"\r\n", b"\n")
        )
        frame = name.encode("utf-8") + b"\0" + payload
        digest.update(len(frame).to_bytes(8, "big"))
        digest.update(frame)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Manifest and result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    """Reproducibility identity for one configured strategy instance."""

    instance_id: str
    implementation: str
    bundle_hash: str
    parameter_hash: str
    symbols: tuple[str, ...]
    capital_fraction: str

    def to_json(self) -> dict[str, object]:
        """Return a canonical JSON-ready representation."""
        return {
            "instance_id": self.instance_id,
            "implementation": self.implementation,
            "bundle_hash": self.bundle_hash,
            "parameter_hash": self.parameter_hash,
            "symbols": list(self.symbols),
            "capital_fraction": self.capital_fraction,
        }


@dataclass(frozen=True, slots=True)
class BacktestManifest:
    """Every reproducibility input for one deterministic backtest run."""

    manifest_version: str
    engine_id: str
    engine_version: str
    code_fingerprint: str
    run_id: str
    shell: str
    schema_version: int
    configuration_hash: str
    seed: int
    start: str
    end: str
    base_currency: str
    initial_cash: str
    data_snapshot_id: str
    data_fingerprint: str
    data_source_id: str
    replay_file_fingerprint: str
    replay_row_fingerprint: str
    replay_data_fingerprint: str
    fill_model: str
    fill_model_fingerprint: str
    fee_schedule: str
    fee_fingerprint: str
    risk_pipeline_fingerprint: str
    strategies: tuple[StrategyIdentity, ...]

    @property
    def manifest_hash(self) -> str:
        """Return the SHA-256 hash of every reproducibility input."""
        return canonical_hash(self.to_json())

    def to_json(self) -> dict[str, object]:
        """Return a canonical JSON-ready representation."""
        return {
            "manifest_version": self.manifest_version,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "code_fingerprint": self.code_fingerprint,
            "run_id": self.run_id,
            "shell": self.shell,
            "schema_version": self.schema_version,
            "configuration_hash": self.configuration_hash,
            "seed": self.seed,
            "start": self.start,
            "end": self.end,
            "base_currency": self.base_currency,
            "initial_cash": self.initial_cash,
            "data_snapshot_id": self.data_snapshot_id,
            "data_fingerprint": self.data_fingerprint,
            "data_source_id": self.data_source_id,
            "replay_file_fingerprint": self.replay_file_fingerprint,
            "replay_row_fingerprint": self.replay_row_fingerprint,
            "replay_data_fingerprint": self.replay_data_fingerprint,
            "fill_model": self.fill_model,
            "fill_model_fingerprint": self.fill_model_fingerprint,
            "fee_schedule": self.fee_schedule,
            "fee_fingerprint": self.fee_fingerprint,
            "risk_pipeline_fingerprint": self.risk_pipeline_fingerprint,
            "strategies": [strategy.to_json() for strategy in self.strategies],
        }


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """One deterministic backtest outcome with reproducibility artifacts."""

    configuration: BacktestConfig
    manifest: BacktestManifest
    status: BacktestStatus
    reason: str | None
    preflight: DataPreflight
    dispatch_hash: str
    dispatched_count: int
    market_event_count: int
    order_count: int
    fills: tuple[Fill, ...]
    fee_assessments: tuple[FeeAssessment, ...]
    fee_totals: FeeAggregation
    portfolio: PortfolioSnapshot
    risk_decisions: tuple[RiskDecision, ...]
    halted_strategies: tuple[str, ...]
    stopped_strategies: tuple[str, ...]
    teardown_errors: tuple[str, ...]
    lookahead_violations: tuple[str, ...]

    @property
    def completed(self) -> bool:
        """Return whether the run replayed its complete fixture."""
        return self.status is BacktestStatus.COMPLETED

    def artifacts(self) -> dict[str, object]:
        """Return the canonical, JSON-ready run artifacts."""
        return {
            "manifest": self.manifest.to_json(),
            "status": self.status.value,
            "reason": self.reason,
            "preflight": self.preflight.to_json(),
            "dispatch_hash": self.dispatch_hash,
            "counts": {
                "dispatched": self.dispatched_count,
                "market_events": self.market_event_count,
                "orders": self.order_count,
                "fills": len(self.fills),
                "risk_decisions": len(self.risk_decisions),
            },
            "strategies": {
                "halted": list(self.halted_strategies),
                "stopped": list(self.stopped_strategies),
                "teardown_errors": list(self.teardown_errors),
                "lookahead_violations": list(self.lookahead_violations),
            },
            "fills": [_fill_json(fill) for fill in self.fills],
            "fees": _fees_json(self.fee_assessments, self.fee_totals),
            "portfolio": _portfolio_json(self.portfolio),
            "risk": [_risk_json(decision) for decision in self.risk_decisions],
        }

    def artifact_json(self) -> str:
        """Return the stable byte-for-byte artifact document for this run."""
        return _canonical_json(self.artifacts())

    def artifact_digest(self) -> str:
        """Return the SHA-256 digest of the complete run artifact."""
        return hashlib.sha256(self.artifact_json().encode("utf-8")).hexdigest()

    def outcome_digest(self) -> str:
        """Return the SHA-256 digest of the run outcome without provenance.

        Provenance (the manifest and preflight record) is excluded, so two runs
        over identical market data share an outcome digest even when their
        data-source identity differs.  This isolates execution from where the
        rows came from.
        """
        artifacts = dict(self.artifacts())
        artifacts.pop("manifest", None)
        artifacts.pop("preflight", None)
        return hashlib.sha256(_canonical_json(artifacts).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported artifact value: {type(value).__name__}")


def _fill_json(fill: Fill) -> dict[str, object]:
    return {
        "event_id": fill.event_id,
        "timestamp": fill.timestamp.isoformat(timespec="microseconds"),
        "sequence": fill.sequence,
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "symbol": str(fill.symbol),
        "side": fill.side.value,
        "quantity": str(fill.quantity),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "liquidity": fill.liquidity,
    }


def _fees_json(
    assessments: Sequence[FeeAssessment], totals: FeeAggregation
) -> dict[str, object]:
    return {
        "assessments": [
            {
                "fill_id": assessment.fill_id,
                "schedule_id": assessment.schedule_id,
                "currency": assessment.currency,
                "run_id": assessment.run_id,
                "strategy_id": assessment.strategy_id,
                "asset_class": assessment.asset_class.value,
                "total": str(assessment.total),
                "components": [
                    {"fee_id": component.fee_id, "amount": str(component.amount)}
                    for component in assessment.components
                ],
            }
            for assessment in assessments
        ],
        "by_run": [_aggregate_json(aggregate) for aggregate in totals.by_run],
        "by_strategy": [_aggregate_json(aggregate) for aggregate in totals.by_strategy],
        "by_asset_class": [
            _aggregate_json(aggregate) for aggregate in totals.by_asset_class
        ],
    }


def _aggregate_json(aggregate: FeeAggregate) -> dict[str, object]:
    return {
        "key": aggregate.key,
        "currency": aggregate.currency,
        "amount": str(aggregate.amount),
    }


def _portfolio_json(snapshot: PortfolioSnapshot) -> dict[str, object]:
    return {
        "account_id": snapshot.account_id,
        "initial_cash": str(snapshot.initial_cash),
        "cash": str(snapshot.cash),
        "fill_count": snapshot.fill_count,
        "realized_pnl": str(snapshot.realized_pnl),
        "unrealized_pnl": _decimal_or_none(snapshot.unrealized_pnl),
        "equity": _decimal_or_none(snapshot.equity),
        "total_pnl": _decimal_or_none(snapshot.total_pnl),
        "positions": [
            {
                "symbol": str(position.symbol),
                "quantity": str(position.quantity),
                "long_quantity": str(position.long_quantity),
                "short_quantity": str(position.short_quantity),
                "cost_basis": str(position.cost_basis),
                "cash_flow": str(position.cash_flow),
                "average_cost": _decimal_or_none(position.average_cost),
                "realized_pnl": str(position.realized_pnl),
                "mark_price": _decimal_or_none(position.mark_price),
                "market_value": _decimal_or_none(position.market_value),
                "unrealized_pnl": _decimal_or_none(position.unrealized_pnl),
            }
            for position in snapshot.positions
        ],
        "books": [
            {
                "book_id": book.book_id,
                "initial_cash": str(book.initial_cash),
                "cash": str(book.cash),
                "equity": _decimal_or_none(book.equity),
                "realized_pnl": str(book.realized_pnl),
                "fill_count": book.fill_count,
                "positions": [
                    {
                        "symbol": str(position.symbol),
                        "quantity": str(position.quantity),
                        "cost_basis": str(position.cost_basis),
                        "cash_flow": str(position.cash_flow),
                        "average_cost": _decimal_or_none(position.average_cost),
                        "realized_pnl": str(position.realized_pnl),
                        "mark_price": _decimal_or_none(position.mark_price),
                        "market_value": _decimal_or_none(position.market_value),
                    }
                    for position in book.positions
                ],
            }
            for book in snapshot.books
        ],
    }


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _risk_json(decision: RiskDecision) -> dict[str, object]:
    return {
        "action": decision.action.value,
        "order_id": str(getattr(decision.original_order, "order_id", "")),
        "strategy_id": decision.strategy_id,
        "reason": decision.reason,
        "halt_scope": decision.halt_scope,
        "audits": [
            {
                "sequence": audit.sequence,
                "pass_number": audit.pass_number,
                "rule_id": audit.rule_id,
                "action": audit.action.value,
                "reason": audit.reason,
                "halt": audit.halt,
            }
            for audit in decision.audits
        ],
    }
