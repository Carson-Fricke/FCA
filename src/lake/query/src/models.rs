//! Request, response, and port types for the file-query mode.
//!
//! The value objects mirror the route's strict response models exactly:
//! `asset_class`, `data_type`, `symbol`, `session_date`, `object_path`, `url`,
//! `sas_url`, `row_count`, `byte_size`, and `checksum`. `url` and `sas_url` are
//! always the same issued value, as the Python route builds them from one
//! `SasIssuer::issue` call.

use std::sync::Arc;

use futures_util::future::BoxFuture;
use lake_catalog::{CatalogRepository, ListingRepository};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use serde::ser::{SerializeStruct, Serializer};
use serde::Serialize;

use crate::bounded::BoundedDuckDbQuery;
use crate::error::QueryApplicationError;

/// A file-query request over one symbol and inclusive session-date window.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryRequest {
    /// Asset class of the requested partitions.
    pub asset_class: AssetClass,
    /// Data type of the requested partitions.
    pub data_type: DataType,
    /// Symbol to query; required for equity and crypto.
    pub symbol: Option<String>,
    /// Inclusive first session date.
    pub start_date: Date,
    /// Inclusive last session date.
    pub end_date: Date,
    /// Whether a coverage gap may be repaired by the ensure callback.
    pub ensure: bool,
    /// Canonical bars interval token; required for bars and absent otherwise.
    pub interval: Option<String>,
}

/// One complete catalog partition and its read-only download URL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryFile {
    /// Asset class of the partition.
    pub asset_class: AssetClass,
    /// Data type of the partition.
    pub data_type: DataType,
    /// Canonical uppercase symbol.
    pub symbol: String,
    /// Session the partition covers.
    pub session_date: Date,
    /// Canonical Hive object path.
    pub object_path: String,
    /// Issued read-only URL.
    pub url: String,
    /// The same issued read-only URL, matching the route's `sas_url`.
    pub sas_url: Option<String>,
    /// Rows written for this symbol-day.
    pub row_count: u64,
    /// Bytes written for this symbol-day.
    pub byte_size: u64,
    /// Content digest of the written object, if any.
    pub checksum: Option<String>,
    /// Canonical bars interval token; omitted for every other data type.
    pub interval: Option<String>,
}

impl Serialize for FileQueryFile {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let fields = 10 + usize::from(self.interval.is_some());
        let mut state = serializer.serialize_struct("FileQueryFile", fields)?;
        state.serialize_field("asset_class", self.asset_class.value())?;
        state.serialize_field("data_type", self.data_type.value())?;
        state.serialize_field("symbol", &self.symbol)?;
        state.serialize_field("session_date", &self.session_date.isoformat())?;
        state.serialize_field("object_path", &self.object_path)?;
        state.serialize_field("url", &self.url)?;
        state.serialize_field("sas_url", &self.sas_url)?;
        state.serialize_field("row_count", &self.row_count)?;
        state.serialize_field("byte_size", &self.byte_size)?;
        state.serialize_field("checksum", &self.checksum)?;
        if let Some(interval) = &self.interval {
            state.serialize_field("interval", interval)?;
        }
        state.end()
    }
}

/// Bounded summary of the requested sessions.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryResults {
    /// Number of sessions the request planned.
    pub requested_sessions: u64,
    /// Number of files returned.
    pub returned_files: u64,
    /// Number of complete partitions returned.
    pub complete_sessions: u64,
    /// Number of sessions still incomplete after any ensure.
    pub pending_sessions: u64,
    /// `"complete"` or `"ensure_pending"`.
    pub state: String,
}

impl Serialize for FileQueryResults {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("FileQueryResults", 5)?;
        state.serialize_field("requested_sessions", &self.requested_sessions)?;
        state.serialize_field("returned_files", &self.returned_files)?;
        state.serialize_field("complete_sessions", &self.complete_sessions)?;
        state.serialize_field("pending_sessions", &self.pending_sessions)?;
        state.serialize_field("state", &self.state)?;
        state.end()
    }
}

/// Files and catalog results returned by the file-query mode.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileQueryResponse {
    /// Canonical complete partitions with issued read URLs.
    pub files: Vec<FileQueryFile>,
    /// Summary of the requested sessions.
    pub results: FileQueryResults,
    /// Stable response limitations.
    pub limitations: Vec<String>,
}

impl Serialize for FileQueryResponse {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        let mut state = serializer.serialize_struct("FileQueryResponse", 3)?;
        state.serialize_field("files", &self.files)?;
        state.serialize_field("results", &self.results)?;
        state.serialize_field("limitations", &self.limitations)?;
        state.end()
    }
}

/// Issues read URLs for canonical lake object paths.
///
/// The lake's storage crate owns the real SAS issuer; this port depends only on
/// the capability so the application layer stays provider independent.
pub trait FileUrlIssuer: Send + Sync {
    /// Return a read-only URL for a canonical object path.
    fn issue(&self, object_path: &str) -> Result<String, DataError>;
}

/// Injected coverage-ensure callback:
/// `(asset_class, data_type, interval, symbol, start_date, end_date)`.
pub type EnsureCallback = Arc<
    dyn Fn(
            AssetClass,
            DataType,
            Option<String>,
            String,
            Date,
            Date,
        ) -> BoxFuture<'static, Result<(), QueryApplicationError>>
        + Send
        + Sync,
>;

/// Everything a file or SQL query needs that is not part of the request.
pub struct FileQueryContext<'a> {
    /// Coverage catalog the request is classified against.
    pub catalog: &'a dyn CatalogRepository,
    /// Optional listings repository for point-in-time delisting classification.
    pub listings: Option<&'a dyn ListingRepository>,
    /// Optional read-URL issuer; required when any file is returned.
    pub sas_issuer: Option<&'a dyn FileUrlIssuer>,
    /// Optional coverage-ensure callback.
    pub ensure_callback: Option<EnsureCallback>,
    /// Point-in-time instant used for coverage classification.
    pub as_of: DateTime,
    /// Optional bounded DuckDB engine backing SQL-mode queries.
    pub bounded_query: Option<&'a BoundedDuckDbQuery>,
    /// Whether the server authorizes `force` overrides for SQL queries.
    pub force_authorized: bool,
}

#[cfg(test)]
#[path = "../tests/unit/models.rs"]
mod tests;
