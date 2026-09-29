//! Typed views of the lake JSON responses the client consumes.

use std::collections::BTreeMap;

use serde::Deserialize;

/// One classified session in a coverage report.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CoverageEntry {
    /// Session the entry describes.
    pub session_date: String,
    /// Coverage classification (`complete`, `empty`, `delisted`, `partial`,
    /// `missing`, or `error`).
    pub status: String,
    /// Whether the session is currently trading.
    #[serde(default)]
    pub is_partial: bool,
    /// Canonical symbol, when the route reports it.
    #[serde(default)]
    pub symbol: Option<String>,
    /// Data type, when the route reports it.
    #[serde(default)]
    pub data_type: Option<String>,
    /// Failure detail when the status is `error`.
    #[serde(default)]
    pub error_message: Option<String>,
}

/// Calendar-aware coverage for one symbol, data type, and date range.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct CoverageReport {
    /// Asset class of the report.
    pub asset_class: String,
    /// Data type of the report.
    pub data_type: String,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Inclusive first session date.
    pub start_date: String,
    /// Inclusive last session date.
    pub end_date: String,
    /// Classifications in session order.
    pub entries: Vec<CoverageEntry>,
}

/// The persisted state of one ensure job.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct JobStatus {
    /// Stable job identifier.
    pub job_id: String,
    /// Current job state.
    pub state: String,
    /// Failure detail when the job failed.
    #[serde(default)]
    pub error_message: Option<String>,
}

/// The `POST /v1/catalog/ensure` union response.
///
/// The route answers either a satisfied coverage report or an accepted job; a
/// body without either still polls coverage, exactly like the historical
/// client.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub(crate) struct EnsureResponse {
    /// Present when the route answered coverage directly.
    #[serde(default)]
    pub entries: Option<Vec<CoverageEntry>>,
    /// Present when the route accepted asynchronous ensure work.
    #[serde(default)]
    pub job_id: Option<String>,
}

/// The `GET /v1/reference/universe` response.
#[derive(Debug, Deserialize)]
pub(crate) struct UniverseResponse {
    /// As-of universe symbols.
    pub symbols: Vec<String>,
}

/// The `GET /v1/reference/facts` response.
#[derive(Debug, Deserialize)]
pub(crate) struct FactsResponse {
    /// Fact-sheet entries per symbol.
    pub facts: BTreeMap<String, BTreeMap<String, String>>,
}
