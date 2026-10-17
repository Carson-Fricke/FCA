//! Shared harness for the lake-http integration tests.
//!
//! Every test drives the real axum [`Router`] through `tower::ServiceExt`
//! oneshot with in-memory repositories, so no network or provider is needed.

#![allow(dead_code)]

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use arrow_array::{
    ArrayRef, BooleanArray, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray,
    UInt32Array, UInt64Array,
};
use arrow_schema::{DataType as ArrowType, Field, Schema};
use axum::body::Body;
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use lake_catalog::{
    CatalogRepository, ContractRecord, CoverageRecord, CoverageStatus, MemoryCatalogRepository,
    MemoryJobRepository,
};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_coverage::{EnsureAdmission, EnsureSubmission};
use lake_http::{create_app, ApiClock, LakeApiConfig, LakeApiState};
use lake_query::{EnsureCallback, FileUrlIssuer, QueryApplicationError};
use lake_storage::{BlobStore, LocalFilesystemStore, ParquetMetadata, ParquetWriter};
use serde_json::Value;
use tower::ServiceExt;

/// The static bearer token every test configures.
pub const TOKEN: &str = "test-secret";

/// The pinned as-of instant used to classify open sessions.
pub fn as_of() -> DateTime {
    DateTime::from_ymd_hms(2024, 1, 22, 12, 0, 0, 0).expect("valid datetime")
}

/// A clock provider pinned to one instant.
pub fn clock_at(instant: DateTime) -> ApiClock {
    Arc::new(move || instant)
}

/// A state with the required repositories and test defaults.
pub fn base_state(catalog: Arc<dyn CatalogRepository>) -> LakeApiState {
    LakeApiState {
        catalog,
        jobs: Arc::new(MemoryJobRepository::new()),
        listings: None,
        contracts: None,
        front_sets: None,
        option_resolver: None,
        sas_issuer: None,
        read_store: None,
        ensure_admission: None,
        ensure_callback: None,
        bounded_query: None,
        authorized_force_override: false,
        bearer_token: Some(TOKEN.to_string()),
        entra: None,
        now: clock_at(as_of()),
    }
}

/// Build the router with the standard test configuration.
pub fn build_app(state: LakeApiState) -> Router {
    create_app(
        Arc::new(state),
        LakeApiConfig {
            bearer_token: Some(TOKEN.to_string()),
        },
    )
}

/// Build the router with an explicit configuration.
pub fn build_app_with(state: LakeApiState, config: LakeApiConfig) -> Router {
    create_app(Arc::new(state), config)
}

/// Send one request and return its status, headers, and parsed JSON body.
pub async fn send(router: &Router, request: Request<Body>) -> (StatusCode, HeaderMap, Value) {
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, headers, body)
}

/// Build an authenticated GET request.
pub fn get_auth(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header(header::AUTHORIZATION, format!("Bearer {TOKEN}"))
        .body(Body::empty())
        .expect("request")
}

/// Build an unauthenticated GET request.
pub fn get(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .body(Body::empty())
        .expect("request")
}

/// Build a JSON POST request with an optional bearer credential.
pub fn post_json(path: &str, token: Option<&str>, body: Value) -> Request<Body> {
    let mut builder = Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    builder.body(Body::from(body.to_string())).expect("request")
}

/// Percent-encode one query value, leaving RFC 3986 unreserved bytes.
pub fn encode_query_value(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(char::from(*byte));
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

/// Build a coverage record with canonical metadata for a complete partition.
pub fn complete_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
) -> CoverageRecord {
    let object_path =
        partition_path(session_date, asset_class, data_type, None, symbol).expect("canonical path");
    let status = CoverageStatus::Complete;
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        Some(object_path),
        12,
        3456,
        Some(TEST_CHECKSUM.to_string()),
        None,
        None,
    )
    .expect("valid record")
}

