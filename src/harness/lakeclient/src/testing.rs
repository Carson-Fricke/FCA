//! Test support: a local fake of the current market-data routes.
//!
//! Enabled by the `test-support` feature. The server binds loopback, requires
//! the configured bearer token, records every request (method, path, decoded
//! query, authorization header, body), and serves programmable JSON and canonical Parquet bodies. No production services are contacted.

use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use arrow_array::builder::{ListBuilder, StringBuilder};
use arrow_array::{
    ArrayRef, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray, UInt32Array,
    UInt64Array,
};
use arrow_schema::{DataType as ArrowType, Field, Schema, TimeUnit};
use axum::body::{Body, Bytes};
use axum::extract::{Path, Query, State};
use axum::http::header::{AUTHORIZATION, CONTENT_TYPE};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

/// One request the fake lake observed.
#[derive(Clone, Debug, PartialEq)]
pub struct RecordedRequest {
    /// HTTP method.
    pub method: String,
    /// Request path without the query string.
    pub path: String,
    /// Decoded query parameters.
    pub query: BTreeMap<String, String>,
    /// The `Authorization` header, when present.
    pub authorization: Option<String>,
    /// Raw request body, when present.
    pub body: Option<Vec<u8>>,
}

/// Programmable fake-lake responses.
///
/// The request handlers pop one response per call from each sequence and
/// repeat the last entry once the sequence is exhausted.
#[derive(Clone, Debug)]
pub struct FakeLakeConfig {
    /// The bearer token every request must carry.
    pub expected_token: String,
    /// `POST /v1/catalog/ensure` responses as `(status, body)`.
    pub ensure_bodies: Vec<(u16, Value)>,
    /// `GET /v1/jobs/{id}` responses as `(status, body)`.
    pub job_bodies: Vec<(u16, Value)>,
    /// `GET /v1/catalog/coverage` responses as `(status, body)`.
    pub coverage_bodies: Vec<(u16, Value)>,
    /// Snapshot partition responses as `(status, body)`.
    pub partition_bodies: Vec<(u16, Vec<u8>)>,
    /// Partition bodies keyed by `data_type`; preferred over the sequence.
    pub partitions_by_type: BTreeMap<String, Vec<u8>>,
    /// `GET /v1/reference/universe` response body.
    pub universe_body: Value,
    /// `GET /v1/reference/facts` response body.
    pub facts_body: Value,
    /// Individual option selection response.
    pub option_resolution: Option<Value>,
}

impl FakeLakeConfig {
    /// Build a configuration with one accepted, covered window per route.
    #[must_use]
    pub fn new(expected_token: impl Into<String>) -> Self {
        Self {
            expected_token: expected_token.into(),
            ensure_bodies: vec![(200, json!({"job_id": "fake-job", "state": "running"}))],
            job_bodies: vec![(200, json!({"job_id": "fake-job", "state": "complete"}))],
            coverage_bodies: vec![(
                200,
                json!({
                    "asset_class": "equity",
                    "data_type": "trade",
                    "symbol": "SPY",
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-02",
                    "entries": [{
                        "session_date": "2024-01-02",
                        "is_partial": false,
                        "status": "complete"
                    }]
                }),
            )],
            partition_bodies: vec![(404, no_data_body())],
            partitions_by_type: BTreeMap::new(),
            universe_body: json!({"as_of": "2024-01-02", "symbols": []}),
            facts_body: json!({"as_of": "2024-01-02", "facts": {}}),
            option_resolution: None,
        }
    }
}

/// The `404 no_data` error envelope the read route publishes.
#[must_use]
pub fn no_data_body() -> Vec<u8> {
    serde_json::to_vec(&json!({
        "error": {"code": "no_data", "message": "no ensured data for the window"}
    }))
    .expect("static no_data body")
}

/// A local fake market-lake server running on loopback.
pub struct FakeLakeServer {
    base_url: String,
    state: Arc<FakeState>,
    runtime: Option<tokio::runtime::Runtime>,
    join: Option<tokio::task::JoinHandle<()>>,
}

