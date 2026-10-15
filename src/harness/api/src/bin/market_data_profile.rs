//! Operator benchmark: one closed symbol-session, durable acquisition and verified cold/warm reads.
use clap::Parser;
use harness_lakeclient::{EnsureSpec, LakeClient, LakeClientConfig, ReadSpec};
use std::{sync::Arc, time::Instant};

#[derive(Parser)]
struct Args {
    #[arg(long)]
    endpoint: Option<String>,
    #[arg(long, default_value = "MSFT")]
    symbol: String,
    #[arg(long)]
    session: String,
    #[arg(long)]
    output: Option<std::path::PathBuf>,
    #[arg(long)]
    no_ensure: bool,
}
#[tokio::main]
async fn main() {
    if let Err(error) = run(Args::parse()).await {
        eprintln!("market-data profile failed: {error}");
        std::process::exit(1)
    }
}
async fn run(args: Args) -> Result<(), Box<dyn std::error::Error>> {
    let env: std::collections::BTreeMap<String, String> = std::env::vars().collect();
    let client = if let Some(endpoint) = &args.endpoint {
        LakeClient::new(LakeClientConfig::new(
            endpoint,
            env.get("LAKE_API__BEARER_TOKEN")
                .ok_or("lake bearer is required")?,
        )?)?
    } else {
        let host = lake_hosted::api::create_host(
            &env,
            Arc::new(|| {
                lake_contracts::DateTime::from_micros(
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .expect("system clock")
                        .as_micros()
                        .min(i64::MAX as u128) as i64,
                )
            }),
            "http://127.0.0.1:8001/market-data",
        )
        .map_err(|e| e.to_string())?;
        LakeClient::from_access(host.access)?
    };
    let beginning = Instant::now();
    let mut acquisitions = Vec::new();
    for data in ["trade", "quote", "bars"] {
        let mut spec = EnsureSpec::new("equity", data, &args.symbol, &args.session, &args.session);
        if data == "bars" {
            spec.interval = Some("1min".into());
        }
        let client = client.clone();
        let no_ensure = args.no_ensure;
        acquisitions.push(async move {
            let start = Instant::now();
            let mut job_id = None;
            let mut admission_seconds = 0.0;
            if !no_ensure {
                let admitted = client.admit_ensure(&spec).await?;
                admission_seconds = start.elapsed().as_secs_f64();
                job_id = admitted.job_id;
                client.ensure(&spec).await?;
            }
            Ok::<_, harness_lakeclient::LakeClientError>((
                spec,
                start.elapsed().as_secs_f64(),
                admission_seconds,
                job_id,
            ))
        });
    }
    let acquired = futures_util::future::try_join_all(acquisitions).await?;
    let ingestion_seconds = beginning.elapsed().as_secs_f64();
    let mut results = Vec::new();
    for (spec, ensure_seconds, admission_seconds, job_id) in acquired {
        let read = ReadSpec {
            asset_class: spec.asset_class.clone(),
            data_type: spec.data_type.clone(),
            symbol: spec.symbol.clone(),
            start_date: spec.start_date.clone(),
            end_date: spec.end_date.clone(),
            interval: spec.interval.clone(),
        };
        let mut reads = Vec::new();
        for _ in 0..3 {
            let start = Instant::now();
            let mut rows = 0u64;
            client
                .visit_arrow(&read, |batch| {
                    rows += batch.num_rows() as u64;
                    Ok(())
                })
                .await?;
            reads.push(serde_json::json!({"seconds":start.elapsed().as_secs_f64(),"rows":rows}));
        }
        let snapshot = client.snapshot(&read).await?;
        results.push(serde_json::json!({"data_type":spec.data_type,"ensure_seconds":ensure_seconds,"admission_seconds":admission_seconds,"job_id":job_id,"reads":reads,"snapshot":snapshot}));
    }
    let output = serde_json::json!({"symbol":args.symbol,"session":args.session,"transport":if args.endpoint.is_some(){"remote_http"}else{"in_process"},"ingestion_seconds":ingestion_seconds,"datasets":results});
    let rendered = serde_json::to_string_pretty(&output)?;
    println!("{rendered}");
    if let Some(path) = args.output {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, rendered)?;
    }
    Ok(())
}
