"""Map one completed harness run into a workspace :class:`BacktestRecord`.

The importer reads a run's catalog row (``get_run``) and its
``backtest.json``/``metrics.json`` artifacts (``read_artifact``) through the
harness MCP tool provider, reconstructs a canonical equity curve and FIFO
trade log, and projects the metrics into the snake_case/camelCase stat keys the
workspace detail and the UI backtest report both consume.

The equity curve prefers the harness ``artifacts/equity.parquet`` artifact and
falls back to a step curve derived from ``backtest.json`` fills and portfolio
cash; an unavailable parquet never fails the import.
"""

from __future__ import annotations

import io
import json
import math
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolResult
from thesis.backtests.artifacts import ArtifactReader, HarnessArtifactReader
from thesis.http.backtests import BacktestRecord
from thesis.integrations.mcp import TOOL_GET_JOB_STATUS, TOOL_GET_RUN

_PENDING_STATES = frozenset({"queued", "running", "pending", "submitted", "created"})
_FAILED_STATES = frozenset({"failed", "cancelled", "canceled", "error", "errored"})
_COMPLETE_STATES = frozenset({"complete", "completed", "succeeded", "success"})

_EQUITY_ARTIFACT = "artifacts/equity.parquet"
_BACKTEST_ARTIFACT = "backtest.json"
_METRICS_ARTIFACT = "metrics.json"
_CONFIG_ARTIFACT = "config.yaml"

_ANNUALIZATION = math.sqrt(252.0)


class BacktestRunError(RuntimeError):
    """A harness run cannot be imported (missing, failed, or unusable)."""


class BacktestRunPending(RuntimeError):
    """A harness run is still queued or running; the caller may retry."""


