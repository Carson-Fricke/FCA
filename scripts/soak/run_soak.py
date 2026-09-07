"""E6.3 full-history AAPL and SPY soak driver.

Runs the market-lake ensure path over the full available XNYS session window
per soak symbol, one session per request, recording per-session rows, bytes,
estimated provider pages, and duration into an append-only JSONL audit file.
A resumable state file tracks completed sessions so an interrupted soak
continues without refetching. The summary projects Azure costs from recorded
bytes and request counts against the published prices in the soak config
(retrieved from the Azure Retail Prices API; see the price assumptions
documented in ``config/lake/soak-aapl-spy.yaml``).

Live mode targets the deployed market-lake API through :mod:`lakeclient`
using the bearer token in ``LAKE_API__BEARER_TOKEN``. Dry-run mode performs
the identical calendar-aware planning math with no network access.

Launch:

    uv run --extra dev python scripts/soak/run_soak.py \
        --config config/lake/soak-aapl-spy.yaml --dry-run

    uv run --extra dev python scripts/soak/run_soak.py \
        --config config/lake/soak-aapl-spy.yaml --base-url https://<api-fqdn>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from lake.configuration.loader import load_yaml_mapping
from lake.coverage import equity_sessions
from lakeclient import (
    CoverageResponse,
    DatasetRequest,
    LakeClient,
    LakeClientError,
)
from lakeclient.models import AssetClass, CoverageEntry, DataType

BEARER_ENV = "LAKE_API__BEARER_TOKEN"
TERMINAL_OUTCOMES = frozenset({"complete", "empty"})


class SoakConfigError(ValueError):
    """The soak configuration file is missing or has unusable values."""


@dataclass(frozen=True, slots=True)
class RunPaths:
    state_file: Path
    results_file: Path
    summary_file: Path


@dataclass(frozen=True, slots=True)
class RunSettings:
    max_requests: int
    job_wait_deadline_seconds: float
    poll_interval_seconds: float
    coverage_recheck_attempts: int
    inter_request_pause_seconds: float


@dataclass(frozen=True, slots=True)
class EstimateSettings:
    assumed_bytes: dict[str, dict[str, int]]
    api_vcores: float
    api_memory_gib: float
    log_analytics_gb_per_1000_requests: float
    storage_retention_days: int


@dataclass(frozen=True, slots=True)
class BlobPrices:
    stored_usd_per_gb_month: float
    write_usd_per_10k: float
    read_usd_per_10k: float
    other_usd_per_10k: float


@dataclass(frozen=True, slots=True)
class TablesPrices:
    stored_usd_per_gb_month: float
    transaction_usd_per_10k: float


@dataclass(frozen=True, slots=True)
class QueuePrices:
    stored_usd_per_gb_month: float
    class1_usd_per_10k: float


@dataclass(frozen=True, slots=True)
class ContainerAppsPrices:
    vcpu_usd_per_second: float
    memory_usd_per_gib_second: float
    requests_usd_per_million: float
    environment_usd_per_hour: float


@dataclass(frozen=True, slots=True)
class LogAnalyticsPrices:
    ingestion_usd_per_gb: float
    retention_usd_per_gb_month: float
    included_free_gb_per_month: float


@dataclass(frozen=True, slots=True)
class CostSettings:
    currency: str
    region: str
    blob: BlobPrices
    tables: TablesPrices
    queue: QueuePrices
    container_apps: ContainerAppsPrices
    log_analytics: LogAnalyticsPrices


@dataclass(frozen=True, slots=True)
class SoakConfig:
    soak_id: str
    symbols: tuple[str, ...]
    data_types: tuple[str, ...]
    default_start: date
    paths: RunPaths
    run: RunSettings
    items_per_page: int
    estimate: EstimateSettings
    cost: CostSettings


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    status: str
    rows: int | None
    byte_size: int | None
    object_path: str | None
    error: str | None
    duration_ms: int

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_OUTCOMES


@dataclass(slots=True)
class Aggregate:
    sessions_complete: int = 0
    sessions_empty: int = 0
    sessions_failed: int = 0
    rows: int = 0
    byte_total: int = 0
    pages: int = 0
    duration_ms: int = 0

    def add(self, status: str, rows: int | None, byte_size: int | None) -> None:
        if status == "complete":
            self.sessions_complete += 1
        elif status == "empty":
            self.sessions_empty += 1
        else:
            self.sessions_failed += 1
        if rows is not None:
            self.rows += rows
        if byte_size is not None:
            self.byte_total += byte_size


@dataclass(frozen=True, slots=True)
class SoakState:
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Configuration loading


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SoakConfigError(f"{where} must be a mapping")
    return value


def _section(parent: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    if key not in parent:
        raise SoakConfigError(f"soak configuration is missing {where}")
    return _mapping(parent[key], where)


def _number(parent: dict[str, Any], key: str, where: str) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SoakConfigError(f"{where}.{key} must be a number")
    return float(value)


def _integer(parent: dict[str, Any], key: str, where: str) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SoakConfigError(f"{where}.{key} must be an integer")
    return value


def _list_values(parent: dict[str, Any], key: str) -> list[object]:
    value = parent.get(key)
    if not isinstance(value, list) or not value:
        raise SoakConfigError(f"soak.{key} must be a non-empty list")
    return value


def _list_item(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SoakConfigError(f"{where} entries must be non-empty strings")
    return value


def _text(parent: dict[str, Any], key: str, where: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SoakConfigError(f"{where}.{key} must be a non-empty string")
    return value


def _load_blob_prices(raw: dict[str, Any]) -> BlobPrices:
    where = "soak.cost.blob_hns_lrs"
    return BlobPrices(
        stored_usd_per_gb_month=_number(raw, "stored_usd_per_gb_month", where),
        write_usd_per_10k=_number(raw, "write_usd_per_10k", where),
        read_usd_per_10k=_number(raw, "read_usd_per_10k", where),
        other_usd_per_10k=_number(raw, "other_usd_per_10k", where),
    )


def _load_tables_prices(raw: dict[str, Any]) -> TablesPrices:
    where = "soak.cost.tables_lrs"
    return TablesPrices(
        stored_usd_per_gb_month=_number(raw, "stored_usd_per_gb_month", where),
        transaction_usd_per_10k=_number(raw, "transaction_usd_per_10k", where),
    )


def _load_queue_prices(raw: dict[str, Any]) -> QueuePrices:
    where = "soak.cost.queue_v2_lrs"
    return QueuePrices(
        stored_usd_per_gb_month=_number(raw, "stored_usd_per_gb_month", where),
        class1_usd_per_10k=_number(raw, "class1_usd_per_10k", where),
    )


def _load_container_apps_prices(raw: dict[str, Any]) -> ContainerAppsPrices:
    where = "soak.cost.container_apps_consumption"
    return ContainerAppsPrices(
        vcpu_usd_per_second=_number(raw, "vcpu_usd_per_second", where),
        memory_usd_per_gib_second=_number(raw, "memory_usd_per_gib_second", where),
        requests_usd_per_million=_number(raw, "requests_usd_per_million", where),
        environment_usd_per_hour=_number(raw, "environment_usd_per_hour", where),
    )


def _load_log_analytics_prices(raw: dict[str, Any]) -> LogAnalyticsPrices:
    where = "soak.cost.log_analytics"
    return LogAnalyticsPrices(
        ingestion_usd_per_gb=_number(raw, "ingestion_usd_per_gb", where),
        retention_usd_per_gb_month=_number(raw, "retention_usd_per_gb_month", where),
        included_free_gb_per_month=_number(raw, "included_free_gb_per_month", where),
    )


def _load_estimate(raw: dict[str, Any]) -> EstimateSettings:
    where = "soak.estimate"
    assumed = _section(raw, "assumed_bytes_per_session", where)
    assumed_bytes: dict[str, dict[str, int]] = {}
    for symbol, per_type in assumed.items():
        typed = _mapping(per_type, f"{where}.assumed_bytes_per_session.{symbol}")
        assumed_bytes[symbol] = {
            data_type: _integer(typed, data_type, f"{where}.{symbol}")
            for data_type in typed
        }
    return EstimateSettings(
        assumed_bytes=assumed_bytes,
        api_vcores=_number(raw, "api_vcores", where),
        api_memory_gib=_number(raw, "api_memory_gib", where),
        log_analytics_gb_per_1000_requests=_number(
            raw, "log_analytics_gb_per_1000_requests", where
        ),
        storage_retention_days=_integer(raw, "storage_retention_days", where),
    )


def load_soak_config(path: Path) -> SoakConfig:
    """Parse and validate the soak YAML via the lake configuration loader."""
    try:
        document = load_yaml_mapping(path)
    except (OSError, ValueError) as error:
        raise SoakConfigError(f"soak configuration failed to load: {error}") from error
    root = _section(document, "soak", "document")
    run_section = _section(root, "run", "soak")
    paths = RunPaths(
        state_file=Path(_text(run_section, "state_file", "soak.run")),
        results_file=Path(_text(run_section, "results_file", "soak.run")),
        summary_file=Path(_text(run_section, "summary_file", "soak.run")),
    )
    run = RunSettings(
        max_requests=_integer(run_section, "max_requests_per_invocation", "soak.run"),
        job_wait_deadline_seconds=_number(
            run_section, "job_wait_deadline_seconds", "soak.run"
        ),
        poll_interval_seconds=_number(run_section, "poll_interval_seconds", "soak.run"),
        coverage_recheck_attempts=_integer(
            run_section, "coverage_recheck_attempts", "soak.run"
        ),
        inter_request_pause_seconds=_number(
            run_section, "inter_request_pause_seconds", "soak.run"
        ),
    )
    pages = _section(root, "pages", "soak")
    cost = _section(root, "cost", "soak")
    symbols = tuple(
        _list_item(value, "soak.symbols") for value in _list_values(root, "symbols")
    )
    data_types = tuple(
        _list_item(value, "soak.data_types")
        for value in _list_values(root, "data_types")
    )
    if not symbols or not data_types:
        raise SoakConfigError("soak.symbols and soak.data_types must not be empty")
    return SoakConfig(
        soak_id=_text(root, "id", "soak"),
        symbols=tuple(symbol.strip().upper() for symbol in symbols),
        data_types=tuple(data_type.strip() for data_type in data_types),
        default_start=date.fromisoformat(
            _text(_section(root, "window", "soak"), "default_start", "soak.window")
        ),
        paths=paths,
        run=run,
        items_per_page=_integer(pages, "assumed_items_per_page", "soak.pages"),
        estimate=_load_estimate(_section(root, "estimate", "soak")),
        cost=CostSettings(
            currency=_text(cost, "currency", "soak.cost"),
            region=_text(cost, "region", "soak.cost"),
            blob=_load_blob_prices(_section(cost, "blob_hns_lrs", "soak.cost")),
            tables=_load_tables_prices(_section(cost, "tables_lrs", "soak.cost")),
            queue=_load_queue_prices(_section(cost, "queue_v2_lrs", "soak.cost")),
            container_apps=_load_container_apps_prices(
                _section(cost, "container_apps_consumption", "soak.cost")
            ),
            log_analytics=_load_log_analytics_prices(
                _section(cost, "log_analytics", "soak.cost")
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Session planning and state


def plan_closed_sessions(start: date, end: date, now: datetime) -> tuple[date, ...]:
    """Return closed XNYS session dates in the window, newest first."""
    sessions = equity_sessions(start, end, as_of=now)
    closed = [entry.session_date for entry in sessions if entry.close_time <= now]
    return tuple(reversed(closed))


def state_key(symbol: str, data_type: str, session_date: date) -> str:
    return f"{symbol}|{data_type}|{session_date.isoformat()}"


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def load_state(path: Path) -> SoakState:
    if not path.exists():
        return SoakState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SoakConfigError(f"state file is unreadable: {error}") from error
    document = _mapping(raw, "state file")
    completed_raw = document.get("completed", {})
    return SoakState(completed=_mapping(completed_raw, "state file completed"))


def save_state(path: Path, state: SoakState) -> None:
    """Atomically persist the resumable soak state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "completed": dict(sorted(state.completed.items())),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(handle.name, path)
    except OSError:
        Path(handle.name).unlink(missing_ok=True)
        raise


