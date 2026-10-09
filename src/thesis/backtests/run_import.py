"""Map one completed harness job into a workspace :class:`BacktestRecord`.

The importer reads a job's result (``get_job_result``) and its
``backtest.json``/``metrics.json`` artifacts (``read_artifact``) through the
harness MCP tool provider and projects them into the record the workspace
detail and the UI backtest report consume.  The record is keyed by the same
``job_id`` the submission allocated.

Nothing is recomputed here.  Every statistic, the trade log, and the equity
curve are read as the harness published them: the canonical ``metrics.json``
``equity`` and ``trades`` blocks and the ``artifacts/equity.parquet`` series.
A value the harness did not publish is ``None`` with an explicit reason, never
a zero and never a figure derived from the fills.  An artifact without the
canonical ``trades`` rows yields an empty trade log and
``trades_unavailable_reason``; its trades are not reconstructed.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.artifacts import ArtifactReader, HarnessArtifactReader
from thesis.http.backtests import BacktestRecord
from thesis.integrations.mcp import TOOL_GET_JOB_RESULT, TOOL_GET_JOB_STATUS
from thesis.research.evidence import numeric_metric

_PENDING_STATES = frozenset({"queued", "running"})
_FAILED_STATES = frozenset({"failed", "cancelled"})
_COMPLETE_STATES = frozenset({"succeeded"})

_EQUITY_ARTIFACT = "artifacts/equity.parquet"
_BACKTEST_ARTIFACT = "backtest.json"
_METRICS_ARTIFACT = "metrics.json"
_CONFIG_ARTIFACT = "config.yaml"

_CANONICAL_TRADE_FIELDS = (
    "trade_count",
    "win_count",
    "loss_count",
    "hit_rate",
    "gross_pnl",
    "fees",
    "open_fees",
    "net_pnl",
    "gross_profit",
    "gross_loss",
    "net_profit",
    "net_loss",
    "profit_factor",
    "average_net_win",
    "average_net_loss",
    "largest_net_win",
    "largest_net_loss",
)

_CANONICAL_TRADE_REASON_FIELDS = (
    "profit_factor_reason",
    "hit_rate_reason",
)

_TRADE_DECIMAL_FIELDS = (
    "quantity",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "entry_fee",
    "exit_fee",
    "fees",
    "net_pnl",
)

_NANOSECONDS_PER_MILLISECOND = 1_000_000


class BacktestRunError(RuntimeError):
    """A harness job cannot be imported (missing, failed, or unusable).

    ``state`` carries the harness-observed terminal state when one was read, so
    a report consumer can distinguish an observed harness failure from a job
    the harness could not resolve at all.
    """

    def __init__(self, message: str, *, state: str | None = None) -> None:
        super().__init__(message)
        self.state = state


class BacktestRunPending(RuntimeError):
    """A harness job is still queued or running; the caller may retry."""


@dataclass(frozen=True, slots=True)
class _EquityStatistics:
    """The equity-block statistics exactly as the harness published them."""

    total_return: float | None
    net_pnl: float | None
    net_pnl_reason: str | None
    gross_pnl: float | None
    gross_pnl_reason: str | None
    total_return_reason: str | None
    max_drawdown: float | None
    max_drawdown_reason: str | None
    cagr: float | None
    cagr_reason: str | None
    sharpe_per_sample: float | None
    sharpe_per_sample_reason: str | None


def import_backtest_run(
    *,
    tools: HarnessToolProvider,
    workspace_id: str,
    job_id: str,
    chat_id: str | None = None,
    execution_id: str | None = None,
    strategy_id: str | None = None,
    name: str | None = None,
    artifact_reader: ArtifactReader | None = None,
    http_get: Callable[[str], bytes] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BacktestRecord:
    """Import one completed harness job as a workspace backtest record.

    Raises :class:`BacktestRunPending` when the job is still queued/running and
    :class:`BacktestRunError` when the job is missing, failed, has no usable
    status, or carries a malformed canonical trade row.  A missing or unreadable
    equity parquet yields no equity series and an ``equity_unavailable_reason``.
    """
    reader = artifact_reader or HarnessArtifactReader(tools, http_get=http_get)
    _require_terminal(tools, job_id)
    catalog = _get_job_result(tools, job_id)
    status = _status_of(catalog)
    if status is None:
        raise BacktestRunError(f"harness job {job_id!r} reported no state")
    if status in _PENDING_STATES:
        raise BacktestRunPending(f"harness job {job_id!r} is still {status}")
    if status not in _COMPLETE_STATES:
        raise BacktestRunError(
            f"harness job {job_id!r} ended in state {status!r}", state=status
        )

    backtest = _read_json(reader, job_id, _BACKTEST_ARTIFACT)
    metrics = _read_json(reader, job_id, _METRICS_ARTIFACT)
    configuration = _read_text(reader, job_id, _CONFIG_ARTIFACT)
    manifest_document = _mapping(backtest.get("manifest"))
    portfolio = _mapping(backtest.get("portfolio"))

    equity_points = _equity_from_parquet(reader, job_id)
    equity = _equity_statistics(metrics)
    canonical = _canonical_trade_stats(metrics)
    trade_reasons = _canonical_trade_reasons(metrics)
    trade_log, trades_reason = _trade_log(metrics)
    stats = _stats_document(
        equity,
        canonical,
        trade_reasons,
        equity_reason=None if equity_points else "artifact_unavailable",
        trades_reason=trades_reason,
        fees_total=_number(_mapping(metrics.get("execution")).get("fees_total")),
    )
    initial_capital = _first_number(
        portfolio.get("initial_cash"),
        manifest_document.get("initial_cash"),
    )
    final_capital = _final_capital(equity_points, portfolio)

    resolved_strategy_id = (
        strategy_id
        or _text(catalog.get("strategy_id"))
        or _text(manifest_document.get("strategy_id"))
        or f"strategy-{job_id}"
    )
    resolved_name = name or f"Backtest {job_id}"
    period_start = _first_text(
        catalog.get("data_start"), manifest_document.get("start")
    )
    period_end = _first_text(catalog.get("data_end"), manifest_document.get("end"))
    created_at = _iso_z((clock or _utc_now)())

    manifest_entry: dict[str, object] = {
        "job_id": job_id,
        "status": status,
        "run_digest": _text(catalog.get("run_digest")),
        "configuration_digest": _text(catalog.get("configuration_digest")),
        "data_start": _text(catalog.get("data_start")),
        "data_end": _text(catalog.get("data_end")),
        "artifact_count": _optional_int(catalog.get("artifact_count")),
        "total_bytes": _optional_int(catalog.get("total_bytes")),
        "metrics": dict(metrics),
        "stats": dict(stats),
        "harness_manifest": dict(manifest_document),
        "configuration": configuration,
    }

    return BacktestRecord(
        job_id=job_id,
        workspace_id=workspace_id,
        strategy_id=resolved_strategy_id,
        name=resolved_name,
        period_start=period_start,
        period_end=period_end,
        created_at=created_at,
        initial_capital=initial_capital,
        final_capital=final_capital,
        total_return=equity.total_return,
        sharpe_per_sample=equity.sharpe_per_sample,
        max_drawdown=equity.max_drawdown,
        win_rate=canonical["hit_rate"],
        trades=_optional_int(canonical["trade_count"]),
        manifest=manifest_entry,
        equity=tuple(equity_points),
        trade_log=trade_log,
        stats=stats,
        status=status,
        chat_id=chat_id,
        execution_id=execution_id,
        config_path=_CONFIG_ARTIFACT if configuration is not None else None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _get_job_result(tools: HarnessToolProvider, job_id: str) -> dict[str, object]:
    result = tools.call_tool(TOOL_GET_JOB_RESULT, {"job_id": job_id})
    if not result.ok:
        raise BacktestRunError(_failure_message(result, job_id))
    return dict(result.structured)


def _failure_message(result: HarnessToolResult, job_id: str) -> str:
    if result.failure is not None and result.failure.message:
        return result.failure.message
    return f"harness job {job_id!r} could not be read"


def _status_of(catalog: Mapping[str, object]) -> str | None:
    status = _text(catalog.get("state"))
    return status.lower() if status is not None else None


def _require_terminal(tools: HarnessToolProvider, job_id: str) -> None:
    """Fail fast on a pending/failed job before the result record exists.

    The async submitter writes the result record only when the worker
    finishes, so a still-running job is distinguishable from a missing one only
    through ``get_job_status``.
    """
    result = tools.call_tool(TOOL_GET_JOB_STATUS, {"job_id": job_id})
    if not result.ok:
        return
    state = _status_of(result.structured)
    if state in _PENDING_STATES:
        raise BacktestRunPending(f"harness job {job_id!r} is still {state}")
    if state in _FAILED_STATES:
        raise BacktestRunError(
            f"harness job {job_id!r} ended in state {state!r}", state=state
        )


def _read_json(reader: ArtifactReader, job_id: str, name: str) -> dict[str, object]:
    data = reader.read(job_id, name)
    if data is None:
        return {}
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return dict(document) if isinstance(document, Mapping) else {}


def _read_text(reader: ArtifactReader, job_id: str, name: str) -> str | None:
    data = reader.read(job_id, name)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _equity_from_parquet(
    reader: ArtifactReader, job_id: str
) -> tuple[dict[str, float | int], ...]:
    data = reader.read(job_id, _EQUITY_ARTIFACT)
    if data is None:
        return ()
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(io.BytesIO(data))
    except Exception:  # an unreadable parquet must never fail the import
        return ()
    columns = table.to_pydict()
    timestamps = columns.get("timestamp")
    equity = columns.get("equity")
    if not isinstance(timestamps, list) or not isinstance(equity, list):
        return ()
    points: list[dict[str, float | int]] = []
    for timestamp, value in zip(timestamps, equity, strict=False):
        millis = _epoch_ms(timestamp)
        number = _number(value)
        if millis is None or number is None:
            continue
        points.append({"t": millis, "v": number})
    return tuple(points)


def _equity_statistics(metrics: Mapping[str, object]) -> _EquityStatistics:
    equity = _mapping(metrics.get("equity"))
    return _EquityStatistics(
        total_return=_number(equity.get("total_return")),
        net_pnl=_number(equity.get("net_pnl")),
        net_pnl_reason=_text(equity.get("net_pnl_reason")),
        gross_pnl=_number(equity.get("gross_pnl")),
        gross_pnl_reason=_text(equity.get("gross_pnl_reason")),
        total_return_reason=_text(equity.get("total_return_reason")),
        max_drawdown=_number(equity.get("max_drawdown")),
        max_drawdown_reason=_text(equity.get("max_drawdown_reason")),
        cagr=_number(equity.get("cagr")),
        cagr_reason=_text(equity.get("cagr_reason")),
        sharpe_per_sample=_number(equity.get("sharpe_per_sample")),
        sharpe_per_sample_reason=_text(equity.get("sharpe_per_sample_reason")),
    )


def _canonical_trade_stats(metrics: Mapping[str, object]) -> dict[str, float | None]:
    """Read the canonical ``trades`` block without recomputing any field.

    A missing block, a missing field, or a JSON ``null`` becomes ``None``.
    Profit factor is taken exactly as published (undefined when no trade lost);
    gross profit is never substituted for it.
    """
    block = _mapping(metrics.get("trades"))
    return {field: _number(block.get(field)) for field in _CANONICAL_TRADE_FIELDS}


def _canonical_trade_reasons(metrics: Mapping[str, object]) -> dict[str, str | None]:
    """Read the published reasons that explain an undefined trade ratio as text."""
    block = _mapping(metrics.get("trades"))
    return {field: _text(block.get(field)) for field in _CANONICAL_TRADE_REASON_FIELDS}


def _trade_log(
    metrics: Mapping[str, object],
) -> tuple[tuple[dict[str, object], ...], str | None]:
    """Project the canonical trade rows; nothing is rebuilt from fills.

    Returns the trade log and, when it is empty for a reason other than having
    no trades, the reason it is unavailable.
    """
    if not metrics:
        return (), "metrics_unavailable"
    rows = _mapping(metrics.get("trades")).get("rows")
    if not isinstance(rows, list):
        return (), "legacy_artifact"
    return tuple(_trade_record(row) for row in rows), None


def _trade_id(value: object) -> int | None:
    """The canonical trade id is a non-negative JSON integer (``uint``).

    A boolean is an integer in Python but never a trade id; a string or any
    other type is malformed.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _trade_record(row: object) -> dict[str, object]:
    """Map one canonical closed-trade row onto the workspace trade shape.

    The workspace id is ``trade-NNNN``, formatted from the integer ``trade_id``.
    ``pnl`` is the net PnL the statistics headline; the gross PnL and the fees
    are carried alongside it.  A row that breaks the canonical contract fails
    the import rather than being dropped or defaulted.
    """
    document = _mapping(row)
    decimals = {name: _number(document.get(name)) for name in _TRADE_DECIMAL_FIELDS}
    entry_ns = _epoch_ns(document.get("entry_timestamp_ns"))
    exit_ns = _epoch_ns(document.get("exit_timestamp_ns"))
    trade_id = _trade_id(document.get("trade_id"))
    symbol = _text(document.get("symbol"))
    side = _text(document.get("side"))
    if (
        trade_id is None
        or symbol is None
        or side not in ("long", "short")
        or entry_ns is None
        or exit_ns is None
        or any(value is None for value in decimals.values())
    ):
        raise BacktestRunError(
            f"harness trade row {document.get('trade_id')!r} breaks the canonical contract"
        )
    return {
        "id": f"trade-{trade_id:04d}",
        "symbol": symbol,
        "side": "buy" if side == "long" else "sell",
        "qty": decimals["quantity"],
        "entryTime": entry_ns // _NANOSECONDS_PER_MILLISECOND,
        "entryPrice": decimals["entry_price"],
        "exitTime": exit_ns // _NANOSECONDS_PER_MILLISECOND,
        "exitPrice": decimals["exit_price"],
        "grossPnl": decimals["gross_pnl"],
        "fees": decimals["fees"],
        "entryFee": decimals["entry_fee"],
        "exitFee": decimals["exit_fee"],
        "pnl": decimals["net_pnl"],
    }