impl FakeLakeServer {
    /// Start the fake server and wait for it to accept connections.
    #[must_use]
    pub fn start(config: FakeLakeConfig) -> Self {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .expect("fake lake runtime");
        let state = Arc::new(FakeState::new(config));
        let app = router(Arc::clone(&state));
        let (ready_tx, ready_rx) = std::sync::mpsc::channel::<String>();
        let join = runtime.spawn(async move {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("fake lake bind");
            let addr = listener.local_addr().expect("fake lake address");
            ready_tx
                .send(format!("http://{addr}"))
                .expect("ready signal");
            axum::serve(listener, app).await.expect("fake lake serve");
        });
        let base_url = ready_rx.recv().expect("fake lake ready");
        Self {
            base_url,
            state,
            runtime: Some(runtime),
            join: Some(join),
        }
    }

    /// The loopback base URL.
    #[must_use]
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Every request the fake has observed so far.
    #[must_use]
    pub fn requests(&self) -> Vec<RecordedRequest> {
        self.state.requests()
    }

    /// Drop every recorded request.
    pub fn clear_requests(&self) {
        self.state
            .requests
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clear();
    }
}

impl Drop for FakeLakeServer {
    fn drop(&mut self) {
        if let Some(join) = self.join.take() {
            join.abort();
        }
        // `shutdown_background` is safe inside an asynchronous context, where
        // a plain runtime drop would panic; the test process is ending the
        // fake anyway.
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_background();
        }
    }
}

struct FakeState {
    config: FakeLakeConfig,
    requests: Mutex<Vec<RecordedRequest>>,
    ensure_index: AtomicUsize,
    job_index: AtomicUsize,
    coverage_index: AtomicUsize,
    read_index: AtomicUsize,
    objects: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl FakeState {
    fn new(config: FakeLakeConfig) -> Self {
        Self {
            config,
            requests: Mutex::new(Vec::new()),
            ensure_index: AtomicUsize::new(0),
            job_index: AtomicUsize::new(0),
            coverage_index: AtomicUsize::new(0),
            read_index: AtomicUsize::new(0),
            objects: Default::default(),
        }
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.requests
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    fn record(
        &self,
        method: &str,
        path: &str,
        query: BTreeMap<String, String>,
        headers: &HeaderMap,
        body: Option<Vec<u8>>,
    ) {
        let authorization = headers
            .get(AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        self.requests
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(RecordedRequest {
                method: method.to_string(),
                path: path.to_string(),
                query,
                authorization,
                body,
            });
    }

    fn authorized(&self, headers: &HeaderMap) -> bool {
        headers
            .get(AUTHORIZATION)
            .and_then(|value| value.to_str().ok())
            == Some(format!("Bearer {}", self.config.expected_token).as_str())
    }
}

fn router(state: Arc<FakeState>) -> Router {
    Router::new()
        .route("/v1/catalog/ensure", post(ensure))
        .route("/v1/options/resolve", post(resolve_option))
        .route("/v1/jobs/{job_id}", get(job))
        .route("/v1/catalog/coverage", get(coverage))
        .route("/v1/snapshot", get(snapshot))
        .route("/objects/{checksum}", get(object))
        .route("/v1/reference/universe", get(universe))
        .route("/v1/reference/facts", get(facts))
        .with_state(state)
}

fn sequence_item<T: Clone>(items: &[T], index: &AtomicUsize) -> Option<T> {
    if items.is_empty() {
        return None;
    }
    let position = index.fetch_add(1, Ordering::SeqCst).min(items.len() - 1);
    Some(items[position].clone())
}

fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(json!({"error": {"code": "unauthorized", "message": "invalid bearer token"}})),
    )
        .into_response()
}

async fn ensure(State(state): State<Arc<FakeState>>, headers: HeaderMap, body: Bytes) -> Response {
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record(
        "POST",
        "/v1/catalog/ensure",
        BTreeMap::new(),
        &headers,
        Some(body.to_vec()),
    );
    let (status, body) = sequence_item(&state.config.ensure_bodies, &state.ensure_index)
        .unwrap_or_else(|| (200, json!({})));
    json_response(status, body)
}

async fn resolve_option(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    state.record(
        "POST",
        "/v1/options/resolve",
        BTreeMap::new(),
        &headers,
        Some(body.to_vec()),
    );
    if !state.authorized(&headers) {
        return unauthorized();
    }
    match &state.config.option_resolution {
        Some(value) => Json(value.clone()).into_response(),
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"error":{"code":"option_selection_unavailable"}})),
        )
            .into_response(),
    }
}

