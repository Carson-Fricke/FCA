//! Publication contention and failures through the real leased object/catalog boundary.

use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Condvar, Mutex,
};
use std::time::{Duration, Instant};

use arrow_array::{ArrayRef, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray};
use lake_catalog::{
    BarSource, CatalogRepository, CoverageRecord, CoverageStatus, MemoryCatalogRepository,
};
use lake_contracts::{schema_for, AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_ingestion::{
    AtomicIngestCommitter, LeasedAtomicIngestCommitter, ObjectCatalogCommitter, PartitionWriter,
    StagedParquetObject, StagingPartitionWriter,
};
use lake_storage::{BlobStore, Lease, LeaseProvider, SymbolDayScope};

#[derive(Default)]
struct LockState {
    held: AtomicBool,
    attempts: AtomicUsize,
    releases: AtomicUsize,
    contended: Mutex<bool>,
    changed: Condvar,
    denied: AtomicBool,
}
struct Held(Arc<LockState>);
impl Lease for Held {
    fn lease_id(&self) -> Option<&str> {
        Some("test-publication-owner")
    }
    fn renew(&self) -> Result<(), DataError> {
        if self.0.held.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(DataError::Conflict("released".into()))
        }
    }
    fn release(&self) -> Result<(), DataError> {
        self.0.releases.fetch_add(1, Ordering::SeqCst);
        self.0.held.store(false, Ordering::SeqCst);
        Ok(())
    }
}
struct Locks(Arc<LockState>);
impl LeaseProvider for Locks {
    fn acquire(&self, scope: &SymbolDayScope) -> Result<Box<dyn Lease>, DataError> {
        assert_eq!(scope.symbol, "MSFT");
        self.0.attempts.fetch_add(1, Ordering::SeqCst);
        if self.0.denied.load(Ordering::SeqCst) {
            return Err(DataError::Io("storage permission denied".into()));
        }
        if self
            .0
            .held
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            *self.0.contended.lock().unwrap() = true;
            self.0.changed.notify_all();
            return Err(DataError::Conflict(
                "another symbol-day publisher owns the lease".into(),
            ));
        }
        Ok(Box::new(Held(self.0.clone())))
    }
}
struct Objects {
    lock: Arc<LockState>,
    values: Mutex<BTreeMap<String, Vec<u8>>>,
    calls: AtomicUsize,
    first_started: Mutex<Option<std::sync::mpsc::Sender<()>>>,
    fail: AtomicBool,
}
impl Objects {
    fn new(lock: Arc<LockState>) -> Self {
        Self {
            lock,
            values: Mutex::default(),
            calls: AtomicUsize::new(0),
            first_started: Mutex::default(),
            fail: AtomicBool::new(false),
        }
    }
}
impl BlobStore for Objects {
    fn replace(&self, path: &str, payload: &[u8]) -> Result<(), DataError> {
        assert!(
            self.lock.held.load(Ordering::SeqCst),
            "publication must own its lease"
        );
        self.calls.fetch_add(1, Ordering::SeqCst);
        if let Some(started) = self.first_started.lock().unwrap().take() {
            started.send(()).unwrap();
            let (contended, _) = self
                .lock
                .changed
                .wait_timeout_while(
                    self.lock.contended.lock().unwrap(),
                    Duration::from_secs(1),
                    |seen| !*seen,
                )
                .unwrap();
            assert!(
                *contended,
                "the concurrent publication must reach lease acquisition"
            );
        }
        if self.fail.load(Ordering::SeqCst) {
            return Err(DataError::Io("publication outcome is uncertain".into()));
        }
        self.values
            .lock()
            .unwrap()
            .insert(path.into(), payload.into());
        Ok(())
    }
}
fn publication(interval: &str) -> (Vec<StagedParquetObject>, Vec<CoverageRecord>) {
    let date = Date::from_ymd(2025, 12, 30).unwrap();
    let value = Arc::new(
        Decimal128Array::from(vec![Decimal18::ONE.unscaled()])
            .with_precision_and_scale(38, 18)
            .unwrap(),
    ) as ArrayRef;
    let batch = RecordBatch::try_new(
        Arc::new(schema_for(AssetClass::Equity, DataType::Bars).unwrap()),
        vec![
            Arc::new(
                TimestampNanosecondArray::from(vec![DateTime::midnight(date).to_nanos()])
                    .with_timezone("UTC"),
            ),
            Arc::new(StringArray::from(vec!["MSFT"])),
            value.clone(),
            value.clone(),
            value.clone(),
            value.clone(),
            value,
        ],
    )
    .unwrap();
    let writer = StagingPartitionWriter::new();
    let metadata = writer
        .write(
            &batch,
            date,
            AssetClass::Equity,
            DataType::Bars,
            Some(interval),
            "MSFT",
        )
        .unwrap();
    let coverage = CoverageRecord::new(
        AssetClass::Equity,
        DataType::Bars,
        "MSFT",
        date,
        CoverageStatus::Complete,
        Some(metadata.path.clone()),
        metadata.rows,
        metadata.bytes,
        Some(metadata.checksum.clone()),
        None,
        Some(interval.into()),
    )
    .unwrap()
    .with_bar_provenance(BarSource::Provider, metadata.checksum)
    .unwrap();
    (writer.drain_staged(), vec![coverage])
}
fn committer(
    objects: Arc<Objects>,
    catalog: Arc<MemoryCatalogRepository>,
    lock: Arc<LockState>,
) -> LeasedAtomicIngestCommitter {
    LeasedAtomicIngestCommitter::new(
        Arc::new(ObjectCatalogCommitter::new(objects, catalog)),
        Arc::new(Locks(lock)),
    )
}

