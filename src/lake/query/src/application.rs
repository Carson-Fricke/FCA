//! Provider-independent orchestration for file queries.
//!
//! Ports the file path of `lake/query/application.py` and the file-mode
//! response the `POST /v1/query` route builds from it. A request is validated,
//! coverage is classified through [`requested_coverage`] with the `"query"`
//! operation, a gap is repaired through the injected ensure callback when the
//! caller asked for it, and only complete partitions with a canonical object
//! path become files.
//!
//! Classification mirrors `_classify_files` exactly: an entry is usable only
//! when its state is `complete` **and** its object path equals the canonical
//! `date=/asset_class=/data_type=/SYMBOL.parquet` partition. Every other entry
//! is incomplete, including terminal `empty` and `delisted` sessions, so a file
//! query never pretends a session with no partition is covered.
//!
//! The bounded DuckDB SQL mode is not ported; `query_unavailable` therefore
//! names no reachable path in this crate.

use lake_contracts::{partition_path, AssetClass};
use lake_coverage::{
    requested_coverage, CoverageReport, CoverageState, EnsureSpec, SessionCoverage,
};
use serde_json::json;

use crate::error::QueryApplicationError;
use crate::models::{
    FileQueryContext, FileQueryFile, FileQueryRequest, FileQueryResponse, FileQueryResults,
    FileUrlIssuer,
};

/// Execute one file query and return its canonical files and read URLs.
///
/// The request is validated (equity or crypto with a symbol), coverage is
/// computed, and:
///
/// * without `ensure`, any incomplete session is a `coverage_incomplete`
///   rejection carrying the pending sessions;
/// * with `ensure`, the injected callback is awaited once and coverage is
///   recomputed; a missing callback is `ensure_unavailable`, a failing callback
///   is `ensure_failed`, and remaining incomplete sessions are reported as an
///   `ensure_pending` result;
/// * files need a configured issuer when at least one complete session is
///   returned, otherwise the rejection is `sas_unavailable`.
pub async fn execute_file_query(
    context: FileQueryContext<'_>,
    request: &FileQueryRequest,
) -> Result<FileQueryResponse, QueryApplicationError> {
    let symbol = validate_request(request)?;
    let spec = EnsureSpec {
        asset_class: request.asset_class,
        data_type: request.data_type,
        symbol: symbol.to_string(),
        start_date: request.start_date,
        end_date: request.end_date,
    };

    let mut report = coverage_report(&context, &spec)?;
    let (mut usable, mut incomplete) = classify(&report, request, symbol);
    if !incomplete.is_empty() && !request.ensure {
        return Err(incomplete_error(&incomplete, false));
    }

    if !incomplete.is_empty() {
        let Some(callback) = context.ensure_callback.as_ref() else {
            return Err(QueryApplicationError::new(
                "ensure_unavailable",
                "coverage ensure is not configured",
                None,
            ));
        };
        callback(
            spec.asset_class,
            spec.data_type,
            spec.symbol.clone(),
            spec.start_date,
            spec.end_date,
        )
        .await
        .map_err(|error| {
            QueryApplicationError::new(
                "ensure_failed",
                "coverage ensure failed",
                Some(json!({"error": error.to_string()})),
            )
        })?;
        report = coverage_report(&context, &spec)?;
        (usable, incomplete) = classify(&report, request, symbol);
    }

    let pending_sessions = incomplete.len() as u64;
    let ensure_pending = pending_sessions > 0;

    let mut files = Vec::new();
    if let Some(issuer) = context.sas_issuer {
        files = build_files(&usable, issuer)?;
    } else if !usable.is_empty() {
        return Err(sas_unavailable());
    }

    let mut limitations = vec![
        "Only complete catalog partitions are returned.".to_string(),
        "URLs are read-only, object-scoped, and expire according to SAS policy.".to_string(),
    ];
    if ensure_pending {
        limitations.push("Coverage ensure was requested but is not complete yet.".to_string());
    }

    Ok(FileQueryResponse {
        results: FileQueryResults {
            requested_sessions: report.entries.len() as u64,
            returned_files: files.len() as u64,
            complete_sessions: files.len() as u64,
            pending_sessions,
            state: if ensure_pending {
                "ensure_pending".to_string()
            } else {
                "complete".to_string()
            },
        },
        files,
        limitations,
    })
}