async fn job(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Path(job_id): Path<String>,
) -> Response {
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record(
        "GET",
        &format!("/v1/jobs/{job_id}"),
        BTreeMap::new(),
        &headers,
        None,
    );
    let (status, body) = sequence_item(&state.config.job_bodies, &state.job_index)
        .unwrap_or_else(|| (200, json!({})));
    json_response(status, body)
}

async fn coverage(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record("GET", "/v1/catalog/coverage", query, &headers, None);
    let (status, body) = sequence_item(&state.config.coverage_bodies, &state.coverage_index)
        .unwrap_or_else(|| (200, json!({})));
    json_response(status, body)
}

async fn snapshot(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    use harness_market_reader::{MarketDataSnapshot, Partition, SnapshotRequest};
    use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
    use sha2::{Digest, Sha256};
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record("GET", "/v1/snapshot", query.clone(), &headers, None);
    let request = SnapshotRequest {
        asset_class: query["asset_class"].clone(),
        data_type: query["data_type"].clone(),
        symbol: query["symbol"].clone(),
        start_date: query["start_date"].clone(),
        end_date: query["end_date"].clone(),
        interval: query.get("interval").cloned(),
    };
    let (status, body) = state
        .config
        .partitions_by_type
        .get(&request.data_type)
        .map(|body| (200, body.clone()))
        .unwrap_or_else(|| {
            sequence_item(&state.config.partition_bodies, &state.read_index)
                .unwrap_or_else(|| (404, no_data_body()))
        });
    let mut files = Vec::new();
    let mut locations = BTreeMap::new();
    if status == 200 {
        let checksum = hex::encode(Sha256::digest(&body));
        let rows = match ParquetRecordBatchReaderBuilder::try_new(bytes::Bytes::from(body.clone()))
        {
            Ok(builder) => builder.metadata().file_metadata().num_rows() as u64,
            Err(_) => return json_response(500, json!({"error":{"code":"invalid_partition"}})),
        };
        let path = lake_contracts::partition_path(
            lake_contracts::Date::parse(&request.start_date).unwrap(),
            lake_contracts::AssetClass::from_value(&request.asset_class).unwrap(),
            lake_contracts::DataType::from_value(&request.data_type).unwrap(),
            request.interval.as_deref(),
            &request.symbol,
        )
        .unwrap();
        files.push(Partition {
            session_date: request.start_date.clone(),
            object_path: path,
            checksum: checksum.clone(),
            row_count: rows,
            byte_size: body.len() as u64,
        });
        state.objects.lock().unwrap().insert(checksum.clone(), body);
        locations.insert(
            checksum.clone(),
            format!(
                "http://{}/objects/{checksum}",
                headers.get("host").unwrap().to_str().unwrap()
            ),
        );
    } else if status != 404 {
        return raw_response(status, body);
    }
    match MarketDataSnapshot::new(request, files) {
        Ok(snapshot) => Json(json!({"snapshot":snapshot,"locations":locations})).into_response(),
        Err(error) => json_response(
            422,
            json!({"error":{"code":"invalid_snapshot","message":error.to_string()}}),
        ),
    }
}