#[test]
fn concurrent_symbol_day_publications_wait_and_both_preserve_their_objects_and_coverage() {
    let lock = Arc::new(LockState::default());
    let objects = Arc::new(Objects::new(lock.clone()));
    let catalog = Arc::new(MemoryCatalogRepository::new());
    let publisher = Arc::new(
        committer(objects.clone(), catalog.clone(), lock.clone())
            .with_acquisition_timeout(Duration::from_secs(1)),
    );
    let (first_started, waiting) = std::sync::mpsc::channel();
    *objects.first_started.lock().unwrap() = Some(first_started);
    let first = publication("1min");
    let second = publication("1d");
    std::thread::scope(|scope| {
        let first = scope.spawn(|| publisher.commit(&first.0, &first.1));
        waiting.recv_timeout(Duration::from_secs(1)).unwrap();
        let second = scope.spawn(|| publisher.commit(&second.0, &second.1));
        first.join().unwrap().unwrap();
        second.join().unwrap().unwrap();
    });
    assert!(*lock.contended.lock().unwrap());
    assert_eq!(lock.releases.load(Ordering::SeqCst), 2);
    assert_eq!(objects.calls.load(Ordering::SeqCst), 2);
    for (staged, rows) in [first, second] {
        assert_eq!(
            objects.values.lock().unwrap()[&staged[0].metadata.path],
            staged[0].payload
        );
        let row = &rows[0];
        assert_eq!(
            catalog
                .get(
                    row.asset_class,
                    row.data_type,
                    row.interval.as_deref(),
                    &row.symbol,
                    row.session_date
                )
                .unwrap(),
            Some(row.clone())
        );
    }
}

#[test]
fn busy_deadline_permission_denial_and_uncertain_publication_never_replay_mutations() {
    let (staged, rows) = publication("1min");
    for mode in ["busy", "denied", "publication"] {
        let lock = Arc::new(LockState::default());
        lock.held.store(mode == "busy", Ordering::SeqCst);
        lock.denied.store(mode == "denied", Ordering::SeqCst);
        let objects = Arc::new(Objects::new(lock.clone()));
        objects.fail.store(mode == "publication", Ordering::SeqCst);
        let catalog = Arc::new(MemoryCatalogRepository::new());
        let publisher = committer(objects.clone(), catalog.clone(), lock.clone())
            .with_acquisition_timeout(Duration::from_millis(2));
        let start = Instant::now();
        let failure = publisher.commit(&staged, &rows).unwrap_err();
        assert!(start.elapsed() < Duration::from_secs(1));
        match mode {
            "busy" => {
                assert!(
                    matches!(failure, DataError::Conflict(message) if message.contains("timed out"))
                );
                assert_eq!(objects.calls.load(Ordering::SeqCst), 0);
            }
            "denied" => {
                assert!(matches!(failure, DataError::Io(_)));
                assert_eq!(lock.attempts.load(Ordering::SeqCst), 1);
                assert_eq!(objects.calls.load(Ordering::SeqCst), 0);
            }
            _ => {
                assert!(matches!(failure, DataError::Io(_)));
                assert_eq!(objects.calls.load(Ordering::SeqCst), 1);
                assert_eq!(lock.releases.load(Ordering::SeqCst), 1);
            }
        }
        assert!(catalog
            .get(
                AssetClass::Equity,
                DataType::Bars,
                Some("1min"),
                "MSFT",
                rows[0].session_date
            )
            .unwrap()
            .is_none());
    }
}