/// Validate the request and return its raw symbol.
///
/// Mirrors the route's model validator for equity and crypto: the symbol is
/// required. Option selection is out of scope for this crate, so any other
/// asset class is rejected as a validation error rather than planned.
fn validate_request(request: &FileQueryRequest) -> Result<&str, QueryApplicationError> {
    if !matches!(request.asset_class, AssetClass::Equity | AssetClass::Crypto) {
        return Err(QueryApplicationError::new(
            "validation_error",
            "file queries support equity and crypto only",
            None,
        ));
    }
    let symbol = request.symbol.as_deref().unwrap_or("");
    if symbol.trim().is_empty() {
        return Err(QueryApplicationError::new(
            "validation_error",
            "symbol is required for equity and crypto queries",
            None,
        ));
    }
    Ok(symbol)
}

/// Classify one requested report into usable and incomplete entries.
///
/// Mirrors `_classify_files`: an entry is usable only when it is `complete` and
/// its object path is the canonical partition for the request.
fn classify(
    report: &CoverageReport,
    request: &FileQueryRequest,
    symbol: &str,
) -> (Vec<SessionCoverage>, Vec<SessionCoverage>) {
    let mut usable = Vec::new();
    let mut incomplete = Vec::new();
    for entry in &report.entries {
        if entry.state == CoverageState::Complete && has_expected_path(entry, request, symbol) {
            usable.push(entry.clone());
        } else {
            incomplete.push(entry.clone());
        }
    }
    (usable, incomplete)
}

/// Return whether a complete entry points at its canonical object.
fn has_expected_path(entry: &SessionCoverage, request: &FileQueryRequest, symbol: &str) -> bool {
    let Some(record) = entry.record.as_ref() else {
        return false;
    };
    let Some(object_path) = record.object_path.as_deref() else {
        return false;
    };
    partition_path(
        entry.session_date(),
        request.asset_class,
        request.data_type,
        symbol,
    )
    .is_ok_and(|expected| object_path == expected)
}

/// Compute one coverage report with the Python `"query"` operation.
fn coverage_report(
    context: &FileQueryContext<'_>,
    spec: &EnsureSpec,
) -> Result<CoverageReport, QueryApplicationError> {
    requested_coverage(
        context.catalog,
        context.listings,
        spec,
        context.as_of,
        "query",
    )
    .map_err(QueryApplicationError::from)
}

/// Build the Python `coverage_incomplete` rejection for pending sessions.
fn incomplete_error(entries: &[SessionCoverage], ensure_requested: bool) -> QueryApplicationError {
    let message = if ensure_requested {
        "coverage remains incomplete after ensure"
    } else {
        "requested coverage is incomplete; set ensure=true to request coverage"
    };
    let sessions: Vec<serde_json::Value> = entries
        .iter()
        .map(|entry| {
            json!({
                "session_date": entry.session_date().isoformat(),
                "state": entry.state.value(),
            })
        })
        .collect();
    QueryApplicationError::new(
        "coverage_incomplete",
        message,
        Some(json!({ "sessions": sessions })),
    )
}

/// Build the Python `sas_unavailable` rejection.
fn sas_unavailable() -> QueryApplicationError {
    QueryApplicationError::new(
        "sas_unavailable",
        "file query SAS issuance is not configured",
        None,
    )
}

/// Map complete entries to files with one issued URL each.
fn build_files(
    entries: &[SessionCoverage],
    issuer: &dyn FileUrlIssuer,
) -> Result<Vec<FileQueryFile>, QueryApplicationError> {
    let mut files = Vec::with_capacity(entries.len());
    for entry in entries {
        let Some(record) = entry.record.as_ref() else {
            return Err(missing_object_path());
        };
        let Some(object_path) = record.object_path.as_deref() else {
            return Err(missing_object_path());
        };
        let url = issuer.issue(object_path).map_err(|error| {
            QueryApplicationError::new(
                "sas_unavailable",
                format!("file query SAS issuance failed: {error}"),
                Some(json!({ "error": error.to_string() })),
            )
        })?;
        files.push(FileQueryFile {
            asset_class: entry.asset_class,
            data_type: entry.data_type,
            symbol: entry.symbol.clone(),
            session_date: entry.session_date(),
            object_path: object_path.to_string(),
            url: url.clone(),
            sas_url: Some(url),
            row_count: record.row_count,
            byte_size: record.byte_size,
            checksum: record.checksum.clone(),
        });
    }
    Ok(files)
}

/// The Python `RuntimeError` for a usable entry without an object path.
///
/// Classification guarantees this cannot happen; the mapping keeps the crate
/// panic-free if a future classifier regresses.
fn missing_object_path() -> QueryApplicationError {
    QueryApplicationError::new(
        "internal_error",
        "complete coverage entry has no object path",
        None,
    )
}

#[cfg(test)]
#[path = "../tests/unit/application.rs"]
mod tests;