async fn object(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Path(checksum): Path<String>,
) -> Response {
    state.record(
        "GET",
        &format!("/objects/{checksum}"),
        BTreeMap::new(),
        &headers,
        None,
    );
    assert!(
        headers.get(AUTHORIZATION).is_none(),
        "API bearer leaked to object storage"
    );
    let body = state.objects.lock().unwrap().get(&checksum).cloned();
    match body {
        Some(body) => Body::from(body).into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn universe(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record("GET", "/v1/reference/universe", query, &headers, None);
    Json(state.config.universe_body.clone()).into_response()
}

async fn facts(
    State(state): State<Arc<FakeState>>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    if !state.authorized(&headers) {
        return unauthorized();
    }
    state.record("GET", "/v1/reference/facts", query, &headers, None);
    Json(state.config.facts_body.clone()).into_response()
}

fn json_response(status: u16, value: Value) -> Response {
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    (status, Json(value)).into_response()
}

fn raw_response(status: u16, body: Vec<u8>) -> Response {
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
    let content_type = if status == StatusCode::OK {
        "application/vnd.apache.arrow.stream"
    } else {
        "application/json"
    };
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, content_type)
        .body(Body::from(body))
        .expect("fake lake response")
}

/// One synthetic bar row for the fake bars fixtures.
#[derive(Clone, Debug)]
pub struct FakeBarRow {
    /// UTC timestamp in nanoseconds since the Unix epoch.
    pub timestamp_nanos: i64,
    /// Session open (exact decimal literal).
    pub open: String,
    /// Session high (exact decimal literal).
    pub high: String,
    /// Session low (exact decimal literal).
    pub low: String,
    /// Session close (exact decimal literal).
    pub close: String,
    /// Session volume (exact decimal literal).
    pub volume: String,
}

impl FakeBarRow {
    /// Build one fixture bar from exact decimal literals.
    #[must_use]
    pub fn new(
        timestamp_nanos: i64,
        open: &str,
        high: &str,
        low: &str,
        close: &str,
        volume: &str,
    ) -> Self {
        Self {
            timestamp_nanos,
            open: open.to_string(),
            high: high.to_string(),
            low: low.to_string(),
            close: close.to_string(),
            volume: volume.to_string(),
        }
    }
}

/// The frozen `equity.bars` schema (`decimal128(38, 18)` prices).
#[must_use]
pub fn equity_bars_schema() -> Schema {
    let timestamp = || ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let price = || ArrowType::Decimal128(38, 18);
    let mut metadata = HashMap::new();
    metadata.insert("lake.schema".to_string(), "equity.bars".to_string());
    metadata.insert("lake.schema.version".to_string(), "1".to_string());
    Schema::new_with_metadata(
        vec![
            Field::new("timestamp", timestamp(), false),
            Field::new("symbol", ArrowType::Utf8, false),
            Field::new("open", price(), false),
            Field::new("high", price(), false),
            Field::new("low", price(), false),
            Field::new("close", price(), false),
            Field::new("volume", price(), false),
        ],
        metadata,
    )
}

/// Build one `equity.bars` record batch from exact decimal literals.
#[must_use]
pub fn equity_bars_batch(symbol: &str, rows: &[FakeBarRow]) -> RecordBatch {
    let schema = Arc::new(equity_bars_schema());
    let timestamps: Vec<Option<i64>> = rows.iter().map(|row| Some(row.timestamp_nanos)).collect();
    let symbols = StringArray::from(vec![symbol; rows.len()]);
    let decimal = |values: Vec<Option<i128>>| -> ArrayRef {
        Arc::new(
            Decimal128Array::from(values)
                .with_precision_and_scale(38, 18)
                .expect("fixture decimal precision"),
        )
    };
    let arrays: Vec<ArrayRef> = vec![
        Arc::new(TimestampNanosecondArray::from(timestamps).with_timezone("UTC".to_string())),
        Arc::new(symbols),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.open)))
                .collect(),
        ),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.high)))
                .collect(),
        ),
        decimal(rows.iter().map(|row| Some(unscaled_18(&row.low))).collect()),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.close)))
                .collect(),
        ),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.volume)))
                .collect(),
        ),
    ];
    RecordBatch::try_new(schema, arrays).expect("fixture bars batch")
}