def import_backtest_run(
    *,
    tools: HarnessToolProvider,
    workspace_id: str,
    run_id: str,
    chat_id: str | None = None,
    execution_id: str | None = None,
    strategy_id: str | None = None,
    name: str | None = None,
    artifact_reader: ArtifactReader | None = None,
    http_get: Callable[[str], bytes] | None = None,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[str], str] | None = None,
) -> BacktestRecord:
    """Import one completed harness run as a workspace backtest record.

    Raises :class:`BacktestRunPending` when the run is still queued/running and
    :class:`BacktestRunError` when the run is missing, failed, or has no usable
    status.  A missing or unreadable equity parquet is tolerated.
    """
    reader = artifact_reader or HarnessArtifactReader(tools, http_get=http_get)
    _require_terminal(tools, run_id)
    catalog = _get_run(tools, run_id)
    status = _status_of(catalog)
    if status is None:
        raise BacktestRunError(f"harness run {run_id!r} reported no status")
    if status in _PENDING_STATES:
        raise BacktestRunPending(f"harness run {run_id!r} is still {status}")
    if status not in _COMPLETE_STATES:
        raise BacktestRunError(f"harness run {run_id!r} ended in state {status!r}")

    backtest = _read_json(reader, run_id, _BACKTEST_ARTIFACT)
    metrics = _read_json(reader, run_id, _METRICS_ARTIFACT)
    configuration = _read_text(reader, run_id, _CONFIG_ARTIFACT)
    manifest_document = _mapping(backtest.get("manifest"))
    portfolio = _mapping(backtest.get("portfolio"))
    fills = _sequence(backtest.get("fills"))

    initial_capital = _first_number(
        portfolio.get("initial_cash"),
        manifest_document.get("initial_cash"),
    )
    equity_points = _equity_from_parquet(reader, run_id)
    if not equity_points:
        equity_points = _equity_from_fills(fills, manifest_document, initial_capital)
    equity_values = [float(point["v"]) for point in equity_points]

    trades = _build_trades(fills)
    final_capital = _final_capital(equity_values, initial_capital, portfolio, metrics)
    total_pnl = _number(portfolio.get("total_pnl"))
    if total_pnl is None:
        total_pnl = final_capital - initial_capital
    stats = _stats(metrics, equity_values, trades, initial_capital, total_pnl)

    resolved_strategy_id = (
        strategy_id
        or _text(catalog.get("strategy_id"))
        or _text(manifest_document.get("strategy_id"))
        or f"strategy-{run_id}"
    )
    resolved_name = name or f"Backtest {run_id}"
    period_start = _first_text(
        catalog.get("data_start"), manifest_document.get("start")
    )
    period_end = _first_text(catalog.get("data_end"), manifest_document.get("end"))
    created_at = _iso_z((clock or _utc_now)())
    resolved_status = _text(backtest.get("status")) or (
        "completed" if status in _COMPLETE_STATES else status
    )

    manifest_entry: dict[str, object] = {
        "run_id": run_id,
        "status": resolved_status,
        "run_digest": _text(catalog.get("run_digest")),
        "configuration_digest": _text(catalog.get("configuration_digest")),
        "data_start": _text(catalog.get("data_start")),
        "data_end": _text(catalog.get("data_end")),
        "artifact_count": _integer(catalog.get("artifact_count"), 0),
        "total_bytes": _integer(catalog.get("total_bytes"), 0),
        "metrics": dict(metrics),
        "stats": dict(stats),
        "harness_manifest": dict(manifest_document),
        "configuration": configuration,
    }

    return BacktestRecord(
        id=(id_factory or _new_record_id)("bt"),
        workspace_id=workspace_id,
        strategy_id=resolved_strategy_id,
        name=resolved_name,
        period_start=period_start,
        period_end=period_end,
        created_at=created_at,
        initial_capital=initial_capital,
        final_capital=final_capital,
        total_return=stats["total_return"],
        sharpe=stats["sharpe"],
        max_drawdown=stats["max_drawdown"],
        win_rate=stats["win_rate"],
        trades=int(stats["trade_count"]),
        manifest=manifest_entry,
        equity=tuple(equity_points),
        trade_log=tuple(trades),
        stats=stats,
        status=resolved_status,
        run_id=run_id,
        chat_id=chat_id,
        execution_id=execution_id,
        config_path=_CONFIG_ARTIFACT if configuration is not None else None,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_record_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _get_run(tools: HarnessToolProvider, run_id: str) -> dict[str, object]:
    result = tools.call_tool(TOOL_GET_RUN, {"run_id": run_id})
    if not result.ok:
        raise BacktestRunError(_failure_message(result, run_id))
    return dict(result.structured)


def _failure_message(result: HarnessToolResult, run_id: str) -> str:
    if result.failure is not None and result.failure.message:
        return result.failure.message
    return f"harness run {run_id!r} could not be read"


def _status_of(catalog: Mapping[str, object]) -> str | None:
    status = _text(catalog.get("status")) or _text(catalog.get("state"))
    return status.lower() if status is not None else None


def _require_terminal(tools: HarnessToolProvider, run_id: str) -> None:
    """Fail fast on a pending/failed job before the run catalog row exists.

    The async submitter writes the run catalog row only when the worker
    finishes, so a still-running run is distinguishable from a missing one only
    through ``get_job_status``.
    """
    result = tools.call_tool(TOOL_GET_JOB_STATUS, {"job_id": run_id})
    if not result.ok:
        return
    state = _status_of(result.structured)
    if state in _PENDING_STATES:
        raise BacktestRunPending(f"harness run {run_id!r} is still {state}")
    if state in _FAILED_STATES:
        raise BacktestRunError(f"harness run {run_id!r} ended in state {state!r}")


def _read_json(reader: ArtifactReader, run_id: str, name: str) -> dict[str, object]:
    data = reader.read(run_id, name)
    if data is None:
        return {}
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return dict(document) if isinstance(document, Mapping) else {}


def _read_text(reader: ArtifactReader, run_id: str, name: str) -> str | None:
    data = reader.read(run_id, name)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _equity_from_parquet(
    reader: ArtifactReader, run_id: str
) -> tuple[dict[str, float | int], ...]:
    data = reader.read(run_id, _EQUITY_ARTIFACT)
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


def _equity_from_fills(
    fills: Sequence[object],
    manifest: Mapping[str, object],
    initial_capital: float,
) -> tuple[dict[str, float | int], ...]:
    cash = initial_capital
    points: list[dict[str, float | int]] = []
    start = _epoch_ms(manifest.get("start"))
    if start is not None:
        points.append({"t": start, "v": cash})
    for fill in sorted(fills, key=_fill_order):
        document = _mapping(fill)
        millis = _epoch_ms(document.get("timestamp"))
        quantity = _number(document.get("quantity")) or 0.0
        price = _number(document.get("price")) or 0.0
        fee = _number(document.get("fee")) or 0.0
        side = (_text(document.get("side")) or "").lower()
        if side == "buy":
            cash -= quantity * price + fee
        elif side == "sell":
            cash += quantity * price - fee
        if millis is not None:
            points.append({"t": millis, "v": cash})
    return tuple(points)


def _fill_order(fill: object) -> tuple[int, int]:
    document = _mapping(fill)
    sequence = _integer(document.get("sequence"), 0)
    millis = _epoch_ms(document.get("timestamp")) or 0
    return (sequence, millis)


def _build_trades(fills: Sequence[object]) -> list[dict[str, object]]:
    lots: dict[str, deque[list[float]]] = {}
    trades: list[dict[str, object]] = []
    for fill in sorted(fills, key=_fill_order):
        document = _mapping(fill)
        symbol = _text(document.get("symbol")) or "?"
        side = (_text(document.get("side")) or "").lower()
        quantity = _number(document.get("quantity")) or 0.0
        price = _number(document.get("price")) or 0.0
        millis = _epoch_ms(document.get("timestamp")) or 0
        signed = quantity if side == "buy" else -quantity
        if signed == 0:
            continue
        queue = lots.setdefault(symbol, deque())
        remaining = signed
        while remaining != 0 and queue and (queue[0][0] > 0) != (remaining > 0):
            lot = queue[0]
            lot_quantity = lot[0]
            matched = min(abs(lot_quantity), abs(remaining))
            entry_price = lot[1]
            entry_time = int(lot[2])
            is_long = lot_quantity > 0
            pnl = (
                (price - entry_price) * matched
                if is_long
                else (entry_price - price) * matched
            )
            trades.append(
                {
                    "id": f"trade-{len(trades) + 1:04d}",
                    "symbol": symbol,
                    "side": "buy" if is_long else "sell",
                    "qty": matched,
                    "entryTime": entry_time,
                    "entryPrice": entry_price,
                    "exitTime": millis,
                    "exitPrice": price,
                    "pnl": pnl,
                }
            )
            if abs(lot_quantity) == matched:
                queue.popleft()
            elif is_long:
                lot[0] = lot_quantity - matched
            else:
                lot[0] = lot_quantity + matched
            remaining += matched if is_long else -matched
        if remaining != 0:
            queue.append([remaining, price, float(millis)])
    return trades


def _stats(
    metrics: Mapping[str, object],
    equity_values: Sequence[float],
    trades: Sequence[Mapping[str, object]],
    initial_capital: float,
    total_pnl: float,
) -> dict[str, float]:
    equity_metrics = _mapping(metrics.get("equity"))
    total_return = _number(equity_metrics.get("total_return"))
    if total_return is None:
        total_return = _return_from_equity(equity_values, initial_capital)
    max_drawdown = _number(equity_metrics.get("max_drawdown"))
    if max_drawdown is None:
        max_drawdown = _max_drawdown(equity_values)
    cagr = _number(equity_metrics.get("cagr"))
    if cagr is None:
        cagr = total_return
    sharpe = _number(equity_metrics.get("sharpe"))
    if sharpe is None:
        sharpe = _sharpe(equity_values)
    trade_count = len(trades)
    wins = sum(1 for trade in trades if (_number(trade.get("pnl")) or 0.0) > 0)
    win_rate = wins / trade_count if trade_count else 0.0
    gross_profit = sum(
        pnl for trade in trades if (pnl := _number(trade.get("pnl")) or 0.0) > 0
    )
    gross_loss = sum(
        pnl for trade in trades if (pnl := _number(trade.get("pnl")) or 0.0) < 0
    )
    profit_factor = (
        gross_profit / abs(gross_loss)
        if gross_loss < 0
        else (gross_profit if gross_profit > 0 else 0.0)
    )
    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "cagr": cagr,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trade_count": float(trade_count),
        "total_pnl": total_pnl,
        "totalReturn": total_return,
        "maxDrawdown": max_drawdown,
        "hitRate": win_rate,
        "profitFactor": profit_factor,
        "tradeCount": float(trade_count),
        "totalPnl": total_pnl,
    }


def _return_from_equity(values: Sequence[float], initial_capital: float) -> float:
    if not values or initial_capital <= 0:
        return 0.0
    return values[-1] / initial_capital - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    peak: float | None = None
    worst = 0.0
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            drawdown = value / peak - 1.0
            if drawdown < worst:
                worst = drawdown
    return worst


def _sharpe(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    returns: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous:
            returns.append(current / previous - 1.0)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    if deviation == 0:
        return 0.0
    return mean / deviation * _ANNUALIZATION


def _final_capital(
    values: Sequence[float],
    initial_capital: float,
    portfolio: Mapping[str, object],
    metrics: Mapping[str, object],
) -> float:
    if values:
        return values[-1]
    equity = _number(_mapping(portfolio.get("equity")))
    if equity is not None:
        return equity
    equity_metrics = _mapping(metrics.get("equity"))
    total_return = _number(equity_metrics.get("total_return"))
    if total_return is not None:
        return initial_capital * (1.0 + total_return)
    total_pnl = _number(portfolio.get("total_pnl")) or 0.0
    return initial_capital + total_pnl


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


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


def _first_number(*values: object) -> float:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return 0.0


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _integer(value: object, default: int) -> int:
    number = _number(value)
    return default if number is None else int(number)


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