/// Build a coverage record with only a status and no object metadata.
pub fn status_record(
    asset_class: AssetClass,
    data_type: DataType,
    symbol: &str,
    session_date: Date,
    status: CoverageStatus,
) -> CoverageRecord {
    CoverageRecord::new(
        asset_class,
        data_type,
        symbol,
        session_date,
        status,
        None,
        0,
        0,
        None,
        (status == CoverageStatus::Error).then(|| "source unavailable".to_string()),
        None,
    )
    .expect("valid record")
}

/// An issuer recording every path it was asked to sign.
#[derive(Default)]
pub struct RecordingIssuer {
    paths: Mutex<Vec<String>>,
}

impl RecordingIssuer {
    /// The paths issued so far.
    pub fn paths(&self) -> Vec<String> {
        self.paths.lock().expect("lock").clone()
    }
}

impl FileUrlIssuer for RecordingIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        self.paths
            .lock()
            .expect("lock")
            .push(object_path.to_string());
        Ok(format!(
            "https://blob.test/{object_path}?read-only-expiring"
        ))
    }
}

/// An issuer that always fails, standing in for a storage outage.
pub struct FailingIssuer;

impl FileUrlIssuer for FailingIssuer {
    fn issue(&self, _object_path: &str) -> Result<String, DataError> {
        Err(DataError::io("issuer unavailable"))
    }
}

/// An ensure launcher recording every job it received.
#[derive(Clone, Default)]
pub struct RecordingAdmission {
    jobs: Arc<Mutex<Vec<EnsureSubmission>>>,
    pub records: Arc<MemoryJobRepository>,
}

impl RecordingAdmission {
    /// The jobs launched so far.
    pub fn jobs(&self) -> Vec<EnsureSubmission> {
        self.jobs.lock().expect("lock").clone()
    }

    /// The launcher seam itself.
    pub fn launcher(&self) -> EnsureAdmission {
        let jobs = Arc::clone(&self.jobs);
        let records = self.records.clone();
        Arc::new(move |job| {
            let jobs = Arc::clone(&jobs);
            let records = records.clone();
            Box::pin(async move {
                use lake_catalog::JobRepository;
                let mut admitted = jobs.lock().expect("lock");
                if let Some(existing) = admitted
                    .iter()
                    .find(|accepted| accepted.job_id == job.job_id)
                {
                    if existing.requests != job.requests {
                        return Err(DataError::Conflict("ingestion input changed".into()));
                    }
                }
                if records.get(&job.job_id)?.is_none() {
                    records.upsert(&lake_catalog::JobRecord::new(
                        job.job_id.clone(),
                        lake_catalog::JobState::Queued,
                        as_of(),
                        as_of(),
                        None,
                    )?)?;
                    admitted.push(job);
                }
                Ok(())
            })
        })
    }
}

/// A plain contract record for option selection tests.
pub fn contract(symbol: &str, underlying: &str, expiration: Date) -> ContractRecord {
    ContractRecord::new(
        symbol,
        underlying,
        expiration,
        lake_contracts::Decimal18::parse("500").expect("strike"),
        lake_catalog::OptionType::Call,
        100,
        None,
        None,
    )
    .expect("valid contract")
}

/// One recorded ensure callback invocation.
pub type RecordedCall = (AssetClass, DataType, Option<String>, String, Date, Date);

/// An ensure callback recording every request it receives.
#[derive(Clone, Default)]
pub struct RecordingCallback {
    calls: Arc<Mutex<Vec<RecordedCall>>>,
}

impl RecordingCallback {
    /// The callback invocations so far.
    pub fn calls(&self) -> Vec<RecordedCall> {
        self.calls.lock().expect("lock").clone()
    }

    /// The callback seam itself.
    pub fn callback(&self) -> EnsureCallback {
        let calls = Arc::clone(&self.calls);
        Arc::new(
            move |asset_class, data_type, interval, symbol, start_date, end_date| {
                let calls = Arc::clone(&calls);
                Box::pin(async move {
                    calls.lock().expect("lock").push((
                        asset_class,
                        data_type,
                        interval,
                        symbol,
                        start_date,
                        end_date,
                    ));
                    Ok(())
                })
            },
        )
    }
}