/// Encode one `equity.bars` batch as canonical Parquet.
#[must_use]
pub fn bars_parquet(symbol: &str, rows: &[FakeBarRow]) -> Vec<u8> {
    let batch = equity_bars_batch(symbol, rows);
    let schema = batch.schema();
    parquet_bytes(schema.as_ref(), &[batch])
}

/// One synthetic equity quote row for the fake quote stream.
#[derive(Clone, Debug)]
pub struct FakeQuoteRow {
    /// UTC timestamp in nanoseconds since the Unix epoch.
    pub timestamp_nanos: i64,
    /// Bid price (exact decimal literal).
    pub bid_price: String,
    /// Ask price (exact decimal literal).
    pub ask_price: String,
    /// Bid size in shares.
    pub bid_size: u32,
    /// Ask size in shares.
    pub ask_size: u32,
    /// Reporting exchange.
    pub exchange: String,
}

impl FakeQuoteRow {
    /// Build one fixture quote from exact decimal literals.
    #[must_use]
    pub fn new(
        timestamp_nanos: i64,
        bid_price: &str,
        ask_price: &str,
        bid_size: u32,
        ask_size: u32,
        exchange: &str,
    ) -> Self {
        Self {
            timestamp_nanos,
            bid_price: bid_price.to_string(),
            ask_price: ask_price.to_string(),
            bid_size,
            ask_size,
            exchange: exchange.to_string(),
        }
    }
}

/// One synthetic equity trade row for the fake trade stream.
#[derive(Clone, Debug)]
pub struct FakeTradeRow {
    /// UTC timestamp in nanoseconds since the Unix epoch.
    pub timestamp_nanos: i64,
    /// Provider trade identifier.
    pub trade_id: u64,
    /// Trade price (exact decimal literal).
    pub price: String,
    /// Trade size in shares.
    pub size: u32,
    /// Reporting exchange.
    pub exchange: String,
}

impl FakeTradeRow {
    /// Build one fixture trade from exact decimal literals.
    #[must_use]
    pub fn new(
        timestamp_nanos: i64,
        trade_id: u64,
        price: &str,
        size: u32,
        exchange: &str,
    ) -> Self {
        Self {
            timestamp_nanos,
            trade_id,
            price: price.to_string(),
            size,
            exchange: exchange.to_string(),
        }
    }
}

/// The frozen `equity.quote` schema (`decimal128(38, 18)` prices).
#[must_use]
pub fn equity_quote_schema() -> Schema {
    let timestamp = || ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let price = || ArrowType::Decimal128(38, 18);
    let conditions = || ArrowType::List(Arc::new(Field::new("item", ArrowType::Utf8, false)));
    let mut metadata = HashMap::new();
    metadata.insert("lake.schema".to_string(), "equity.quote".to_string());
    metadata.insert("lake.schema.version".to_string(), "1".to_string());
    Schema::new_with_metadata(
        vec![
            Field::new("timestamp", timestamp(), false),
            Field::new("symbol", ArrowType::Utf8, false),
            Field::new("bid_exchange", ArrowType::Utf8, false),
            Field::new("bid_price", price(), false),
            Field::new("bid_size", ArrowType::UInt32, false),
            Field::new("ask_exchange", ArrowType::Utf8, false),
            Field::new("ask_price", price(), false),
            Field::new("ask_size", ArrowType::UInt32, false),
            Field::new("conditions", conditions(), false),
            Field::new("tape", ArrowType::Utf8, false),
        ],
        metadata,
    )
}

/// The frozen `equity.trade` schema (`decimal128(38, 18)` prices).
#[must_use]
pub fn equity_trade_schema() -> Schema {
    let timestamp = || ArrowType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let price = || ArrowType::Decimal128(38, 18);
    let conditions = || ArrowType::List(Arc::new(Field::new("item", ArrowType::Utf8, false)));
    let mut metadata = HashMap::new();
    metadata.insert("lake.schema".to_string(), "equity.trade".to_string());
    metadata.insert("lake.schema.version".to_string(), "1".to_string());
    Schema::new_with_metadata(
        vec![
            Field::new("timestamp", timestamp(), false),
            Field::new("symbol", ArrowType::Utf8, false),
            Field::new("trade_id", ArrowType::UInt64, false),
            Field::new("exchange", ArrowType::Utf8, false),
            Field::new("price", price(), false),
            Field::new("size", ArrowType::UInt32, false),
            Field::new("conditions", conditions(), false),
            Field::new("tape", ArrowType::Utf8, false),
            Field::new("update", ArrowType::Utf8, true),
        ],
        metadata,
    )
}

