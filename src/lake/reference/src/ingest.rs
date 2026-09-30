//! Idempotent point-in-time reference dataset ingestion.
//!
//! Ports `lake.reference.ingest`. Classification snapshots replace their whole
//! partition object: each daily directory is one complete, deterministic
//! archive row set. Fundamentals merge into existing per-CIK, per-`filed`
//! objects so incremental `companyfacts` fetches never drop facts from other
//! tags already stored for the same knowledge date. Both paths publish staged
//! objects and coverage rows through one commit boundary, mirroring the
//! equity ingest transaction.

use std::collections::BTreeMap;
use std::sync::Arc;

use arrow_array::RecordBatch;
use lake_catalog::{CatalogRepository, CoverageRecord, CoverageStatus};
use lake_contracts::{AssetClass, DataError, DataType, Date};
use lake_storage::ports::BlobStore;
use lake_storage::{LocalFilesystemStore, ParquetMetadata};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::{WriterProperties, WriterVersion};
use sha2::{Digest, Sha256};

use crate::company_tickers::{CompanyTickers, COMPANY_TICKERS_URL};
use crate::configuration::ReferenceSettings;
use crate::edgar::{company_facts_url, parse_companyfacts, XbrlFact};
use crate::error::ReferenceError;
use crate::http::ReferenceFetchClient;
use crate::nasdaq::{EasternInstant, NasdaqSnapshot};
use crate::partitions::{
    cik_object_name, cik_symbol, reference_partition_path, CLASSIFICATION_OBJECT_NAME,
};
use crate::schemas::{
    classification_table_with_extras, fundamentals_table, read_fundamentals_rows,
    ClassificationExtras, ClassificationRow, FactRow,
};

/// The catalog symbol covering the whole classification snapshot.
pub const CLASSIFICATION_COVERAGE_SYMBOL: &str = "DIRECTORY";

/// Catalog surface needed to persist reference coverage rows.
pub trait ReferenceCoverageSink: Send + Sync {
    /// Replace the row stored under the record's logical key.
    fn upsert(&self, record: &CoverageRecord) -> Result<(), ReferenceError>;
}

impl<T: CatalogRepository + ?Sized> ReferenceCoverageSink for T {
    fn upsert(&self, record: &CoverageRecord) -> Result<(), ReferenceError> {
        CatalogRepository::upsert(self, record).map_err(ReferenceError::Data)
    }
}

/// Store surface the reference ingest needs: atomic replacement plus reading
/// previously committed objects, returning `Ok(None)` when absent.
pub trait ReferenceObjectStore: Send + Sync {
    /// Atomically make `payload` the complete contents at `relative_path`.
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError>;

    /// Return object bytes, or `None` when the object does not exist.
    fn read_optional(&self, relative_path: &str) -> Result<Option<Vec<u8>>, DataError>;
}

/// Adapter for the local filesystem store.
///
/// `replace` goes through the store's atomic rename; `read_optional` reads
/// crate-generated relative paths below the resolved store root and maps a
/// missing file to `None`.
impl ReferenceObjectStore for LocalFilesystemStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        BlobStore::replace(self, relative_path, payload)
    }

    fn read_optional(&self, relative_path: &str) -> Result<Option<Vec<u8>>, DataError> {
        let mut target = self.root().to_path_buf();
        for part in relative_path.split('/') {
            target.push(part);
        }
        match std::fs::read(&target) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(DataError::io(error)),
        }
    }
}

/// A complete Parquet payload waiting for an atomic reference commit.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StagedReferenceObject {
    /// Metadata for the exact bytes committed to an object store.
    pub metadata: ParquetMetadata,
    /// The serialized Parquet payload.
    pub payload: Vec<u8>,
}

/// Published metadata for one symbol directory snapshot.
#[derive(Clone, Debug)]
pub struct ClassificationIngestResult {
    /// The snapshot's knowledge date.
    pub snapshot_date: Date,
    /// The file creation instant in US/Eastern and UTC.
    pub file_creation_time: EasternInstant,
    /// The published coverage rows.
    pub coverage: Vec<CoverageRecord>,
    /// The published object metadata.
    pub objects: Vec<ParquetMetadata>,
}

/// Published metadata for one fundamentals ingest batch.
#[derive(Clone, Debug)]
pub struct FundamentalsIngestResult {
    /// The sorted unique CIKs of the published objects.
    pub ciks: Vec<u64>,
    /// The published coverage rows.
    pub coverage: Vec<CoverageRecord>,
    /// The published object metadata.
    pub objects: Vec<ParquetMetadata>,
}

