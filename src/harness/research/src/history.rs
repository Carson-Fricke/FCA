//! Owns replay scratch allocation and telemetry cadence at the application boundary.
use backtest::backtest::history::HistoryResources;
use harness_contracts::progress::ProgressReporter;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
pub fn resources() -> Result<HistoryResources, String> {
    let cap = std::env::var("HARNESS_HISTORY_SCRATCH_BYTES")
        .ok()
        .map(|v| v.parse::<u64>().map_err(|e| e.to_string()))
        .transpose()?
        .unwrap_or(2 * 1024 * 1024 * 1024);
    let root = std::env::temp_dir();
    Ok(HistoryResources::new(
        cap,
        Arc::new(move || tempfile::NamedTempFile::new_in(&root)),
    ))
}
pub fn paced(reporter: Option<ProgressReporter>) -> Option<ProgressReporter> {
    reporter.map(|reporter| {
        let last = Mutex::new(None::<Instant>);
        Arc::new(
            move |progress: harness_contracts::progress::ExecutionProgress| {
                let mut last = last.lock().map_err(|e| e.to_string())?;
                if !progress.completed
                    && last.is_some_and(|time| time.elapsed() < Duration::from_secs(1))
                {
                    return Ok(());
                }
                reporter(progress)?;
                *last = Some(Instant::now());
                Ok(())
            },
        ) as ProgressReporter
    })
}