/// Build one `equity.quote` record batch from exact decimal literals.
#[must_use]
pub fn equity_quote_batch(symbol: &str, rows: &[FakeQuoteRow]) -> RecordBatch {
    let schema = Arc::new(equity_quote_schema());
    let timestamps: Vec<Option<i64>> = rows.iter().map(|row| Some(row.timestamp_nanos)).collect();
    let symbols = StringArray::from(vec![symbol; rows.len()]);
    let exchanges = StringArray::from(
        rows.iter()
            .map(|row| Some(row.exchange.as_str()))
            .collect::<Vec<_>>(),
    );
    let decimal = |values: Vec<Option<i128>>| -> ArrayRef {
        Arc::new(
            Decimal128Array::from(values)
                .with_precision_and_scale(38, 18)
                .expect("fixture decimal precision"),
        )
    };
    let arrays: Vec<ArrayRef> = vec![
        Arc::new(TimestampNanosecondArray::from(timestamps).with_timezone("UTC".to_string())),
        Arc::new(symbols),
        Arc::new(exchanges.clone()),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.bid_price)))
                .collect(),
        ),
        Arc::new(UInt32Array::from(
            rows.iter()
                .map(|row| Some(row.bid_size))
                .collect::<Vec<_>>(),
        )),
        Arc::new(exchanges),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.ask_price)))
                .collect(),
        ),
        Arc::new(UInt32Array::from(
            rows.iter()
                .map(|row| Some(row.ask_size))
                .collect::<Vec<_>>(),
        )),
        Arc::new(empty_string_lists(rows.len())),
        Arc::new(StringArray::from(vec!["C"; rows.len()])),
    ];
    RecordBatch::try_new(schema, arrays).expect("fixture quote batch")
}

/// Build one `equity.trade` record batch from exact decimal literals.
#[must_use]
pub fn equity_trade_batch(symbol: &str, rows: &[FakeTradeRow]) -> RecordBatch {
    let schema = Arc::new(equity_trade_schema());
    let timestamps: Vec<Option<i64>> = rows.iter().map(|row| Some(row.timestamp_nanos)).collect();
    let symbols = StringArray::from(vec![symbol; rows.len()]);
    let exchanges = StringArray::from(
        rows.iter()
            .map(|row| Some(row.exchange.as_str()))
            .collect::<Vec<_>>(),
    );
    let decimal = |values: Vec<Option<i128>>| -> ArrayRef {
        Arc::new(
            Decimal128Array::from(values)
                .with_precision_and_scale(38, 18)
                .expect("fixture decimal precision"),
        )
    };
    let arrays: Vec<ArrayRef> = vec![
        Arc::new(TimestampNanosecondArray::from(timestamps).with_timezone("UTC".to_string())),
        Arc::new(symbols),
        Arc::new(UInt64Array::from(
            rows.iter()
                .map(|row| Some(row.trade_id))
                .collect::<Vec<_>>(),
        )),
        Arc::new(exchanges),
        decimal(
            rows.iter()
                .map(|row| Some(unscaled_18(&row.price)))
                .collect(),
        ),
        Arc::new(UInt32Array::from(
            rows.iter().map(|row| Some(row.size)).collect::<Vec<_>>(),
        )),
        Arc::new(empty_string_lists(rows.len())),
        Arc::new(StringArray::from(vec!["C"; rows.len()])),
        Arc::new(StringArray::from(
            rows.iter().map(|_| None::<&str>).collect::<Vec<_>>(),
        )),
    ];
    RecordBatch::try_new(schema, arrays).expect("fixture trade batch")
}