/// Publish staged objects before atomically replacing coverage rows.
pub struct ReferenceIngestCommitter {
    object_store: Arc<dyn ReferenceObjectStore>,
    catalog: Arc<dyn ReferenceCoverageSink>,
}

impl ReferenceIngestCommitter {
    /// Build a committer over an object store and a coverage sink.
    pub fn new(
        object_store: Arc<dyn ReferenceObjectStore>,
        catalog: Arc<dyn ReferenceCoverageSink>,
    ) -> Self {
        Self {
            object_store,
            catalog,
        }
    }

    /// The object store behind this committer.
    pub fn object_store(&self) -> &Arc<dyn ReferenceObjectStore> {
        &self.object_store
    }

    /// Replace objects, then replace their logical coverage keys.
    pub fn commit(
        &self,
        objects: &[StagedReferenceObject],
        coverage: &[CoverageRecord],
    ) -> Result<(), ReferenceError> {
        for staged in objects {
            self.object_store
                .replace(&staged.metadata.path, &staged.payload)?;
        }
        for record in coverage {
            self.catalog.upsert(record)?;
        }
        Ok(())
    }
}

/// Stage and publish one complete symbol directory snapshot.
pub struct ClassificationIngest {
    committer: ReferenceIngestCommitter,
}

impl ClassificationIngest {
    /// Build over a committer.
    pub fn new(committer: ReferenceIngestCommitter) -> Self {
        Self { committer }
    }

    /// Normalize, stage, and commit the snapshot for its knowledge date.
    ///
    /// Every security is joined against the SEC ticker map and the resolved
    /// CIK lands in the snapshot's additive `cik` column, so the mapping is
    /// stored under the same knowledge date as the directory rows it came
    /// with. `sic` and `sic_industry` stay absent: the only EDGAR source for
    /// them is the per-CIK submissions document, which would add thousands of
    /// requests to every directory ingest (decision D006 defers that).
    pub fn run(
        &self,
        snapshot: &NasdaqSnapshot,
        tickers: &CompanyTickers,
    ) -> Result<ClassificationIngestResult, ReferenceError> {
        let records: Vec<(ClassificationRow, ClassificationExtras)> = snapshot
            .securities
            .iter()
            .map(|security| {
                let row = ClassificationRow {
                    file_creation_time: snapshot.file_creation_time.utc(),
                    symbol: security.symbol.clone(),
                    security_name: security.security_name.clone(),
                    listing_exchange: security.listing_exchange.clone(),
                    market_category: security.market_category.clone(),
                    etf: security.etf,
                    round_lot_size: security.round_lot_size,
                    test_issue: security.test_issue,
                    financial_status: security.financial_status.clone(),
                };
                let extras = ClassificationExtras {
                    cik: tickers.cik_for(&security.symbol),
                    sic: None,
                    sic_industry: None,
                };
                (row, extras)
            })
            .collect();
        let table = classification_table_with_extras(records)?;
        let path = reference_partition_path(
            snapshot.snapshot_date,
            DataType::Classification,
            CLASSIFICATION_OBJECT_NAME,
        )?;
        let staged = stage(&table, &path)?;
        let coverage = vec![CoverageRecord::new(
            AssetClass::Reference,
            DataType::Classification,
            CLASSIFICATION_COVERAGE_SYMBOL,
            snapshot.snapshot_date,
            if staged.metadata.rows == 0 {
                CoverageStatus::Empty
            } else {
                CoverageStatus::Complete
            },
            Some(staged.metadata.path.clone()),
            staged.metadata.rows,
            staged.metadata.bytes,
            Some(staged.metadata.checksum.clone()),
            None,
            None,
        )?];
        self.committer
            .commit(std::slice::from_ref(&staged), &coverage)?;
        Ok(ClassificationIngestResult {
            snapshot_date: snapshot.snapshot_date,
            file_creation_time: snapshot.file_creation_time,
            coverage,
            objects: vec![staged.metadata],
        })
    }
}

/// Merge and publish XBRL facts per CIK and `filed` knowledge date.
pub struct FundamentalsIngest {
    committer: ReferenceIngestCommitter,
}

impl FundamentalsIngest {
    /// Build over a committer.
    pub fn new(committer: ReferenceIngestCommitter) -> Self {
        Self { committer }
    }

