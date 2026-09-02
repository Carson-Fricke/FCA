"""Bounded query facade."""

from lake.query.service import (
    BoundedDuckDBQuery,
    BoundedQueryError,
    BoundedQueryResult,
    EnsuredQueryFile,
    ForceOverrideNotAuthorizedError,
    QueryLimits,
    QueryStorageUnavailableError,
    ResultLimitExceededError,
    ScanLimitExceededError,
    SqlNotAllowedError,
    StoragePathNotAllowedError,
    validate_read_only_sql,
)

__all__ = [
    "BoundedDuckDBQuery",
    "BoundedQueryError",
    "BoundedQueryResult",
    "EnsuredQueryFile",
    "ForceOverrideNotAuthorizedError",
    "QueryLimits",
    "QueryStorageUnavailableError",
    "ResultLimitExceededError",
    "ScanLimitExceededError",
    "SqlNotAllowedError",
    "StoragePathNotAllowedError",
    "validate_read_only_sql",
]