/// Encode one `equity.quote` batch as canonical Parquet.
#[must_use]
pub fn quote_parquet(symbol: &str, rows: &[FakeQuoteRow]) -> Vec<u8> {
    let batch = equity_quote_batch(symbol, rows);
    let schema = batch.schema();
    parquet_bytes(schema.as_ref(), &[batch])
}

/// Encode one `equity.trade` batch as canonical Parquet.
#[must_use]
pub fn trade_parquet(symbol: &str, rows: &[FakeTradeRow]) -> Vec<u8> {
    let batch = equity_trade_batch(symbol, rows);
    let schema = batch.schema();
    parquet_bytes(schema.as_ref(), &[batch])
}

/// Canonical option fixtures share values with equity fixtures but use the option contract's fields.
fn option_partition(batch: RecordBatch, kind: lake_contracts::DataType) -> Vec<u8> {
    let schema = lake_contracts::schema_for(lake_contracts::AssetClass::Option, kind).unwrap();
    let arrays = schema
        .fields()
        .iter()
        .map(|field| {
            if field.name() == "condition" {
                Arc::new(StringArray::from(vec!["R"; batch.num_rows()])) as ArrayRef
            } else {
                batch.column_by_name(field.name()).unwrap().clone()
            }
        })
        .collect();
    let batch = RecordBatch::try_new(Arc::new(schema), arrays).unwrap();
    parquet_bytes(batch.schema().as_ref(), std::slice::from_ref(&batch))
}
pub fn option_quote_parquet(symbol: &str, rows: &[FakeQuoteRow]) -> Vec<u8> {
    option_partition(
        equity_quote_batch(symbol, rows),
        lake_contracts::DataType::Quote,
    )
}
pub fn option_trade_parquet(symbol: &str, rows: &[FakeTradeRow]) -> Vec<u8> {
    option_partition(
        equity_trade_batch(symbol, rows),
        lake_contracts::DataType::Trade,
    )
}
pub fn option_bars_parquet(symbol: &str, rows: &[FakeBarRow]) -> Vec<u8> {
    option_partition(
        equity_bars_batch(symbol, rows),
        lake_contracts::DataType::Bars,
    )
}

/// Build one empty-list `conditions` column matching the frozen `List<Utf8>`.
fn empty_string_lists(count: usize) -> arrow_array::ListArray {
    let field = Arc::new(Field::new("item", ArrowType::Utf8, false));
    let mut builder = ListBuilder::new(StringBuilder::new()).with_field(field);
    for _ in 0..count {
        builder.append_value(std::iter::empty::<Option<&str>>());
    }
    builder.finish()
}

/// Encode record batches as canonical Parquet.
#[must_use]
pub fn parquet_bytes(schema: &Schema, batches: &[RecordBatch]) -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut writer =
            parquet::arrow::ArrowWriter::try_new(&mut bytes, Arc::new(schema.clone()), None)
                .expect("fixture Parquet writer");
        for batch in batches {
            writer.write(batch).expect("fixture Parquet batch");
        }
        writer.close().expect("fixture Parquet finish");
    }
    bytes
}

/// Parse one exact decimal literal at scale 18 for a fixture column.
fn unscaled_18(text: &str) -> i128 {
    let (negative, digits) = match text.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, text),
    };
    let (whole, fraction) = match digits.split_once('.') {
        Some((whole, fraction)) => (whole, fraction),
        None => (digits, ""),
    };
    assert!(
        whole.len() + fraction.len() <= 38,
        "fixture decimal overflow"
    );
    let mut unscaled: i128 = whole.parse::<i128>().expect("fixture whole part") * 10_i128.pow(18);
    if !fraction.is_empty() {
        assert!(
            fraction.len() <= 18,
            "fixture decimal has more than 18 fractional digits"
        );
        let padded = format!("{fraction:0<18}");
        unscaled += padded.parse::<i128>().expect("fixture fraction part");
    }
    if negative {
        -unscaled
    } else {
        unscaled
    }
}
