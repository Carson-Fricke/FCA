//! A single leased symbol-day manifest is the coverage visibility authority.
//! Objects are immutable; readers never inspect candidate objects or a secondary index.
use crate::{CatalogRepository, CoverageQuery, CoverageRecord};

/// One namespace shared by coverage readers and manifest ownership adapters.
pub const PUBLICATION_PREFIX: &str = "publications";
use lake_contracts::{
    canonical_symbol, immutable_path, partition_path, AssetClass, DataError, DataType, Date,
};
use lake_storage::{Lease, LeaseProvider, ReadableBlobStore, SymbolDayScope};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::{Duration, Instant};

/// Shared local/Azure publication contract. Storage adapters own fencing.
pub struct PublicationCatalog {
    objects: Arc<dyn ReadableBlobStore>,
    leases: Arc<dyn LeaseProvider>,
    acquisition_timeout: Duration,
    pipeline: Arc<lake_pipeline::Pipeline>,
}

impl PublicationCatalog {
    pub fn new(objects: Arc<dyn ReadableBlobStore>, leases: Arc<dyn LeaseProvider>) -> Self {
        Self {
            objects,
            leases,
            acquisition_timeout: Duration::from_secs(75),
            pipeline: lake_pipeline::Pipeline::global(),
        }
    }

    pub fn with_pipeline(mut self, pipeline: Arc<lake_pipeline::Pipeline>) -> Self {
        self.pipeline = pipeline;
        self
    }

    pub fn with_acquisition_timeout(mut self, timeout: Duration) -> Self {
        self.acquisition_timeout = timeout;
        self
    }

    /// Upload candidates, then make every selected dataset visible in one fenced write.
    /// Failure before that write leaves prior coverage unchanged. An interrupted write
    /// has an unknown outcome; callers reconcile coverage rather than replaying it.
    pub fn publish(
        &self,
        records: &[CoverageRecord],
        upload: impl FnOnce() -> Result<(), DataError>,
    ) -> Result<(), DataError> {
        let scope = scope_for_coverage(records)?;
        validate(&scope, records)?;
        let _io = self.pipeline.publication()?;
        let waiting = self.pipeline.timer(lake_pipeline::Stage::LeaseWait);
        let lease = self.acquire(&scope)?;
        drop(waiting);
        let held = RenewingLease::new(lease);
        let mut current = decode(&scope, &held.lease.read()?)?;
        upload()?;
        for record in records {
            current.retain(|old| {
                (old.data_type, &old.interval) != (record.data_type, &record.interval)
            });
            current.push(record.clone());
        }
        current.sort_by_key(|r| (r.data_type, r.interval.clone()));
        if held.lost.load(Ordering::Acquire) {
            return Err(DataError::Conflict(
                "publication lease renewal failed".into(),
            ));
        }
        let payload = serde_json::to_vec(&current).map_err(DataError::io)?;
        let _manifest = self.pipeline.timer(lake_pipeline::Stage::Manifest);
        self.pipeline.commit(|| held.lease.publish(&payload))?;
        // Release errors cannot change the committed outcome. Drop also handles
        // early returns and panics; the finite Azure lease is the crash backstop.
        Ok(())
    }

    fn acquire(&self, scope: &SymbolDayScope) -> Result<Box<dyn Lease>, DataError> {
        let started = Instant::now();
        let mut delay = Duration::from_millis(10);
        loop {
            self.pipeline.check()?;
            match self.leases.acquire(scope) {
                Ok(lease) => return Ok(lease),
                Err(DataError::Conflict(_)) if started.elapsed() < self.acquisition_timeout => {
                    std::thread::sleep(
                        delay.min(self.acquisition_timeout.saturating_sub(started.elapsed())),
                    );
                    delay = delay.saturating_mul(2).min(Duration::from_millis(250));
                }
                Err(error) => return Err(error),
            }
        }
    }

    fn day(&self, scope: &SymbolDayScope) -> Result<Vec<CoverageRecord>, DataError> {
        match self
            .objects
            .read(&format!("{PUBLICATION_PREFIX}/{}", scope.blob_name()))
        {
            Ok(bytes) => decode(scope, &bytes),
            Err(DataError::NotFound(_)) => Ok(Vec::new()),
            Err(error) => Err(error),
        }
    }
}

pub fn scope_for_coverage(records: &[CoverageRecord]) -> Result<SymbolDayScope, DataError> {
    let first = records
        .first()
        .ok_or_else(|| DataError::value("publication requires coverage records"))?;
    let scope = SymbolDayScope::new(first.asset_class, &first.symbol, first.session_date)?;
    validate(&scope, records)?;
    Ok(scope)
}

fn validate(scope: &SymbolDayScope, records: &[CoverageRecord]) -> Result<(), DataError> {
    let mut keys = std::collections::BTreeSet::new();
    for record in records {
        if (
            record.asset_class,
            record.symbol.as_str(),
            record.session_date,
        ) != (scope.asset_class, scope.symbol.as_str(), scope.session_date)
            || !keys.insert((record.data_type, record.interval.clone()))
        {
            return Err(DataError::value(
                "publication must contain unique datasets for one symbol-day",
            ));
        }
        if record.object_path.is_none()
            && (record.row_count != 0 || record.byte_size != 0 || record.checksum.is_some())
        {
            return Err(DataError::value(
                "coverage without an object cannot claim written rows, bytes or a checksum",
            ));
        }
        if let Some(path) = &record.object_path {
            let checksum = record
                .checksum
                .as_deref()
                .ok_or_else(|| DataError::value("published object requires checksum"))?;
            let logical = partition_path(
                record.session_date,
                record.asset_class,
                record.data_type,
                record.interval.as_deref(),
                &record.symbol,
            )?;
            if *path != immutable_path(&logical, checksum)? {
                return Err(DataError::value(
                    "outdated or mismatched publication object path",
                ));
            }
        }
    }
    Ok(())
}