def _stats_document(
    equity: _EquityStatistics,
    canonical: Mapping[str, float | None],
    reasons: Mapping[str, str | None],
    *,
    equity_reason: str | None,
    trades_reason: str | None,
    fees_total: float | None,
) -> dict[str, object]:
    """Project canonical snake_case statistics with explicit equity/trade PnL."""
    return {
        **canonical,
        **reasons,
        "total_return": equity.total_return,
        "total_return_reason": equity.total_return_reason,
        "max_drawdown": equity.max_drawdown,
        "max_drawdown_reason": equity.max_drawdown_reason,
        "cagr": equity.cagr,
        "cagr_reason": equity.cagr_reason,
        "sharpe_per_sample": equity.sharpe_per_sample,
        "sharpe_per_sample_reason": equity.sharpe_per_sample_reason,
        "equity_unavailable_reason": equity_reason,
        "trades_unavailable_reason": trades_reason,
        "net_pnl": equity.net_pnl,
        "net_pnl_reason": equity.net_pnl_reason,
        "gross_pnl": equity.gross_pnl,
        "gross_pnl_reason": equity.gross_pnl_reason,
        "fees_total": fees_total,
        "closed_trade_net_pnl": canonical["net_pnl"],
        "closed_trade_gross_pnl": canonical["gross_pnl"],
    }


def _final_capital(
    equity_points: tuple[dict[str, float | int], ...],
    portfolio: Mapping[str, object],
) -> float | None:
    """The harness's final equity: its last series point, else its portfolio."""
    if equity_points:
        return float(equity_points[-1]["v"])
    return _number(portfolio.get("equity"))


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_text(*values: object) -> str:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return ""


def _first_number(*values: object) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: object) -> float | None:
    """Parse finite canonical numeric values without interpreting reasons."""
    return numeric_metric(value)


def _optional_int(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _epoch_ns(value: object) -> int | None:
    """Epoch nanoseconds kept exact: JSON integers are never routed through float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _epoch_ms(value: object) -> int | None:
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(moment.timestamp() * 1000)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e17:
            return int(number / 1e6)
        if number > 1e14:
            return int(number / 1e3)
        if number > 1e11:
            return int(number)
        return int(number * 1000)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        return _epoch_ms(moment)
    return None


__all__ = [
    "BacktestRunError",
    "BacktestRunPending",
    "import_backtest_run",
]
