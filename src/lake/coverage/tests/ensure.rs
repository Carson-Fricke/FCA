//! Ensure flow parity tests with injected fake launchers.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::future::BoxFuture;
use lake_catalog::{
    CatalogRepository, CoverageRecord, CoverageStatus, JobRecord, JobRepository, JobState,
    MemoryCatalogRepository, MemoryJobRepository,
};
use lake_contracts::{AssetClass, DataError, DataType, Date, DateTime};
use lake_coverage::{
    ensure_coverage, ensure_job_id, requested_coverage, CoverageState, EnsureJob,
    EnsureJobLauncher, EnsureOutcome, EnsureSpec,
};

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn instant(year: i32, month: u32, day_of_month: u32, hour: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day_of_month, hour, 0, 0, 0).expect("valid datetime")
}

fn as_of() -> DateTime {
    instant(2024, 1, 22, 12)
}

fn spec() -> EnsureSpec {
    EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Trade,
        symbol: "AAPL".to_string(),
        start_date: day(2024, 1, 18),
        end_date: day(2024, 1, 19),
        interval: None,
    }
}

fn single_day_spec() -> EnsureSpec {
    EnsureSpec {
        end_date: day(2024, 1, 18),
        ..spec()
    }
}

fn coverage_record(status: CoverageStatus, session_date: Date) -> CoverageRecord {
    CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        status,
        None,
        0,
        0,
        None,
        None,
        None,
    )
    .expect("valid coverage")
}

fn launcher_from<F>(launcher: F) -> EnsureJobLauncher
where
    F: Fn(EnsureJob) -> BoxFuture<'static, Result<(), DataError>> + Send + Sync + 'static,
{
    Arc::new(launcher)
}

fn recording_launcher() -> (EnsureJobLauncher, Arc<Mutex<Vec<EnsureJob>>>) {
    let launched: Arc<Mutex<Vec<EnsureJob>>> = Arc::new(Mutex::new(Vec::new()));
    let recorder = Arc::clone(&launched);
    let launcher = launcher_from(move |job| {
        recorder.lock().expect("launcher lock").push(job);
        Box::pin(async { Ok(()) })
    });
    (launcher, launched)
}

#[test]
fn ensure_job_id_is_pinned_to_the_python_digest() {
    assert_eq!(
        ensure_job_id(&spec()).expect("job id"),
        "ensure-8036196a191cc542158b5ff9e51141d1a68e90214dee4838acfb589d296fb87e"
    );
}

#[test]
fn ensure_job_id_is_stable_for_equivalent_requests() {
    let noisy = EnsureSpec {
        symbol: "  aapl  ".to_string(),
        ..spec()
    };

    assert_eq!(
        ensure_job_id(&noisy).expect("job id"),
        ensure_job_id(&spec()).expect("job id")
    );
    assert_eq!(noisy.canonical(), spec());
    assert_ne!(
        ensure_job_id(&EnsureSpec {
            symbol: "MSFT".to_string(),
            ..spec()
        })
        .expect("job id"),
        ensure_job_id(&spec()).expect("job id")
    );
}

#[test]
fn requested_coverage_validates_the_interval_pairing() {
    let catalog = MemoryCatalogRepository::new();
    let bars = |interval: Option<&str>| EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Bars,
        symbol: "AAPL".to_string(),
        start_date: day(2024, 1, 18),
        end_date: day(2024, 1, 19),
        interval: interval.map(str::to_string),
    };

    let missing = requested_coverage(&catalog, None, &bars(None), as_of(), "ensure")
        .expect_err("bars without an interval");
    assert_eq!(missing.code, "invalid_interval");
    assert_eq!(missing.to_string(), "bars requires an interval");

    let unknown = requested_coverage(&catalog, None, &bars(Some("5m")), as_of(), "ensure")
        .expect_err("unknown interval token");
    assert_eq!(unknown.code, "invalid_interval");
    assert_eq!(
        unknown.to_string(),
        "invalid bar interval: \"5m\"; expected one of 1min, 1d"
    );

    let misplaced = requested_coverage(
        &catalog,
        None,
        &EnsureSpec {
            data_type: DataType::Trade,
            interval: Some("1min".to_string()),
            ..bars(Some("1min"))
        },
        as_of(),
        "ensure",
    )
    .expect_err("interval on a non-bars type");
    assert_eq!(misplaced.code, "invalid_interval");
    assert_eq!(misplaced.to_string(), "interval is supported for bars only");

    let option_bars = requested_coverage(
        &catalog,
        None,
        &EnsureSpec {
            asset_class: AssetClass::Option,
            data_type: DataType::Bars,
            interval: Some("1d".to_string()),
            ..bars(Some("1d"))
        },
        as_of(),
        "ensure",
    )
    .expect("option bars use XNYS sessions");
    assert_eq!(option_bars.entries.len(), 2);
    assert_eq!(option_bars.missing().len(), 2);

    let report = requested_coverage(&catalog, None, &bars(Some("1min")), as_of(), "ensure")
        .expect("equity bars are supported");
    assert_eq!(report.entries.len(), 2);
    assert!(report.missing().len() == 2);
}

