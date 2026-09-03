"""Deliberate public facade for the thin market-lake Python client."""

from lakeclient.client import (
    JobPollingTimeout,
    LakeClient,
    LakeClientError,
    LakeProtocolError,
)
from lakeclient.duckdb import nbbo_spread, read_files
from lakeclient.models import (
    AssetClass,
    CoverageEntry,
    CoverageResponse,
    CoverageState,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryFile,
    FileQueryResponse,
    FileQueryResults,
    FileQueryState,
    JobState,
    JobStatus,
)

__all__ = [
    "AssetClass",
    "CoverageEntry",
    "CoverageResponse",
    "CoverageState",
    "DataType",
    "DatasetRequest",
    "EnsureAccepted",
    "FileQueryFile",
    "FileQueryResponse",
    "FileQueryResults",
    "FileQueryState",
    "JobPollingTimeout",
    "JobState",
    "JobStatus",
    "LakeClient",
    "LakeClientError",
    "LakeProtocolError",
    "nbbo_spread",
    "read_files",
]