    /// Merge facts into per-CIK, per-filed objects and commit coverage.
    pub fn run(&self, facts: &[XbrlFact]) -> Result<FundamentalsIngestResult, ReferenceError> {
        let mut grouped: BTreeMap<(u64, Date), Vec<XbrlFact>> = BTreeMap::new();
        for fact in facts {
            grouped
                .entry((fact.cik, fact.filed))
                .or_default()
                .push(fact.clone());
        }

        let mut objects: Vec<StagedReferenceObject> = Vec::new();
        let mut coverage: Vec<CoverageRecord> = Vec::new();
        for ((cik, filed_date), group) in &grouped {
            let path = reference_partition_path(
                *filed_date,
                DataType::Fundamentals,
                &cik_object_name(*cik)?,
            )?;
            let existing = self.existing_rows(&path)?;
            let rows = merged_rows(existing, group.iter().map(FactRow::from_fact));
            let table = fundamentals_table(rows)?;
            let staged = stage(&table, &path)?;
            objects.push(staged.clone());
            coverage.push(CoverageRecord::new(
                AssetClass::Reference,
                DataType::Fundamentals,
                cik_symbol(*cik)?,
                *filed_date,
                CoverageStatus::Complete,
                Some(staged.metadata.path.clone()),
                staged.metadata.rows,
                staged.metadata.bytes,
                Some(staged.metadata.checksum.clone()),
                None,
                None,
            )?);
        }
        self.committer.commit(&objects, &coverage)?;
        Ok(FundamentalsIngestResult {
            ciks: grouped
                .keys()
                .map(|(cik, _)| *cik)
                .collect::<std::collections::BTreeSet<u64>>()
                .into_iter()
                .collect(),
            coverage,
            objects: objects.into_iter().map(|staged| staged.metadata).collect(),
        })
    }

    /// Read previously committed rows, tolerating an absent object.
    fn existing_rows(&self, path: &str) -> Result<Vec<FactRow>, ReferenceError> {
        let Some(payload) = self.committer.object_store.read_optional(path)? else {
            return Ok(Vec::new());
        };
        read_fundamentals_rows(&payload, path, "existing fundamentals")
    }
}

/// Keep facts for the selected CIKs and XBRL tags, if filters are given.
pub fn filter_facts(
    facts: &[XbrlFact],
    ciks: Option<&[u64]>,
    tags: Option<&[String]>,
) -> Vec<XbrlFact> {
    facts
        .iter()
        .filter(|fact| {
            ciks.is_none_or(|selected| selected.contains(&fact.cik))
                && tags.is_none_or(|selected| selected.iter().any(|tag| tag == &fact.tag))
        })
        .cloned()
        .collect()
}

/// Serialize one table and return its staged, deterministic payload.
///
/// The payload mirrors the Python `_stage` writer settings: zstd compression,
/// Parquet 2.6 layout, and the pinned `lake.schema` metadata keys.
pub fn stage(batch: &RecordBatch, path: &str) -> Result<StagedReferenceObject, ReferenceError> {
    let schema = batch.schema();
    let schema_metadata = schema.metadata();
    let name = schema_metadata
        .get(lake_contracts::SCHEMA_METADATA_KEY)
        .cloned()
        .unwrap_or_default();
    let version = schema_metadata
        .get(lake_contracts::SCHEMA_VERSION_METADATA_KEY)
        .cloned()
        .unwrap_or_default();
    let properties = WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .set_writer_version(WriterVersion::PARQUET_2_0)
        .set_key_value_metadata(Some(vec![
            KeyValue::new(lake_contracts::SCHEMA_METADATA_KEY.to_string(), name),
            KeyValue::new(
                lake_contracts::SCHEMA_VERSION_METADATA_KEY.to_string(),
                version,
            ),
        ]))
        .build();
    let mut payload: Vec<u8> = Vec::new();
    {
        let mut writer = ArrowWriter::try_new(&mut payload, schema, Some(properties))
            .map_err(|error| ReferenceError::invalid(error.to_string()))?;
        writer
            .write(batch)
            .map_err(|error| ReferenceError::invalid(error.to_string()))?;
        writer
            .close()
            .map_err(|error| ReferenceError::invalid(error.to_string()))?;
    }
    let checksum = hex::encode(Sha256::digest(&payload));
    let metadata = ParquetMetadata {
        path: path.to_string(),
        rows: batch.num_rows() as u64,
        bytes: payload.len() as u64,
        checksum,
    };
    Ok(StagedReferenceObject { metadata, payload })
}

/// Collapse exact-duplicate rows while preserving partition order.
pub(crate) fn merged_rows(
    existing: Vec<FactRow>,
    facts: impl IntoIterator<Item = FactRow>,
) -> Vec<FactRow> {
    let mut rows = existing;
    rows.extend(facts);
    rows.sort();
    rows.dedup();
    rows
}

