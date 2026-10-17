//! Publication contention and failures through the real leased object/catalog boundary.

use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Condvar, Mutex,
};
use std::time::{Duration, Instant};

use arrow_array::{ArrayRef, Decimal128Array, RecordBatch, StringArray, TimestampNanosecondArray};
use lake_catalog::{
    BarSource, CatalogRepository, CoverageRecord, CoverageStatus, PublicationCatalog,
};
use lake_contracts::{schema_for, AssetClass, DataError, DataType, Date, DateTime, Decimal18};
use lake_ingestion::{
    AtomicIngestCommitter, ObjectCatalogCommitter, PartitionWriter, StagedParquetObject,
    StagingPartitionWriter,
};
use lake_storage::{BlobStore, Lease, LeaseProvider, SymbolDayScope};

#[derive(Default)]
struct LockState {
    held: AtomicBool,
    manifest: Mutex<Vec<u8>>,
    generation: AtomicUsize,
    attempts: AtomicUsize,
    releases: AtomicUsize,
    contended: Mutex<bool>,
    changed: Condvar,
    denied: AtomicBool,
    renewals: AtomicUsize,
    acknowledgement_lost: AtomicBool,
    wait_for_renewal: AtomicBool,
}
struct Held(Arc<LockState>, usize);
impl Lease for Held {
    fn renewal_interval(&self) -> Option<Duration> {
        Some(Duration::from_millis(10))
    }
    fn read(&self) -> Result<Vec<u8>, DataError> {
        let bytes = self.0.manifest.lock().unwrap().clone();
        Ok(if bytes.is_empty() {
            b"[]".to_vec()
        } else {
            bytes
        })
    }
    fn publish(&self, payload: &[u8]) -> Result<(), DataError> {
        if self.0.generation.load(Ordering::SeqCst) != self.1 || !self.0.held.load(Ordering::SeqCst)
        {
            return Err(DataError::Conflict("stale publication fence".into()));
        }
        *self.0.manifest.lock().unwrap() = payload.to_vec();
        if self.0.acknowledgement_lost.swap(false, Ordering::SeqCst) {
            return Err(DataError::Io(
                "manifest acknowledgement lost after commit".into(),
            ));
        }
        Ok(())
    }
    fn lease_id(&self) -> Option<&str> {
        Some("test-publication-owner")
    }
    fn renew(&self) -> Result<(), DataError> {
        self.0.renewals.fetch_add(1, Ordering::SeqCst);
        self.0.changed.notify_all();
        if self.0.held.load(Ordering::SeqCst) && self.0.generation.load(Ordering::SeqCst) == self.1
        {
            Ok(())
        } else {
            Err(DataError::Conflict("released".into()))
        }
    }
    fn release(&self) -> Result<(), DataError> {
        self.0.releases.fetch_add(1, Ordering::SeqCst);
        if self.0.generation.load(Ordering::SeqCst) == self.1 {
            self.0.held.store(false, Ordering::SeqCst);
        }
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
        Ok(Box::new(Held(
            self.0.clone(),
            self.0.generation.fetch_add(1, Ordering::SeqCst) + 1,
        )))
    }
}
struct Objects {
    lock: Arc<LockState>,
    values: Mutex<BTreeMap<String, Vec<u8>>>,
    calls: AtomicUsize,
    first_started: Mutex<Option<std::sync::mpsc::Sender<()>>>,
    fail: AtomicBool,
    expire: AtomicBool,
    fail_second: AtomicBool,
}
impl Objects {
    fn new(lock: Arc<LockState>) -> Self {
        Self {
            lock,
            values: Mutex::default(),
            calls: AtomicUsize::new(0),
            first_started: Mutex::default(),
            fail: AtomicBool::new(false),
            expire: AtomicBool::new(false),
            fail_second: AtomicBool::new(false),
        }
    }
}
impl BlobStore for Objects {
    fn replace(&self, path: &str, payload: &[u8]) -> Result<(), DataError> {
        assert!(
            self.lock.held.load(Ordering::SeqCst),
            "publication must own its lease"
        );
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        if self.lock.wait_for_renewal.load(Ordering::SeqCst) {
            let _wait = self
                .lock
                .changed
                .wait_timeout_while(
                    self.lock.contended.lock().unwrap(),
                    Duration::from_secs(1),
                    |_| self.lock.renewals.load(Ordering::SeqCst) == 0,
                )
                .unwrap();
            assert!(
                self.lock.renewals.load(Ordering::SeqCst) > 0,
                "slow publication must renew its ownership"
            );
        }
        if self.expire.swap(false, Ordering::SeqCst) {
            self.lock.generation.fetch_add(1, Ordering::SeqCst);
        }
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
        if self.fail.load(Ordering::SeqCst)
            || (self.fail_second.load(Ordering::SeqCst) && call % 2 == 1)
        {
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
    publication_at(interval, "1")
}
fn publication_at(interval: &str, price: &str) -> (Vec<StagedParquetObject>, Vec<CoverageRecord>) {
    let date = Date::from_ymd(2025, 12, 30).unwrap();
    let value = Arc::new(
        Decimal128Array::from(vec![Decimal18::parse(price).unwrap().unscaled()])
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
impl lake_storage::ReadableBlobStore for Objects {
    fn read(&self, path: &str) -> Result<Vec<u8>, DataError> {
        if path.starts_with("publications/") {
            let bytes = self.lock.manifest.lock().unwrap().clone();
            return if bytes.is_empty() {
                Err(DataError::NotFound(path.into()))
            } else {
                Ok(bytes)
            };
        }
        self.values
            .lock()
            .unwrap()
            .get(path)
            .cloned()
            .ok_or_else(|| DataError::NotFound(path.into()))
    }
    fn list_prefix(&self, _: &str) -> Result<Vec<String>, DataError> {
        unimplemented!()
    }
    fn delete_prefix(&self, _: &str) -> Result<u64, DataError> {
        unimplemented!()
    }
}
fn catalog(
    objects: Arc<Objects>,
    lock: Arc<LockState>,
    timeout: Duration,
) -> Arc<PublicationCatalog> {
    Arc::new(
        PublicationCatalog::new(objects, Arc::new(Locks(lock))).with_acquisition_timeout(timeout),
    )
}
fn committer(objects: Arc<Objects>, catalog: Arc<PublicationCatalog>) -> ObjectCatalogCommitter {
    ObjectCatalogCommitter::new(objects, catalog)
}

#[test]
fn concurrent_symbol_day_publications_wait_and_both_preserve_their_objects_and_coverage() {
    let lock = Arc::new(LockState::default());
    let objects = Arc::new(Objects::new(lock.clone()));
    let catalog = catalog(objects.clone(), lock.clone(), Duration::from_millis(100));
    let publisher = Arc::new(committer(objects.clone(), catalog.clone()));
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
        let catalog = catalog(objects.clone(), lock.clone(), Duration::from_millis(100));
        let publisher = committer(objects.clone(), catalog.clone());
        let start = Instant::now();
        let failure = publisher.commit(&staged, &rows).unwrap_err();
        assert!(start.elapsed() < Duration::from_secs(1));
        match mode {
            "busy" => {
                assert!(matches!(failure, DataError::Conflict(_)));
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

#[test]
fn failed_multi_dataset_upload_and_expired_owner_leave_previous_manifest_visible() {
    use lake_storage::ReadableBlobStore;
    for mode in ["second_upload", "expired_owner", "cancelled"] {
        let lock = Arc::new(LockState::default());
        let objects = Arc::new(Objects::new(lock.clone()));
        let pipeline = lake_pipeline::Pipeline::new(lake_pipeline::Limits::default()).unwrap();
        let catalog = Arc::new(
            PublicationCatalog::new(objects.clone(), Arc::new(Locks(lock.clone())))
                .with_pipeline(pipeline.clone()),
        );
        let publisher = committer(objects.clone(), catalog.clone());
        let original = publication("1min");
        publisher.commit(&original.0, &original.1).unwrap();
        let previous = lock.manifest.lock().unwrap().clone();
        let original_bytes = objects.read(&original.0[0].metadata.path).unwrap();
        let mut replacement = publication_at("1min", "2");
        let second = publication("1d");
        replacement.0.extend(second.0);
        replacement.1.extend(second.1);
        objects.calls.store(0, Ordering::SeqCst);
        objects
            .fail_second
            .store(mode == "second_upload", Ordering::SeqCst);
        objects
            .expire
            .store(mode == "expired_owner", Ordering::SeqCst);
        if mode == "cancelled" {
            pipeline.cancel();
        }
        assert!(publisher.commit(&replacement.0, &replacement.1).is_err());
        assert_eq!(*lock.manifest.lock().unwrap(), previous, "{mode}");
        assert_eq!(
            objects.read(&original.0[0].metadata.path).unwrap(),
            original_bytes
        );
        assert_eq!(
            catalog
                .get(
                    AssetClass::Equity,
                    DataType::Bars,
                    Some("1min"),
                    "MSFT",
                    original.1[0].session_date
                )
                .unwrap(),
            Some(original.1[0].clone())
        );
        assert!(catalog
            .get(
                AssetClass::Equity,
                DataType::Bars,
                Some("1d"),
                "MSFT",
                original.1[0].session_date
            )
            .unwrap()
            .is_none());
        if mode == "second_upload" {
            assert_eq!(
                objects.calls.load(Ordering::SeqCst),
                2,
                "failed writes are never replayed"
            );
            objects.fail_second.store(false, Ordering::SeqCst);
            publisher.commit(&replacement.0, &replacement.1).unwrap();
            // Even after a successful replacement, the previously pinned bytes remain valid.
            assert_eq!(
                objects.read(&original.0[0].metadata.path).unwrap(),
                original_bytes
            );
            assert_eq!(
                catalog
                    .get(
                        AssetClass::Equity,
                        DataType::Bars,
                        Some("1min"),
                        "MSFT",
                        original.1[0].session_date
                    )
                    .unwrap(),
                Some(replacement.1[0].clone())
            );
        }
    }
}

#[test]
fn slow_uploads_renew_and_lost_manifest_acknowledgements_are_not_replayed() {
    let lock = Arc::new(LockState::default());
    let objects = Arc::new(Objects::new(lock.clone()));
    let catalog = catalog(objects.clone(), lock.clone(), Duration::from_secs(1));
    let publisher = committer(objects.clone(), catalog.clone());
    lock.wait_for_renewal.store(true, Ordering::SeqCst);
    lock.acknowledgement_lost.store(true, Ordering::SeqCst);
    let (objects_to_publish, coverage) = publication("1min");
    assert!(matches!(
        publisher.commit(&objects_to_publish, &coverage),
        Err(DataError::Io(_))
    ));
    assert_eq!(objects.calls.load(Ordering::SeqCst), 1);
    assert_eq!(lock.releases.load(Ordering::SeqCst), 1);
    assert!(lock.renewals.load(Ordering::SeqCst) > 0);
    // The failed acknowledgement is unknown, not rollback: reconciliation sees
    // the one committed manifest and can retain the original execution identity.
    assert_eq!(
        catalog
            .get(
                AssetClass::Equity,
                DataType::Bars,
                Some("1min"),
                "MSFT",
                coverage[0].session_date
            )
            .unwrap(),
        Some(coverage[0].clone())
    );
}

#[test]
fn invalid_publication_candidates_never_reach_storage_mutation() {
    for mode in ["duplicate", "payload", "missing_object"] {
        let lock = Arc::new(LockState::default());
        let objects = Arc::new(Objects::new(lock.clone()));
        let catalog = catalog(objects.clone(), lock.clone(), Duration::from_secs(1));
        let publisher = committer(objects.clone(), catalog);
        let (mut staged, mut coverage) = publication("1min");
        if mode == "duplicate" {
            staged.push(staged[0].clone());
            coverage.extend(publication("1d").1);
        } else if mode == "missing_object" {
            staged.clear();
            coverage[0].object_path = None;
        } else {
            staged[0].payload[0] ^= 1;
        }
        assert!(publisher.commit(&staged, &coverage).is_err());
        assert_eq!(objects.calls.load(Ordering::SeqCst), 0);
        assert_eq!(lock.attempts.load(Ordering::SeqCst), 0);
        assert!(lock.manifest.lock().unwrap().is_empty());
    }
}
