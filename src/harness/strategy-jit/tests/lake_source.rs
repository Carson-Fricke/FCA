//! `LakeScreeningSource` behavior over a deterministic fake lake boundary.
//!
//! The fake implements [`ScreeningLakeReads`] directly, so the tests pin the
//! window, ensure, and read specifications and the Arrow-to-[`Bar`] mapping
//! without a server, a network, or lake files.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use arrow_array::RecordBatch;
use harness_lakeclient::testing::{equity_bars_batch, FakeBarRow};
use harness_lakeclient::{EnsureSpec, LakeClientError, ReadSpec};
use harness_strategy_jit::{
    LakeScreeningSource, ScreeningDataSource, ScreeningDataSourceError, ScreeningLakeReads,
};

#[derive(Default)]
struct FakeLake {
    universe: Vec<String>,
    facts: BTreeMap<String, BTreeMap<String, String>>,
    batches: BTreeMap<String, Vec<RecordBatch>>,
    failure: Option<LakeClientError>,
    ensures: Mutex<Vec<EnsureSpec>>,
    reads: Mutex<Vec<ReadSpec>>,
}

impl FakeLake {
    fn check_failure(&self) -> Result<(), LakeClientError> {
        match &self.failure {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}

impl ScreeningLakeReads for FakeLake {
    fn ensure(&self, spec: &EnsureSpec) -> Result<(), LakeClientError> {
        self.check_failure()?;
        self.ensures
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(spec.clone());
        Ok(())
    }

    fn read_arrow(&self, spec: &ReadSpec) -> Result<Vec<RecordBatch>, LakeClientError> {
        self.check_failure()?;
        self.reads
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(spec.clone());
        Ok(self.batches.get(&spec.symbol).cloned().unwrap_or_default())
    }

    fn reference_universe(&self, as_of: &str) -> Result<Vec<String>, LakeClientError> {
        assert_eq!(as_of, "2024-01-02");
        self.check_failure()?;
        Ok(self.universe.clone())
    }

    fn reference_facts(
        &self,
        _symbols: &[String],
        as_of: &str,
    ) -> Result<BTreeMap<String, BTreeMap<String, String>>, LakeClientError> {
        assert_eq!(as_of, "2024-01-02");
        self.check_failure()?;
        Ok(self.facts.clone())
    }
}

fn facts(entries: &[(&str, &str)]) -> BTreeMap<String, String> {
    entries
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect()
}

fn spy_batches() -> Vec<RecordBatch> {
    vec![equity_bars_batch(
        "SPY",
        &[
            FakeBarRow::new(
                1_704_205_800_000_000_000,
                "100.5",
                "101.25",
                "99.75",
                "101",
                "123456",
            ),
            FakeBarRow::new(
                1_704_292_200_000_000_000,
                "101",
                "102.5",
                "100.25",
                "102.5",
                "654321",
            ),
        ],
    )]
}

fn fake_lake() -> FakeLake {
    FakeLake {
        universe: vec!["SPY".to_string(), "MSFT".to_string()],
        facts: BTreeMap::from([
            ("SPY".to_string(), facts(&[("sector", "etf")])),
            ("MSFT".to_string(), facts(&[("sector", "technology")])),
        ]),
        batches: BTreeMap::from([("SPY".to_string(), spy_batches())]),
        ..FakeLake::default()
    }
}

#[test]
fn reads_the_reference_universe_and_facts() {
    let source = LakeScreeningSource::new(Arc::new(fake_lake()));

    let universe = source.universe("2024-01-02").expect("universe");
    assert_eq!(universe, ["SPY", "MSFT"]);

    let facts = source.facts(&universe, "2024-01-02").expect("facts");
    assert_eq!(
        facts
            .get("MSFT")
            .and_then(|entry| entry.get("sector"))
            .map(String::as_str),
        Some("technology")
    );
}

#[test]
fn maps_lake_arrow_rows_into_exact_sdk_bars() {
    let lake = Arc::new(fake_lake());
    let source = LakeScreeningSource::new(Arc::clone(&lake) as Arc<dyn ScreeningLakeReads>);

    let bars = source
        .bars(
            &["SPY".to_string(), "MSFT".to_string()],
            "1d",
            "2024-01-02",
            3,
        )
        .expect("bars");

    // The lake returns no batches for MSFT, so the symbol is present with an
    // empty list; both symbols are keyed.
    assert_eq!(
        bars.keys().map(String::as_str).collect::<Vec<_>>(),
        ["MSFT", "SPY"]
    );
    assert!(bars["MSFT"].is_empty());
    let spy = &bars["SPY"];
    assert_eq!(spy.len(), 2);
    assert_eq!(spy[0].date, "2024-01-02");
    assert_eq!(spy[0].open, "100.500000000000000000");
    assert_eq!(spy[0].high, "101.250000000000000000");
    assert_eq!(spy[0].low, "99.750000000000000000");
    assert_eq!(spy[0].close, "101.000000000000000000");
    assert_eq!(spy[0].volume, "123456.000000000000000000");
    assert_eq!(spy[1].date, "2024-01-03");
    assert_eq!(spy[1].close, "102.500000000000000000");

    // One ensure and one read per symbol, both over `as_of - lookback_days`
    // through `as_of` with the configured interval.
    let ensures = lake
        .ensures
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(ensures.len(), 2);
    assert_eq!(ensures[0].asset_class, "equity");
    assert_eq!(ensures[0].data_type, "bars");
    assert_eq!(ensures[0].symbol, "SPY");
    assert_eq!(ensures[0].start_date, "2023-12-30");
    assert_eq!(ensures[0].end_date, "2024-01-02");
    assert_eq!(ensures[0].interval.as_deref(), Some("1d"));
    assert_eq!(ensures[1].symbol, "MSFT");

    let reads = lake
        .reads
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(reads.len(), 2);
    assert_eq!(reads[0].symbol, "SPY");
    assert_eq!(reads[0].start_date, "2023-12-30");
    assert_eq!(reads[0].end_date, "2024-01-02");
    assert_eq!(reads[0].interval.as_deref(), Some("1d"));
    assert_eq!(reads[1].symbol, "MSFT");
}

#[test]
fn a_minute_window_is_ensured_and_read_with_its_interval() {
    let lake = Arc::new(fake_lake());
    let source = LakeScreeningSource::new(Arc::clone(&lake) as Arc<dyn ScreeningLakeReads>);

    let bars = source
        .bars(&["SPY".to_string()], "1min", "2024-01-02", 1)
        .expect("bars");

    assert_eq!(bars["SPY"].len(), 2);
    let ensures = lake
        .ensures
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    assert_eq!(ensures[0].interval.as_deref(), Some("1min"));
    assert_eq!(ensures[0].start_date, "2024-01-01");
}

#[test]
fn a_lake_failure_fails_loudly_without_synthetic_data() {
    let source = LakeScreeningSource::new(Arc::new(FakeLake {
        failure: Some(LakeClientError::Transport("connection refused".to_string())),
        ..FakeLake::default()
    }));

    let error = source
        .bars(&["SPY".to_string()], "1d", "2024-01-02", 3)
        .expect_err("the lake failure must fail the run");
    match error {
        ScreeningDataSourceError::Data(message) => {
            assert!(message.contains("connection refused"), "{message}");
        }
    }

    let error = source.universe("2024-01-02").expect_err("universe failure");
    assert!(error.to_string().contains("connection refused"), "{error}");
}

#[test]
fn an_invalid_as_of_date_is_a_data_error() {
    let source = LakeScreeningSource::new(Arc::new(FakeLake::default()));

    let error = source
        .bars(&["SPY".to_string()], "1d", "not-a-date", 3)
        .expect_err("invalid date");
    assert!(error.to_string().contains("invalid as-of date"), "{error}");
}

#[test]
fn from_env_requires_a_bearer_token() {
    let saved = std::env::var("LAKE_API__BEARER_TOKEN").ok();
    std::env::remove_var("LAKE_API__BEARER_TOKEN");

    let error = LakeScreeningSource::from_env().expect_err("token is required");
    assert!(
        error.to_string().contains("LAKE_API__BEARER_TOKEN"),
        "{error}"
    );

    if let Some(value) = saved {
        std::env::set_var("LAKE_API__BEARER_TOKEN", value);
    }
}