/// An ensure callback that fills the catalog and then fails on demand.
pub fn filling_callback(
    catalog: Arc<dyn CatalogRepository>,
    records: Vec<CoverageRecord>,
) -> EnsureCallback {
    Arc::new(move |_, _, _, _, _, _| {
        let catalog = Arc::clone(&catalog);
        let records = records.clone();
        Box::pin(async move {
            for record in records {
                catalog.upsert(&record).map_err(|error| {
                    QueryApplicationError::new("ensure_failed", error.to_string(), None)
                })?;
            }
            Ok(())
        })
    })
}

/// A repository alias used by tests to keep both a concrete and dyn handle.
pub fn shared_catalog(
    records: impl IntoIterator<Item = CoverageRecord>,
) -> Arc<MemoryCatalogRepository> {
    Arc::new(MemoryCatalogRepository::from_records(records))
}

/// A job repository alias.
pub fn shared_jobs(
    jobs: impl IntoIterator<Item = lake_catalog::JobRecord>,
) -> Arc<MemoryJobRepository> {
    Arc::new(MemoryJobRepository::from_records(jobs))
}

/// Send one request and return its status, headers, and raw body bytes.
pub async fn send_bytes(
    router: &Router,
    request: Request<Body>,
) -> (StatusCode, HeaderMap, Vec<u8>) {
    let response = router.clone().oneshot(request).await.expect("oneshot");
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    (status, headers, bytes.to_vec())
}

/// A complete bars coverage record with the canonical interval partition path.
pub fn complete_bars_record(interval: &str, symbol: &str, session_date: Date) -> CoverageRecord {
    let object_path = partition_path(
        session_date,
        AssetClass::Equity,
        DataType::Bars,
        Some(interval),
        symbol,
    )
    .expect("canonical bars path");
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        symbol,
        session_date,
        CoverageStatus::Complete,
        Some(object_path),
        12,
        3456,
        Some(TEST_CHECKSUM.to_string()),
        None,
        Some(interval.to_string()),
    )
    .expect("valid bars record")
}

/// Write one market-data partition with the pinned dataset schema.
pub fn write_market_partition(
    store: &Arc<LocalFilesystemStore>,
    asset_class: AssetClass,
    data_type: DataType,
    interval: Option<&str>,
    symbol: &str,
    session_date: Date,
    records: &[Value],
) -> ParquetMetadata {
    let schema = lake_contracts::schema_for(asset_class, data_type).expect("schema");
    let timestamps = TimestampNanosecondArray::from_iter_values(records.iter().map(|record| {
        DateTime::parse_iso_utc(record["timestamp"].as_str().expect("timestamp"))
            .expect("timestamp")
            .to_nanos()
    }))
    .with_timezone("UTC");
    let decimal = |field: &str| -> ArrayRef {
        Arc::new(
            Decimal128Array::from_iter_values(records.iter().map(|record| {
                Decimal18::parse(record[field].as_str().expect("decimal"))
                    .expect("decimal")
                    .unscaled()
            }))
            .with_precision_and_scale(38, 18)
            .expect("decimal column"),
        )
    };
    let string = |field: &str| -> ArrayRef {
        Arc::new(StringArray::from_iter_values(
            records
                .iter()
                .map(|record| record[field].as_str().expect("string")),
        ))
    };
    let mut columns: Vec<ArrayRef> = vec![
        Arc::new(timestamps),
        Arc::new(StringArray::from_iter_values(std::iter::repeat_n(
            symbol,
            records.len(),
        ))),
    ];
    match (asset_class, data_type) {
        (_, DataType::Bars) => {
            columns.extend(["open", "high", "low", "close", "volume"].map(decimal));
        }
        (AssetClass::Equity, DataType::Trade) => {
            let mut conditions =
                arrow_array::builder::ListBuilder::new(arrow_array::builder::StringBuilder::new())
                    .with_field(Arc::new(Field::new("item", ArrowType::Utf8, false)));
            for record in records {
                for value in record["conditions"].as_array().expect("conditions") {
                    conditions
                        .values()
                        .append_value(value.as_str().expect("condition"));
                }
                conditions.append(true);
            }
            columns.extend([
                Arc::new(UInt64Array::from_iter_values(
                    records
                        .iter()
                        .map(|record| record["trade_id"].as_u64().expect("trade id")),
                )) as ArrayRef,
                string("exchange"),
                decimal("price"),
                Arc::new(UInt32Array::from_iter_values(records.iter().map(
                    |record| u32::try_from(record["size"].as_u64().expect("size")).expect("size"),
                ))),
                Arc::new(conditions.finish()),
                string("tape"),
                Arc::new(StringArray::from_iter(
                    records.iter().map(|record| record["update"].as_str()),
                )),
            ]);
        }
        _ => panic!("unsupported test fixture dataset"),
    }
    let batch = RecordBatch::try_new(Arc::new(schema), columns).expect("fixture batch");
    ParquetWriter::new(Arc::clone(store))
        .write(
            &batch,
            session_date,
            asset_class,
            data_type,
            interval,
            symbol,
        )
        .expect("market partition write")
}