#[test]
fn requested_coverage_keeps_bar_intervals_distinct() {
    let date = day(2024, 1, 18);
    let catalog = MemoryCatalogRepository::from_records([CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        date,
        CoverageStatus::Complete,
        Some(
            "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
                .to_string(),
        ),
        3,
        100,
        None,
        None,
        Some("1min".to_string()),
    )
    .expect("bars coverage")]);
    let spec = |interval: &str| EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Bars,
        symbol: "AAPL".to_string(),
        start_date: date,
        end_date: date,
        interval: Some(interval.to_string()),
    };

    let minute = requested_coverage(&catalog, None, &spec("1min"), as_of(), "ensure")
        .expect("minute coverage");
    assert!(minute.is_satisfied());
    let day_report =
        requested_coverage(&catalog, None, &spec("1d"), as_of(), "ensure").expect("day coverage");
    assert_eq!(day_report.missing().len(), 1);
    assert!(day_report.entries[0].record.is_none());
}

#[tokio::test]
async fn satisfied_coverage_returns_covered_without_launching() {
    let catalog = MemoryCatalogRepository::from_records([
        coverage_record(CoverageStatus::Complete, day(2024, 1, 18)),
        coverage_record(CoverageStatus::Complete, day(2024, 1, 19)),
    ]);
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let launched: Arc<Mutex<usize>> = Arc::new(Mutex::new(0));
    let counter = Arc::clone(&launched);
    let launcher = launcher_from(move |_job| {
        *counter.lock().expect("launcher lock") += 1;
        Box::pin(async { Ok(()) })
    });

    let outcome = ensure_coverage(&catalog, &jobs, None, launcher, &spec(), as_of(), 1.0)
        .await
        .expect("ensure");

    match outcome {
        EnsureOutcome::Covered(report) => {
            assert_eq!(report.complete().len(), 2);
            assert!(report.is_satisfied());
        }
        other => panic!("expected covered coverage, got {other:?}"),
    }
    assert_eq!(*launched.lock().expect("launcher lock"), 0);
    assert!(jobs
        .get(&ensure_job_id(&spec()).expect("job id"))
        .expect("job lookup")
        .is_none());
}

#[tokio::test]
async fn a_successful_launcher_that_fills_the_catalog_returns_covered() {
    let catalog = Arc::new(MemoryCatalogRepository::new());
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let launcher = {
        let catalog = Arc::clone(&catalog);
        launcher_from(move |job| {
            let catalog = Arc::clone(&catalog);
            Box::pin(async move {
                for gap in &job.gaps {
                    let record = CoverageRecord::new(
                        job.request.asset_class,
                        job.request.data_type,
                        job.request.symbol.clone(),
                        gap.session_date(),
                        CoverageStatus::Complete,
                        None,
                        0,
                        0,
                        None,
                        None,
                        job.request.interval.clone(),
                    )?;
                    catalog.upsert(&record)?;
                }
                Ok(())
            })
        })
    };

    let outcome = ensure_coverage(&*catalog, &jobs, None, launcher, &spec(), as_of(), 1.0)
        .await
        .expect("ensure");

    match outcome {
        EnsureOutcome::Covered(report) => {
            assert!(report.is_satisfied());
            assert_eq!(report.complete().len(), 2);
        }
        other => panic!("expected covered coverage, got {other:?}"),
    }
}

#[tokio::test]
async fn in_flight_jobs_are_reused_without_relaunching() {
    let job_id = ensure_job_id(&spec()).expect("job id");
    let repository = MemoryJobRepository::new();
    repository
        .upsert(
            &JobRecord::new(job_id.clone(), JobState::Running, as_of(), as_of(), None)
                .expect("valid job"),
        )
        .expect("job upsert");
    let jobs: Arc<dyn JobRepository> = Arc::new(repository);
    let (launcher, launched) = recording_launcher();

    let outcome = ensure_coverage(
        &MemoryCatalogRepository::new(),
        &jobs,
        None,
        launcher,
        &spec(),
        as_of(),
        1.0,
    )
    .await
    .expect("ensure");

    match outcome {
        EnsureOutcome::Accepted(record) => {
            assert_eq!(record.job_id, job_id);
            assert_eq!(record.state, JobState::Running);
        }
        other => panic!("expected an accepted job, got {other:?}"),
    }
    assert!(launched.lock().expect("launcher lock").is_empty());
}