fn decode(scope: &SymbolDayScope, bytes: &[u8]) -> Result<Vec<CoverageRecord>, DataError> {
    let records: Vec<CoverageRecord> = serde_json::from_slice(bytes).map_err(DataError::io)?;
    validate(scope, &records).map_err(|error| {
        DataError::Io(format!("outdated or corrupt publication manifest: {error}"))
    })?;
    Ok(records)
}

struct RenewingLease {
    lease: Arc<dyn Lease>,
    lost: Arc<AtomicBool>,
    stop: std::sync::mpsc::Sender<()>,
    worker: Option<std::thread::JoinHandle<()>>,
}
impl RenewingLease {
    fn new(lease: Box<dyn Lease>) -> Self {
        let lease: Arc<dyn Lease> = lease.into();
        let lost = Arc::new(AtomicBool::new(false));
        let (stop, receive) = std::sync::mpsc::channel();
        let worker_lease = lease.clone();
        let worker_lost = lost.clone();
        let worker = lease.renewal_interval().map(|interval| {
            std::thread::spawn(move || {
                while matches!(
                    receive.recv_timeout(interval),
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout)
                ) {
                    if worker_lease.renew().is_err() {
                        worker_lost.store(true, Ordering::Release);
                        break;
                    }
                }
            })
        });
        Self {
            lease,
            lost,
            stop,
            worker,
        }
    }
}
impl Drop for RenewingLease {
    fn drop(&mut self) {
        let _ = self.stop.send(());
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        if let Err(error) = self.lease.release() {
            eprintln!("publication lease release failed: {error}");
        }
    }
}

impl CatalogRepository for PublicationCatalog {
    fn get(
        &self,
        asset: AssetClass,
        data: DataType,
        interval: Option<&str>,
        symbol: &str,
        date: Date,
    ) -> Result<Option<CoverageRecord>, DataError> {
        Ok(self
            .day(&SymbolDayScope::new(asset, symbol, date)?)?
            .into_iter()
            .find(|r| r.data_type == data && r.interval.as_deref() == interval))
    }
    fn upsert(&self, record: &CoverageRecord) -> Result<(), DataError> {
        self.publish(std::slice::from_ref(record), || Ok(()))
    }
    fn list(&self, query: &CoverageQuery) -> Result<Vec<CoverageRecord>, DataError> {
        if query
            .start
            .zip(query.end)
            .is_some_and(|(start, end)| start > end)
        {
            return Err(DataError::value("start must be on or before end"));
        }
        let symbol = query.symbol.as_deref().map(canonical_symbol).transpose()?;
        let mut prefix = PUBLICATION_PREFIX.to_string();
        if let Some(asset) = query.asset_class {
            prefix.push_str(&format!("/asset_class={}", asset.value()));
            if let Some(symbol) = &symbol {
                prefix.push_str(&format!("/symbol={}", crate::table_key(symbol)));
            }
        }
        let mut rows = Vec::new();
        let mut paths = Vec::new();
        for path in self.objects.list_prefix(&prefix)? {
            if !path.starts_with(&format!("{prefix}/")) {
                continue;
            }
            if let Some(date) = path
                .rsplit_once("/date=")
                .and_then(|(_, d)| d.strip_suffix(".json"))
                .and_then(Date::parse)
            {
                if query.start.is_some_and(|v| v > date) || query.end.is_some_and(|v| v < date) {
                    continue;
                }
            } else {
                return Err(DataError::Io("invalid publication manifest path".into()));
            }
            paths.push(path);
        }
        let payloads = self.objects.read_many(&paths)?;
        if payloads.len() != paths.len() {
            return Err(DataError::Protocol(
                "manifest read batch acknowledgement mismatch".into(),
            ));
        }
        for (path, payload) in paths.into_iter().zip(payloads) {
            let candidates: Vec<CoverageRecord> =
                serde_json::from_slice(&payload).map_err(DataError::io)?;
            if let Some(first) = candidates.first() {
                let scope = scope_for_coverage(&candidates).map_err(|error| {
                    DataError::Io(format!("outdated or corrupt publication manifest: {error}"))
                })?;
                if path != format!("{PUBLICATION_PREFIX}/{}", scope.blob_name()) {
                    return Err(DataError::Io("publication scope mismatch".into()));
                }
                if query.asset_class.is_some_and(|v| v != first.asset_class)
                    || symbol.as_ref().is_some_and(|v| v != &first.symbol)
                    || query.start.is_some_and(|v| v > first.session_date)
                    || query.end.is_some_and(|v| v < first.session_date)
                {
                    continue;
                }
            }
            rows.extend(candidates.into_iter().filter(|r| {
                query.data_type.is_none_or(|v| v == r.data_type)
                    && query
                        .interval
                        .as_ref()
                        .is_none_or(|v| Some(v) == r.interval.as_ref())
            }));
        }
        rows.sort_by_key(|r| {
            (
                r.asset_class,
                r.data_type,
                r.symbol.clone(),
                r.session_date,
                r.interval.clone(),
            )
        });
        Ok(rows)
    }
}