def append_result(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


# ---------------------------------------------------------------------------
# Ensure execution


def _extract_entry(
    response: CoverageResponse, session_date: date
) -> CoverageEntry | None:
    for entry in response.entries:
        if entry.session_date == session_date:
            return entry
    return None


def _outcome_from_entry(
    entry: CoverageEntry | None, duration_ms: int
) -> SessionOutcome:
    if entry is None:
        return SessionOutcome(
            status="error",
            rows=None,
            byte_size=None,
            object_path=None,
            error="coverage response missing the requested session",
            duration_ms=duration_ms,
        )
    return SessionOutcome(
        status=str(entry.status.value),
        rows=entry.row_count,
        byte_size=entry.byte_size,
        object_path=entry.object_path,
        error=entry.error_message,
        duration_ms=duration_ms,
    )


def ensure_session(
    client: LakeClient,
    symbol: str,
    data_type: str,
    session_date: date,
    config: SoakConfig,
) -> SessionOutcome:
    """Ensure one session and classify the resulting coverage entry."""
    request = DatasetRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType(data_type),
        symbol=symbol,
        start_date=session_date,
        end_date=session_date,
    )
    started = time.monotonic()
    last_error: str | None = None
    for _ in range(config.run.coverage_recheck_attempts):
        try:
            response = client.ensure(request)
        except LakeClientError as error:
            return SessionOutcome(
                status="error",
                rows=None,
                byte_size=None,
                object_path=None,
                error=f"ensure failed: {error}",
                duration_ms=_elapsed_ms(started),
            )
        if isinstance(response, CoverageResponse):
            return _outcome_from_entry(
                _extract_entry(response, session_date), _elapsed_ms(started)
            )
        job = client.wait_for_job(
            response.job_id,
            timeout=config.run.job_wait_deadline_seconds,
            poll_interval=config.run.poll_interval_seconds,
        )
        if not job.succeeded:
            return SessionOutcome(
                status="error",
                rows=None,
                byte_size=None,
                object_path=None,
                error=f"ensure job {job.job_id} ended in {job.state.value}"
                + (f": {job.error_message}" if job.error_message else ""),
                duration_ms=_elapsed_ms(started),
            )
        try:
            coverage = client.coverage(request)
        except LakeClientError as error:
            last_error = f"coverage query failed: {error}"
            continue
        return _outcome_from_entry(
            _extract_entry(coverage, session_date), _elapsed_ms(started)
        )
    return SessionOutcome(
        status="error",
        rows=None,
        byte_size=None,
        object_path=None,
        error=last_error
        or "coverage report was not produced within the recheck budget",
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def pages_for(rows: int | None, items_per_page: int) -> int:
    if rows is None or rows <= 0:
        return 0
    return math.ceil(rows / items_per_page)


def run_live(
    client: LakeClient,
    config: SoakConfig,
    symbols: tuple[str, ...],
    sessions: tuple[date, ...],
    state: SoakState,
    max_requests: int,
) -> tuple[Aggregate, int]:
    """Ensure every pending session, newest first; return the aggregate."""
    aggregate = Aggregate()
    requests = 0
    failures = 0
    for symbol in symbols:
        for data_type in config.data_types:
            for session_date in sessions:
                key = state_key(symbol, data_type, session_date)
                if key in state.completed:
                    continue
                if requests >= max_requests:
                    return aggregate, requests
                requests += 1
                outcome = ensure_session(
                    client, symbol, data_type, session_date, config
                )
                record: dict[str, Any] = {
                    "session_date": session_date.isoformat(),
                    "symbol": symbol,
                    "data_type": data_type,
                    "outcome": outcome.status,
                    "rows": outcome.rows,
                    "bytes": outcome.byte_size,
                    "pages_estimated": pages_for(outcome.rows, config.items_per_page),
                    "duration_ms": outcome.duration_ms,
                    "object_path": outcome.object_path,
                    "error": outcome.error,
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
                append_result(config.paths.results_file, record)
                if outcome.is_terminal:
                    state.completed[key] = record
                    save_state(config.paths.state_file, state)
                else:
                    failures += 1
                aggregate.add(outcome.status, outcome.rows, outcome.byte_size)
                aggregate.pages += pages_for(outcome.rows, config.items_per_page)
                aggregate.duration_ms += outcome.duration_ms
                if config.run.inter_request_pause_seconds > 0:
                    time.sleep(config.run.inter_request_pause_seconds)
    if failures:
        print(
            f"soak: {failures} session(s) recorded failures; re-run to resume",
            file=sys.stderr,
        )
    return aggregate, requests


# ---------------------------------------------------------------------------
# Cost projection


def _usd_per_10k(count: int, price: float) -> float:
    return count / 10_000 * price


def project_costs(
    aggregate: Aggregate,
    config: SoakConfig,
    *,
    requested_sessions: int,
) -> dict[str, Any]:
    """Project incremental Azure cost from measured soak volume.

    Assumptions (documented with published prices in the config file):
    - Blob storage is priced at the recorded byte total held for the
      configured retention horizon, prorated from the monthly list price
      (Azure bills decimal GB; 1 GB = 10**9 bytes).
    - One Blob write operation per completed Parquet object.
    - Two Table transactions per terminal session (coverage row plus the
      ensure job upsert); catalog metadata storage is far below 1 GB and is
      not billed separately.
    - Queue operations are zero: the dev composition runs ensure inline
      through the launcher rather than through the queue worker.
    - Container Apps compute is priced from the measured per-session API
      durations at the deployed container size; the always-on environment
      management fee is a fixed cost excluded from the incremental view.
    - Log Analytics ingestion is estimated from request volume; the first
      ``included_free_gb_per_month`` GB across the workspace is free.
    """
    cost = config.cost
    estimate = config.estimate
    gb = aggregate.byte_total / 1e9
    retention_fraction = estimate.storage_retention_days / 30.0
    blob_storage = gb * cost.blob.stored_usd_per_gb_month * retention_fraction
    blob_writes = aggregate.sessions_complete
    blob_write_cost = _usd_per_10k(blob_writes, cost.blob.write_usd_per_10k)
    table_transactions = aggregate.sessions_complete + aggregate.sessions_empty
    table_transaction_cost = _usd_per_10k(
        table_transactions, cost.tables.transaction_usd_per_10k
    )
    duration_seconds = aggregate.duration_ms / 1000
    vcpu_seconds = duration_seconds * estimate.api_vcores
    memory_gib_seconds = duration_seconds * estimate.api_memory_gib
    compute_cost = (
        vcpu_seconds * cost.container_apps.vcpu_usd_per_second
        + memory_gib_seconds * cost.container_apps.memory_usd_per_gib_second
        + requested_sessions / 1e6 * cost.container_apps.requests_usd_per_million
    )
    ingestion_gb = (
        requested_sessions / 1000 * (estimate.log_analytics_gb_per_1000_requests)
    )
    billable_gb = max(
        0.0,
        ingestion_gb - cost.log_analytics.included_free_gb_per_month,
    )
    log_cost = billable_gb * cost.log_analytics.ingestion_usd_per_gb
    total = (
        blob_storage
        + blob_write_cost
        + table_transaction_cost
        + compute_cost
        + log_cost
    )
    return {
        "currency": cost.currency,
        "region": cost.region,
        "measured": {
            "bytes": aggregate.byte_total,
            "rows": aggregate.rows,
            "pages_estimated": aggregate.pages,
            "sessions_complete": aggregate.sessions_complete,
            "sessions_empty": aggregate.sessions_empty,
            "sessions_failed": aggregate.sessions_failed,
            "api_duration_seconds": round(duration_seconds, 3),
            "http_requests": requested_sessions,
        },
        "line_items_usd": {
            "blob_storage_retention": round(blob_storage, 6),
            "blob_write_operations": round(blob_write_cost, 6),
            "table_transactions": round(table_transaction_cost, 6),
            "container_apps_compute_and_requests": round(compute_cost, 6),
            "log_analytics_ingestion": round(log_cost, 6),
            "queue_operations": 0.0,
        },
        "total_usd": round(total, 6),
        "assumptions": {
            "storage_retention_days": estimate.storage_retention_days,
            "api_vcores": estimate.api_vcores,
            "api_memory_gib": estimate.api_memory_gib,
            "items_per_page": config.items_per_page,
            "price_source": "Azure Retail Prices API, retrieved 2026-09-05",
        },
    }


def dry_run(
    config: SoakConfig,
    symbols: tuple[str, ...],
    sessions: tuple[date, ...],
    state: SoakState,
) -> dict[str, Any]:
    """Plan without network access and project costs from assumed bytes."""
    pending_by_stream: dict[str, int] = {}
    assumed_bytes_total = 0
    for symbol in symbols:
        for data_type in config.data_types:
            stream_pending = 0
            for session_date in sessions:
                if state_key(symbol, data_type, session_date) in state.completed:
                    continue
                stream_pending += 1
                per_type = config.estimate.assumed_bytes.get(symbol, {})
                assumed_bytes_total += per_type.get(data_type, 0)
            pending_by_stream[f"{symbol}/{data_type}"] = stream_pending
    assumed_aggregate = Aggregate()
    assumed_aggregate.sessions_complete = sum(pending_by_stream.values())
    assumed_aggregate.byte_total = assumed_bytes_total
    projection = project_costs(
        assumed_aggregate,
        config,
        requested_sessions=assumed_aggregate.sessions_complete,
    )
    return {
        "mode": "dry-run",
        "soak_id": config.soak_id,
        "window": {
            "first_session": sessions[-1].isoformat() if sessions else None,
            "last_session": sessions[0].isoformat() if sessions else None,
            "closed_sessions": len(sessions),
        },
        "plan": pending_by_stream,
        "already_completed": len(state.completed),
        "projected_cost": projection,
    }


# ---------------------------------------------------------------------------
# Entry point


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO date: {value}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_soak.py",
        description="Full-history AAPL/SPY market-lake soak driver.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/lake/soak-aapl-spy.yaml"),
        help="soak YAML config (default: config/lake/soak-aapl-spy.yaml)",
    )
    parser.add_argument(
        "--base-url",
        help="market-lake API base URL; required for live runs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and project costs without network access",
    )
    parser.add_argument("--symbols", help="comma-separated override, e.g. AAPL,SPY")
    parser.add_argument("--start-date", type=_parse_date, help="window start override")
    parser.add_argument("--end-date", type=_parse_date, help="window end override")
    parser.add_argument(
        "--limit-sessions",
        type=int,
        help="bound the plan to the N most recent closed sessions",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        help="bound ensure requests in this invocation (default from config)",
    )
    parser.add_argument("--state-file", type=Path, help="override state file path")
    parser.add_argument("--results-file", type=Path, help="override results path")
    parser.add_argument("--summary-file", type=Path, help="override summary path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_soak_config(args.config)
    except SoakConfigError as error:
        print(f"run_soak: {error}", file=sys.stderr)
        return 2
    paths = config.paths
    if args.state_file:
        paths = replace(paths, state_file=args.state_file)
    if args.results_file:
        paths = replace(paths, results_file=args.results_file)
    if args.summary_file:
        paths = replace(paths, summary_file=args.summary_file)
    config = replace(config, paths=paths)
    if args.max_requests is not None:
        config = replace(
            config,
            run=replace(config.run, max_requests=args.max_requests),
        )

    symbols = config.symbols
    if args.symbols:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in args.symbols.split(",")
            if symbol.strip()
        )
    now = datetime.now(UTC)
    start = args.start_date or config.default_start
    end = args.end_date or now.date()
    sessions = plan_closed_sessions(start, end, now)
    if args.limit_sessions is not None:
        if args.limit_sessions < 1:
            print("run_soak: --limit-sessions must be at least 1", file=sys.stderr)
            return 2
        sessions = sessions[: args.limit_sessions]
    try:
        state = load_state(config.paths.state_file)
    except SoakConfigError as error:
        print(f"run_soak: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        plan = dry_run(config, symbols, sessions, state)
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    if args.base_url is None:
        print("run_soak: --base-url is required for a live run", file=sys.stderr)
        return 2
    bearer = os.environ.get(BEARER_ENV, "").strip()
    if not bearer:
        print(f"run_soak: {BEARER_ENV} must be set for a live run", file=sys.stderr)
        return 2
    try:
        with LakeClient(base_url=args.base_url, bearer_token=bearer) as client:
            aggregate, requests = run_live(
                client,
                config,
                symbols,
                sessions,
                state,
                config.run.max_requests,
            )
    except (LakeClientError, OSError) as error:
        print(f"run_soak: live soak failed: {error}", file=sys.stderr)
        print(
            "run_soak: completed sessions are in the state file; re-run to resume",
            file=sys.stderr,
        )
        return 1
    projection = project_costs(aggregate, config, requested_sessions=requests)
    summary: dict[str, Any] = {
        "mode": "live",
        "soak_id": config.soak_id,
        "completed_state_entries": len(state.completed),
        "cost_projection": projection,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    config.paths.summary_file.parent.mkdir(parents=True, exist_ok=True)
    config.paths.summary_file.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if aggregate.sessions_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