/// Fetch the current symbol directory and the SEC ticker map, then publish the
/// joined classification snapshot partition.
///
/// Both requests go through the caller's fair-access client, so the SEC
/// User-Agent and rate limit apply to the ticker map exactly as they do to
/// every other reference fetch.
pub async fn ingest_classification(
    client: &dyn ReferenceFetchClient,
    settings: &ReferenceSettings,
    object_store: Arc<dyn ReferenceObjectStore>,
    catalog: Arc<dyn ReferenceCoverageSink>,
) -> Result<ClassificationSummary, ReferenceError> {
    let payload = client.get_bytes(&settings.classification_url).await?;
    let snapshot = crate::nasdaq::parse_nasdaq_traded(&payload)?;
    let tickers_payload = client.get_bytes(COMPANY_TICKERS_URL).await?;
    let tickers = CompanyTickers::from_bytes(&tickers_payload)?;
    let result = ClassificationIngest::new(ReferenceIngestCommitter::new(object_store, catalog))
        .run(&snapshot, &tickers)?;
    Ok(classification_summary(&result))
}

/// Fetch per-CIK companyfacts documents and merge their facts.
pub async fn ingest_fundamentals_increment(
    client: &dyn ReferenceFetchClient,
    settings: &ReferenceSettings,
    ciks: &[u64],
    tags: Option<&[String]>,
    object_store: Arc<dyn ReferenceObjectStore>,
    catalog: Arc<dyn ReferenceCoverageSink>,
) -> Result<FundamentalsSummary, ReferenceError> {
    let mut facts: Vec<XbrlFact> = Vec::new();
    for cik in ciks {
        let url = company_facts_url(&settings.company_facts_url_template, *cik)?;
        let document = client.get_json(&url).await?;
        facts.extend(parse_companyfacts(&document)?);
    }
    let result = FundamentalsIngest::new(ReferenceIngestCommitter::new(object_store, catalog))
        .run(&filter_facts(&facts, None, tags))?;
    Ok(fundamentals_summary(&result))
}

/// A coverage row as rendered into ingest summaries.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct CoverageSummaryRow {
    /// Rows written for the partition.
    pub rows: u64,
    /// The ISO knowledge date.
    pub session_date: String,
    /// The coverage status value.
    pub status: String,
    /// The catalog symbol.
    pub symbol: String,
}

/// An object as rendered into ingest summaries.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct ObjectSummary {
    /// The payload size in bytes.
    pub bytes: u64,
    /// The sha256 hex digest of the payload.
    pub checksum: String,
    /// The relative object path.
    pub path: String,
    /// The number of rows in the object.
    pub rows: u64,
}

/// Published summary of one classification ingest.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct ClassificationSummary {
    /// The published coverage rows.
    pub coverage: Vec<CoverageSummaryRow>,
    /// The file creation time, rendered like Python's aware `isoformat()`.
    pub file_creation_time: String,
    /// The published objects.
    pub objects: Vec<ObjectSummary>,
    /// The ISO snapshot knowledge date.
    pub snapshot_date: String,
}

/// Published summary of one fundamentals ingest batch.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct FundamentalsSummary {
    /// The sorted unique CIKs.
    pub ciks: Vec<u64>,
    /// The published coverage rows.
    pub coverage: Vec<CoverageSummaryRow>,
    /// The published objects.
    pub objects: Vec<ObjectSummary>,
}

fn coverage_summary(coverage: &[CoverageRecord]) -> Vec<CoverageSummaryRow> {
    coverage
        .iter()
        .map(|record| CoverageSummaryRow {
            rows: record.row_count,
            session_date: record.session_date.isoformat(),
            status: record.status.value().to_string(),
            symbol: record.symbol.clone(),
        })
        .collect()
}

fn object_summary(objects: &[ParquetMetadata]) -> Vec<ObjectSummary> {
    objects
        .iter()
        .map(|item| ObjectSummary {
            bytes: item.bytes,
            checksum: item.checksum.clone(),
            path: item.path.clone(),
            rows: item.rows,
        })
        .collect()
}

fn classification_summary(result: &ClassificationIngestResult) -> ClassificationSummary {
    ClassificationSummary {
        coverage: coverage_summary(&result.coverage),
        file_creation_time: result.file_creation_time.isoformat(),
        objects: object_summary(&result.objects),
        snapshot_date: result.snapshot_date.isoformat(),
    }
}

fn fundamentals_summary(result: &FundamentalsIngestResult) -> FundamentalsSummary {
    FundamentalsSummary {
        ciks: result.ciks.clone(),
        coverage: coverage_summary(&result.coverage),
        objects: object_summary(&result.objects),
    }
}
