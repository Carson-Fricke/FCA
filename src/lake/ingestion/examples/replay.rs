//! Offline, reproducible loopback-provider -> normalized records -> Parquet ->
//! fenced local publication benchmark. No credentials or external services.
use axum::{extract::Query, routing::get, Json, Router};
use lake_catalog::{CatalogRepository, PublicationCatalog};
use lake_contracts::{parse_iso_nanos, AssetClass, DataType, Date, DateTime};
use lake_coverage::CoverageSession;
use lake_ingestion::{EquityIngestTransaction, ObjectCatalogCommitter, StagingPartitionWriter};
use lake_providers::{AlpacaClient, ProviderConfig, RequestGovernor};
use serde_json::{json, Value};
use std::{collections::BTreeMap, sync::Arc, time::Instant};

fn page(
    params: BTreeMap<String, String>,
    quote: bool,
    rows: usize,
    session: CoverageSession,
) -> Value {
    let lower = parse_iso_nanos(&params["start"]).unwrap();
    let upper = parse_iso_nanos(&params["end"]).unwrap();
    let step = (session.close_time.micros() - session.open_time.micros()) / rows as i64;
    let nanos = step * 1000;
    let open = session.open_time.to_nanos_i64();
    let first = params
        .get("page_token")
        .map(|s| s.parse::<usize>().unwrap())
        .unwrap_or_else(|| ((lower - open).max(0) + nanos - 1).div_euclid(nanos) as usize)
        .min(rows);
    let last = ((upper - open).div_euclid(nanos) + 1).max(0) as usize;
    let last = last.min(rows);
    let end = (first + 10_000).min(last).max(first);
    let values: Vec<Value> = (first..end).map(|index| {
        let timestamp = DateTime::from_micros(session.open_time.micros() + index as i64 * step).isoformat_micros_utc();
        if quote { json!({"t":timestamp,"bx":"Q","bp":199.99,"bs":100,"ax":"P","ap":200.01,"as":100,"c":["R"],"z":"C"}) }
        else { json!({"t":timestamp,"i":index,"x":"Q","p":200.0,"s":100,"c":["@"],"z":"C"}) }
    }).collect();
    let key = if quote { "quotes" } else { "trades" };
    json!({key:{"MSFT":values},"next_page_token":if end < last {Some(end.to_string())} else {None}})
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    lake_pipeline::Pipeline::initialize(lake_pipeline::Limits::from_environment(
        &std::env::vars().collect(),
    )?)?;
    let rows = std::env::args()
        .nth(1)
        .map(|s| s.parse::<usize>())
        .transpose()?
        .unwrap_or(1_000_000);
    let repeats = std::env::args()
        .nth(2)
        .map(|s| s.parse::<usize>())
        .transpose()?
        .unwrap_or(3);
    if rows == 0 || rows > 10_000_000 || repeats == 0 {
        return Err("rows must be 1..10000000 and repeats positive".into());
    }
    let date = Date::parse("2025-12-30").unwrap();
    let session = CoverageSession {
        session_date: date,
        open_time: DateTime::parse_iso_utc("2025-12-30T14:30:00Z").unwrap(),
        close_time: DateTime::parse_iso_utc("2025-12-30T21:00:00Z").unwrap(),
        is_partial: false,
    };
    let router = Router::new()
        .route(
            "/v2/stocks/trades",
            get(
                move |Query(params): Query<BTreeMap<String, String>>| async move {
                    Json(page(params, false, rows, session))
                },
            ),
        )
        .route(
            "/v2/stocks/quotes",
            get(
                move |Query(params): Query<BTreeMap<String, String>>| async move {
                    Json(page(params, true, rows, session))
                },
            ),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let base = format!("http://{}", listener.local_addr()?);
    let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let mut config = ProviderConfig::new(base, None, None);
    config.max_concurrency = 64;
    let client = Arc::new(AlpacaClient::with_reqwest(
        config,
        RequestGovernor::new(None),
    )?);
    let directory = tempfile::tempdir()?;
    let objects = Arc::new(lake_storage::LocalFilesystemStore::new(directory.path()));
    let catalog = Arc::new(PublicationCatalog::new(
        objects.clone(),
        Arc::new(lake_local::FileLeaseProvider(directory.path().into())),
    ));
    let pipeline = lake_pipeline::Pipeline::global();
    for iteration in 0..repeats {
        let transaction = EquityIngestTransaction::new(
            client.clone(),
            Arc::new(StagingPartitionWriter::new()),
            Arc::new(ObjectCatalogCommitter::new(
                objects.clone(),
                catalog.clone(),
            )),
        );
        let started = Instant::now();
        let result = transaction.run("MSFT", &session, None).await?;
        let seconds = started.elapsed().as_secs_f64();
        for data in [DataType::Trade, DataType::Quote] {
            let record = catalog
                .get(AssetClass::Equity, data, None, "MSFT", date)?
                .unwrap();
            if record.row_count != rows as u64 {
                return Err("replay row count mismatch".into());
            }
        }
        println!(
            "{}",
            json!({"benchmark":"loopback_replay", "iteration":iteration,"rows":rows * 2,"seconds":seconds,"rows_per_second":(rows * 2) as f64 / seconds,"parquet_bytes":result.objects.iter().map(|m|m.bytes).sum::<u64>(),"pipeline_cumulative":pipeline.snapshot()})
        );
    }
    server.abort();
    Ok(())
}