#[tokio::test]
async fn terminal_jobs_with_unsatisfied_coverage_are_superseded_and_relaunched() {
    let catalog = MemoryCatalogRepository::from_records([coverage_record(
        CoverageStatus::Error,
        day(2024, 1, 18),
    )]);
    let job_id = ensure_job_id(&single_day_spec()).expect("job id");
    let repository = MemoryJobRepository::new();
    repository
        .upsert(
            &JobRecord::new(
                job_id.clone(),
                JobState::Succeeded,
                as_of(),
                as_of(),
                Some("stale attempt".to_string()),
            )
            .expect("valid job"),
        )
        .expect("job upsert");
    let jobs: Arc<dyn JobRepository> = Arc::new(repository);
    let (launcher, launched) = recording_launcher();

    let outcome = ensure_coverage(
        &catalog,
        &jobs,
        None,
        launcher,
        &single_day_spec(),
        as_of(),
        1.0,
    )
    .await
    .expect("ensure");

    match &outcome {
        EnsureOutcome::Accepted(record) => {
            assert_eq!(record.job_id, job_id);
            assert_eq!(record.state, JobState::Complete);
            assert_eq!(record.error_message, None);
            assert_eq!(record.created_at, as_of());
            assert_eq!(record.updated_at, as_of());
        }
        other => panic!("expected an accepted job, got {other:?}"),
    }

    let launched = launched.lock().expect("launcher lock");
    assert_eq!(launched.len(), 1);
    assert_eq!(
        launched[0].gaps[0].state,
        CoverageState::Error,
        "the repairable error is the relaunched gap"
    );
    drop(launched);

    let polled = jobs.get(&job_id).expect("job lookup").expect("job row");
    assert_eq!(polled.state, JobState::Complete);
    assert_eq!(polled.error_message, None);
}

#[tokio::test]
async fn launcher_failure_is_persisted_as_a_failed_job() {
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let launcher = launcher_from(|job| {
        Box::pin(async move {
            Err(DataError::EnsureJob(format!(
                "launcher failed for {}",
                job.job_id
            )))
        })
    });

    let outcome = ensure_coverage(
        &MemoryCatalogRepository::new(),
        &jobs,
        None,
        launcher,
        &spec(),
        as_of(),
        1.0,
    )
    .await
    .expect("ensure");

    match &outcome {
        EnsureOutcome::Accepted(record) => {
            assert_eq!(record.state, JobState::Failed);
            assert!(record
                .error_message
                .as_deref()
                .is_some_and(|message| message.contains("launcher failed for ensure-")));
        }
        other => panic!("expected an accepted job, got {other:?}"),
    }

    let polled = jobs
        .get(&ensure_job_id(&spec()).expect("job id"))
        .expect("job lookup")
        .expect("job row");
    assert_eq!(polled.state, JobState::Failed);
    assert!(polled
        .error_message
        .as_deref()
        .is_some_and(|message| message.contains("launcher failed for ensure-")));
}

