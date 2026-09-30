//! Temporary live probe: fetch a real Alpaca window through the production
//! client and sample the adaptive lane while it runs.

use std::time::{Duration, Instant};

use lake_contracts::DateTime;
use lake_providers::{AlpacaClient, ProviderConfig, RequestGovernor};

#[tokio::main]
async fn main() {
    let api_key = std::env::var("LAKE_PROVIDER__API_KEY").ok();
    let api_secret = std::env::var("LAKE_PROVIDER__API_SECRET").ok();
    let rpm: Option<u32> = std::env::var("LANE_PROBE_RPM")
        .ok()
        .and_then(|value| value.parse().ok());
    let mut config = ProviderConfig::new("https://data.alpaca.markets", api_key, api_secret);
    config.max_concurrency = 512;
    config.requests_per_minute = rpm;
    let client =
        AlpacaClient::with_reqwest(config, RequestGovernor::new(rpm)).expect("client builds");
    let lane = client.lane().clone();

    let start = DateTime::parse_iso_utc("2025-10-06T13:30:00Z").expect("start");
    let end = DateTime::parse_iso_utc("2025-10-06T14:00:00Z").expect("end");

    let monitor = tokio::spawn({
        let lane = lane.clone();
        async move {
            let began = Instant::now();
            loop {
                tokio::time::sleep(Duration::from_secs(2)).await;
                println!(
                    "t={:>6.1}s bound={} in_flight={}",
                    began.elapsed().as_secs_f64(),
                    lane.bound(),
                    lane.in_flight()
                );
            }
        }
    });

    let began = Instant::now();
    let quotes = client
        .fetch_quotes(&["IWM"], start, end)
        .await
        .expect("fetch succeeds");
    let elapsed = began.elapsed();
    monitor.abort();
    println!(
        "rows={} elapsed={:.2}s rows_per_s={:.0}",
        quotes.len(),
        elapsed.as_secs_f64(),
        quotes.len() as f64 / elapsed.as_secs_f64()
    );
}