/// One recorded classification snapshot row used by the reference fixtures.
pub struct SnapshotRow {
    /// Canonical symbol.
    pub symbol: &'static str,
    /// Full security name.
    pub security_name: &'static str,
    /// Listing exchange code.
    pub listing_exchange: &'static str,
    /// Market category code.
    pub market_category: &'static str,
    /// Whether the security is an ETF.
    pub etf: bool,
    /// Round lot size at lake scale.
    pub round_lot_size: &'static str,
    /// Financial status code.
    pub financial_status: Option<&'static str>,
    /// The optional symbol-to-CIK mapping column.
    pub cik: Option<u64>,
    /// The optional SIC code column.
    pub sic: Option<&'static str>,
    /// The optional SIC industry column.
    pub sic_industry: Option<&'static str>,
}

/// Write one classification snapshot with the optional reference extras.
pub fn write_classification_snapshot(
    store: &Arc<LocalFilesystemStore>,
    snapshot_date: Date,
    rows: &[SnapshotRow],
) -> String {
    let pinned = lake_reference::classification_schema();
    let mut fields: Vec<Field> = pinned
        .fields()
        .iter()
        .map(|field| field.as_ref().clone())
        .collect();
    fields.push(Field::new("cik", ArrowType::UInt64, true));
    fields.push(Field::new("sic", ArrowType::Utf8, true));
    fields.push(Field::new("sic_industry", ArrowType::Utf8, true));
    let schema = Schema::new_with_metadata(fields, pinned.metadata().clone());

    let created = DateTime::midnight(snapshot_date);
    let file_creation_time =
        TimestampNanosecondArray::from(vec![created.to_nanos(); rows.len()]).with_timezone("UTC");
    let symbol = StringArray::from(rows.iter().map(|row| row.symbol).collect::<Vec<_>>());
    let security_name =
        StringArray::from(rows.iter().map(|row| row.security_name).collect::<Vec<_>>());
    let listing_exchange = StringArray::from(
        rows.iter()
            .map(|row| row.listing_exchange)
            .collect::<Vec<_>>(),
    );
    let market_category = StringArray::from(
        rows.iter()
            .map(|row| row.market_category)
            .collect::<Vec<_>>(),
    );
    let etf = BooleanArray::from(rows.iter().map(|row| row.etf).collect::<Vec<_>>());
    let round_lot_size = Decimal128Array::from(
        rows.iter()
            .map(|row| {
                Decimal18::parse(row.round_lot_size)
                    .expect("round lot")
                    .unscaled()
            })
            .collect::<Vec<_>>(),
    )
    .with_precision_and_scale(38, 18)
    .expect("decimal array");
    let test_issue = BooleanArray::from(vec![false; rows.len()]);
    let financial_status = StringArray::from(
        rows.iter()
            .map(|row| row.financial_status.map(str::to_string))
            .collect::<Vec<_>>(),
    );
    let cik = UInt64Array::from(rows.iter().map(|row| row.cik).collect::<Vec<_>>());
    let sic = StringArray::from(
        rows.iter()
            .map(|row| row.sic.map(str::to_string))
            .collect::<Vec<_>>(),
    );
    let sic_industry = StringArray::from(
        rows.iter()
            .map(|row| row.sic_industry.map(str::to_string))
            .collect::<Vec<_>>(),
    );
    let batch = RecordBatch::try_new(
        Arc::new(schema),
        vec![
            Arc::new(file_creation_time),
            Arc::new(symbol),
            Arc::new(security_name),
            Arc::new(listing_exchange),
            Arc::new(market_category),
            Arc::new(etf),
            Arc::new(round_lot_size),
            Arc::new(test_issue),
            Arc::new(financial_status),
            Arc::new(cik),
            Arc::new(sic),
            Arc::new(sic_industry),
        ],
    )
    .expect("classification batch");

    let path = lake_reference::reference_partition_path(
        snapshot_date,
        DataType::Classification,
        lake_reference::CLASSIFICATION_OBJECT_NAME,
    )
    .expect("classification path");
    let staged = lake_reference::ingest::stage(&batch, &path).expect("stage classification");
    BlobStore::replace(store.as_ref(), &path, &staged.payload).expect("write classification");
    path
}