#[tokio::test]
async fn deadline_expiry_returns_running_and_late_completion_settles() {
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let launcher = {
        let release = Arc::clone(&release);
        launcher_from(move |_job| {
            let release = Arc::clone(&release);
            Box::pin(async move {
                release.notified().await;
                Ok(())
            })
        })
    };

    let outcome = ensure_coverage(
        &MemoryCatalogRepository::new(),
        &jobs,
        None,
        launcher,
        &spec(),
        as_of(),
        0.02,
    )
    .await
    .expect("ensure");

    let job_id = ensure_job_id(&spec()).expect("job id");
    match &outcome {
        EnsureOutcome::Accepted(record) => {
            assert_eq!(record.job_id, job_id);
            assert_eq!(record.state, JobState::Running);
        }
        other => panic!("expected the still-running job, got {other:?}"),
    }
    assert_eq!(
        jobs.get(&job_id)
            .expect("job lookup")
            .expect("job row")
            .state,
        JobState::Running
    );

    release.notify_one();

    let mut settled = None;
    for _ in 0..200 {
        let record = jobs.get(&job_id).expect("job lookup").expect("job row");
        if record.state == JobState::Complete {
            settled = Some(record);
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let settled = settled.expect("the detached task settles the job record");
    assert_eq!(settled.created_at, as_of());
    assert_eq!(settled.updated_at, as_of());
}

#[tokio::test]
async fn stale_derived_bars_are_relaunched_as_repairable_gaps() {
    use lake_catalog::BarSource;

    let session_date = day(2024, 1, 18);
    let derived = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some(
            "date=2024-01-18/asset_class=equity/data_type=bars/interval=1min/AAPL.parquet"
                .to_string(),
        ),
        3,
        100,
        None,
        None,
        Some("1min".to_string()),
    )
    .expect("bars coverage")
    .with_bar_provenance(BarSource::Derived, "old-trade-rev")
    .expect("provenance");
    let repaired_trade = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Trade,
        "AAPL",
        session_date,
        CoverageStatus::Complete,
        Some("date=2024-01-18/asset_class=equity/data_type=trade/AAPL.parquet".to_string()),
        2,
        80,
        Some("new-trade-rev".to_string()),
        None,
        None,
    )
    .expect("trade coverage");
    let catalog = MemoryCatalogRepository::from_records([derived, repaired_trade]);

    let spec = EnsureSpec {
        asset_class: AssetClass::Equity,
        data_type: DataType::Bars,
        symbol: "AAPL".to_string(),
        start_date: session_date,
        end_date: session_date,
        interval: Some("1min".to_string()),
    };
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let (launcher, launched) = recording_launcher();
    let outcome = ensure_coverage(
        &catalog,
        &jobs,
        None,
        launcher,
        &spec,
        instant(2024, 1, 22, 12),
        1.0,
    )
    .await
    .expect("ensure");

    match &outcome {
        EnsureOutcome::Accepted(record) => {
            assert_eq!(record.job_id, ensure_job_id(&spec).expect("job id"));
            assert_eq!(record.state, JobState::Complete);
        }
        other => panic!("expected a relaunched job, got {other:?}"),
    }
    let launched = launched.lock().expect("launcher lock");
    assert_eq!(launched.len(), 1);
    assert_eq!(launched[0].gaps.len(), 1);
    assert_eq!(launched[0].gaps[0].state, CoverageState::Partial);
    assert_eq!(launched[0].gaps[0].session_date(), session_date);
}

#[tokio::test]
async fn crypto_ensure_plans_utc_days_as_gaps() {
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let (launcher, launched) = recording_launcher();
    let spec = EnsureSpec {
        asset_class: AssetClass::Crypto,
        data_type: DataType::Quote,
        symbol: "BTC/USD".to_string(),
        start_date: day(2024, 1, 19),
        end_date: day(2024, 1, 22),
        interval: None,
    };

    let outcome = ensure_coverage(
        &MemoryCatalogRepository::new(),
        &jobs,
        None,
        launcher,
        &spec,
        instant(2024, 1, 22, 12),
        1.0,
    )
    .await
    .expect("ensure");

    match &outcome {
        EnsureOutcome::Accepted(record) => assert_eq!(record.state, JobState::Complete),
        other => panic!("expected an accepted job, got {other:?}"),
    }

    let launched = launched.lock().expect("launcher lock");
    assert_eq!(launched.len(), 1);
    assert_eq!(launched[0].request.symbol, "BTC/USD");
    let dates: Vec<Date> = launched[0]
        .gaps
        .iter()
        .map(|gap| gap.session_date())
        .collect();
    assert_eq!(
        dates,
        vec![
            day(2024, 1, 19),
            day(2024, 1, 20),
            day(2024, 1, 21),
            day(2024, 1, 22),
        ]
    );
    let states: Vec<CoverageState> = launched[0].gaps.iter().map(|gap| gap.state).collect();
    assert_eq!(
        states,
        vec![
            CoverageState::Missing,
            CoverageState::Missing,
            CoverageState::Missing,
            CoverageState::Partial,
        ]
    );
}

#[tokio::test]
async fn ensure_covers_multi_year_windows_without_a_date_range_cap() {
    let jobs: Arc<dyn JobRepository> = Arc::new(MemoryJobRepository::new());
    let (launcher, launched) = recording_launcher();
    let spec = EnsureSpec {
        start_date: day(2022, 1, 3),
        end_date: day(2024, 1, 19),
        ..spec()
    };

    let outcome = ensure_coverage(
        &MemoryCatalogRepository::new(),
        &jobs,
        None,
        launcher,
        &spec,
        as_of(),
        1.0,
    )
    .await
    .expect("ensure");

    match &outcome {
        EnsureOutcome::Accepted(record) => assert_eq!(record.state, JobState::Complete),
        other => panic!("expected an accepted job, got {other:?}"),
    }

    let launched = launched.lock().expect("launcher lock");
    assert_eq!(launched.len(), 1);
    let gaps = &launched[0].gaps;
    assert!(gaps.len() > 366, "a multi-year window plans every session");
    assert_eq!(gaps[0].session_date(), day(2022, 1, 3));
    assert_eq!(
        gaps[gaps.len() - 1].session_date(),
        day(2024, 1, 19),
        "the whole window is planned in one job"
    );
}
