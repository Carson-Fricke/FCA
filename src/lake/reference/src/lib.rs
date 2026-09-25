//! Point-in-time reference data ingest.
//!
//! Ports the ingest half of the Python `lake.reference` package: the SEC EDGAR
//! XBRL company facts parser, the Nasdaq Trader symbol directory parser, the
//! fair-access HTTP client (SEC 10 requests/second cap and the required
//! descriptive `User-Agent`), the versioned reference Arrow schemas, the
//! date-first Hive partition layout, the idempotent classification and
//! fundamentals ingest transactions, and the point-in-time read path used as
//! the parity oracle for written partitions.
//!
//! Layout contract (frozen):
//!
//! ```text
//! date=YYYY-MM-DD/asset_class=reference/data_type=classification/SNAPSHOT.parquet
//! date=YYYY-MM-DD/asset_class=reference/data_type=fundamentals/CIK0000320193.parquet
//! ```
//!
//! Classification snapshots replace their whole partition object per knowledge
//! date. Fundamentals merge into per-CIK, per-`filed` objects so incremental
//! fetches never drop facts already stored for the same knowledge date. The
//! `filed` date is the point-in-time knowledge key for facts and the file
//! creation timestamp's US/Eastern calendar date is the knowledge key for
//! snapshots.
//!
//! The Python modules `lake.reference.query` and `lake.reference.schemas`
//! remain the retained surface for Thesis consumers until that migration
//! lands; this crate's `query` module exists so the written partitions can be
//! asserted against the same point-in-time contract in Rust.

pub mod configuration;
pub mod edgar;
pub mod error;
pub mod http;
pub mod ingest;
pub mod nasdaq;
pub mod partitions;
pub mod query;
pub mod schemas;

pub use configuration::{
    load_reference_settings, reference_config_dir, ReferenceHttpSettings, ReferenceRetrySettings,
    ReferenceSettings,
};
pub use edgar::{parse_companyconcept, parse_companyfacts, XbrlFact};
pub use error::ReferenceError;
pub use http::{
    ReferenceFetchClient, ReferenceHTTPClient, ReferenceTransport, ReqwestReferenceTransport,
};
pub use ingest::{
    filter_facts, ingest_classification, ingest_fundamentals_increment, ClassificationIngest,
    ClassificationIngestResult, ClassificationSummary, FundamentalsIngest,
    FundamentalsIngestResult, FundamentalsSummary, ObjectSummary, ReferenceCoverageSink,
    ReferenceIngestCommitter, ReferenceObjectStore, StagedReferenceObject,
    CLASSIFICATION_COVERAGE_SYMBOL,
};
pub use nasdaq::{parse_nasdaq_traded, NasdaqSecurity, NasdaqSnapshot};
pub use partitions::{
    cik_object_name, cik_symbol, parse_cik_symbol, reference_partition_path,
    CLASSIFICATION_OBJECT_NAME,
};
pub use query::{classification_snapshot, fundamentals_facts, latest_fundamentals, FactFilter};
pub use schemas::{
    classification_schema, classification_table, fundamentals_schema, fundamentals_table,
    ClassificationRow, FactRow, REFERENCE_SCHEMA_VERSION,
};