/// Build one fundamentals fact row for a recorded fixture.
#[allow(clippy::too_many_arguments)]
pub fn fact(
    cik: u64,
    taxonomy: &str,
    tag: &str,
    unit: &str,
    period_end: Date,
    filed: Date,
    accn: &str,
    val: &str,
) -> lake_reference::FactRow {
    lake_reference::FactRow {
        cik,
        taxonomy: taxonomy.to_string(),
        tag: tag.to_string(),
        unit: unit.to_string(),
        period_end,
        fy: None,
        fp: None,
        form: "10-Q".to_string(),
        accn: accn.to_string(),
        filed,
        frame: None,
        val: Decimal18::parse(val).expect("fact value"),
    }
}

/// Write facts into per-CIK, per-filed fundamentals objects.
pub fn write_fundamentals(
    store: &Arc<LocalFilesystemStore>,
    rows: Vec<lake_reference::FactRow>,
) -> Vec<String> {
    let mut grouped: BTreeMap<(u64, Date), Vec<lake_reference::FactRow>> = BTreeMap::new();
    for row in rows {
        grouped.entry((row.cik, row.filed)).or_default().push(row);
    }
    let mut paths = Vec::new();
    for ((cik, filed), group) in grouped {
        let path = lake_reference::reference_partition_path(
            filed,
            DataType::Fundamentals,
            &lake_reference::cik_object_name(cik).expect("cik object name"),
        )
        .expect("fundamentals path");
        let table = lake_reference::fundamentals_table(group).expect("fundamentals table");
        let staged = lake_reference::ingest::stage(&table, &path).expect("stage fundamentals");
        BlobStore::replace(store.as_ref(), &path, &staged.payload).expect("write fundamentals");
        paths.push(path);
    }
    paths
}

pub const TEST_CHECKSUM: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
pub fn partition_path(
    date: Date,
    asset: AssetClass,
    data: DataType,
    interval: Option<&str>,
    symbol: &str,
) -> Result<String, DataError> {
    lake_contracts::partition_path(date, asset, data, interval, symbol)
        .and_then(|logical| lake_contracts::immutable_path(&logical, TEST_CHECKSUM))
}
pub fn with_metadata(mut record: CoverageRecord, metadata: &ParquetMetadata) -> CoverageRecord {
    record.object_path = Some(metadata.path.clone());
    record.checksum = Some(metadata.checksum.clone());
    record.byte_size = metadata.bytes;
    record.row_count = metadata.rows;
    record
}
