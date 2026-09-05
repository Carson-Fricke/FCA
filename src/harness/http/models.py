"""Strict request and response models for the quant-harness API boundary."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from harness.results.store import RunStatus
from lake.catalog.models import JobState
from lake.http.models import StrictModel


class RunResponse(StrictModel):
    """One stored backtest run's catalog row."""

    run_id: str
    status: RunStatus
    strategy_id: str
    data_start: date | None = None
    data_end: date | None = None
    run_digest: str | None = None
    configuration_digest: str | None = None
    artifact_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    metrics: dict[str, str] = {}


class RunListResponse(StrictModel):
    """Stored backtest runs sorted by run ID."""

    runs: tuple[RunResponse, ...]


class ArtifactSummary(StrictModel):
    """One stored run artifact with optional read-only SAS access."""

    name: str
    size: int = Field(ge=0)
    object_path: str
    sas_url: str | None = None


class ArtifactListResponse(StrictModel):
    """The artifacts stored below one run's blob prefix."""

    run_id: str
    artifacts: tuple[ArtifactSummary, ...]


class LaunchAcceptedResponse(StrictModel):
    """A submitted backtest launch that continues as an asynchronous job."""

    job_id: str
    state: JobState


class BacktestJobStatusResponse(StrictModel):
    """The persisted state of one backtest launch job."""

    job_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None


__all__ = [
    "ArtifactListResponse",
    "ArtifactSummary",
    "BacktestJobStatusResponse",
    "LaunchAcceptedResponse",
    "RunListResponse",
    "RunResponse",
    "RunStatus",
]
