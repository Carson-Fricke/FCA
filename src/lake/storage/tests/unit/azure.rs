//! Hermetic tests for the Azure blob adapters, ported from
//! `tests/lake/storage/test_azure.py`, `test_azure_readable.py`, and the
//! lease cases of `test_leases.py`.

use std::collections::BTreeMap;
use std::sync::Mutex;

use super::*;
use lake_contracts::{AssetClass, Date};

const MISSING: &str = "missing";

fn session_date() -> Date {
    Date::from_ymd(2024, 1, 19).expect("session date")
}

/// Records uploads like the Python `FakeBlob`.
#[derive(Default)]
struct FakeBlob {
    uploads: Vec<(Vec<u8>, bool)>,
}

/// Duck-typed `ContainerClient` fake mirroring the Python tests.
#[derive(Default)]
struct FakeContainer {
    blobs: Mutex<BTreeMap<String, FakeBlob>>,
    listings: Mutex<Vec<String>>,
    deletions: Mutex<Vec<String>>,
}

impl FakeContainer {
    fn payload_of(&self, path: &str) -> Option<Vec<u8>> {
        self.blobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(path)
            .and_then(|blob| blob.uploads.last().map(|(payload, _)| payload.clone()))
    }
}

impl AzureBlobContainer for FakeContainer {
    fn upload(&self, path: &str, payload: &[u8], overwrite: bool) -> Result<(), DataError> {
        let mut blobs = self
            .blobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let blob = blobs.entry(path.to_string()).or_default();
        if !overwrite && !blob.uploads.is_empty() {
            return Err(DataError::Conflict("blob exists".into()));
        }
        blob.uploads.push((payload.to_vec(), overwrite));
        Ok(())
    }

    fn download(&self, path: &str) -> Result<Vec<u8>, DataError> {
        self.payload_of(path)
            .ok_or_else(|| DataError::NotFound(MISSING.to_string()))
    }

    fn list(&self, prefix: &str) -> Result<Vec<String>, DataError> {
        self.listings
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(prefix.to_string());
        let blobs = self
            .blobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Ok(blobs
            .keys()
            .filter(|path| path.starts_with(prefix))
            .cloned()
            .collect())
    }

    fn delete(&self, path: &str) -> Result<(), DataError> {
        self.deletions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(path.to_string());
        self.blobs
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(path);
        Ok(())
    }

    fn acquire_lease(&self, path: &str, _lease_duration_seconds: u32) -> Result<String, DataError> {
        Ok(format!("lease-{path}"))
    }

    fn renew_lease(&self, path: &str, lease_id: &str) -> Result<(), DataError> {
        if lease_id != format!("lease-{path}") {
            return Err(DataError::Conflict("lease lost".into()));
        }
        Ok(())
    }

    fn release_lease(&self, _path: &str, _lease_id: &str) -> Result<(), DataError> {
        Ok(())
    }
}

/// SAS backend fake capturing the requested signing window.
struct FakeSasBackend {
    calls: Mutex<Vec<(String, OffsetDateTime, OffsetDateTime)>>,
    service_url: String,
}

impl FakeSasBackend {
    fn new(service_url: &str) -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            service_url: service_url.to_string(),
        }
    }

    fn calls(&self) -> Vec<(String, OffsetDateTime, OffsetDateTime)> {
        self.calls
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }
}

impl AzureSasBackend for FakeSasBackend {
    fn account_name(&self) -> Result<String, DataError> {
        Ok("lakeaccount".to_string())
    }

    fn service_url(&self) -> Result<String, DataError> {
        Ok(self.service_url.clone())
    }

    fn signed_token(
        &self,
        object_path: &str,
        start: OffsetDateTime,
        expiry: OffsetDateTime,
    ) -> Result<String, DataError> {
        self.calls
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push((object_path.to_string(), start, expiry));
        Ok("sv=fake-signature".to_string())
    }
}

fn issued_at() -> OffsetDateTime {
    OffsetDateTime::from_unix_timestamp(1_705_665_600).expect("fixed instant") // 2024-01-19T12:00Z
}

#[test]
fn blob_store_replaces_objects_without_exposing_reads() {
    let container = Arc::new(FakeContainer::default());
    let adapter = AzureBlobStore::new(Arc::clone(&container) as Arc<dyn AzureBlobContainer>);

    adapter
        .replace("date=2024-01-19/AAPL.parquet", b"new")
        .expect("first replace");
    adapter
        .replace("date=2024-01-19/AAPL.parquet", b"replacement")
        .expect("second replace");

    let blobs = container
        .blobs
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let uploads = &blobs["date=2024-01-19/AAPL.parquet"].uploads;
    assert_eq!(
        uploads,
        &[(b"new".to_vec(), true), (b"replacement".to_vec(), true)]
    );
    // The replace-only store implements the replace boundary; the readable
    // extension lives on `AzureReadableBlobStore`, exactly like Python's
    // read-free `AzureBlobStore`.
    fn assert_blob_store<T: BlobStore>(_: &T) {}
    assert_blob_store(&adapter);
}

#[test]
fn blob_store_rejects_paths_that_are_not_relative_objects() {
    let adapter = AzureBlobStore::new(Arc::new(FakeContainer::default()));

    for path in ["", "/lake/file", "../file", "lake/../file", "lake\\file"] {
        let error = adapter.replace(path, b"payload").expect_err("must reject");
        assert!(
            matches!(error, DataError::ValueError(ref message) if message.contains("relative POSIX")),
            "unexpected error for {path:?}: {error:?}"
        );
    }
}

#[test]
fn sas_issuer_uses_user_delegation_and_one_read_only_object() {
    let backend = Arc::new(FakeSasBackend::new(
        "https://lakeaccount.blob.core.windows.net/",
    ));
    let issuer = AzureBlobSasIssuer::with_ttl_seconds(
        Arc::clone(&backend) as Arc<dyn AzureSasBackend>,
        "lake",
        900,
    )
    .expect("issuer")
    .with_clock(Arc::new(issued_at));

    let result = issuer.issue("date=2024-01-19/AAPL.parquet").expect("issue");

    assert_eq!(
        result,
        "https://lakeaccount.blob.core.windows.net/lake/date=2024-01-19/AAPL.parquet?sv=fake-signature"
    );
    let calls = backend.calls();
    assert_eq!(calls.len(), 1);
    let (blob, start, expiry) = &calls[0];
    assert_eq!(blob, "date=2024-01-19/AAPL.parquet");
    assert_eq!(*start, issued_at());
    assert_eq!(*expiry, issued_at() + time::Duration::seconds(900));
}

#[test]
fn sas_issuer_url_matches_python_quoting() {
    let backend = Arc::new(FakeSasBackend::new(
        "https://lakeaccount.blob.core.windows.net",
    ));
    let issuer = AzureBlobSasIssuer::new(Arc::clone(&backend) as Arc<dyn AzureSasBackend>, "lake")
        .expect("issuer")
        .with_clock(Arc::new(issued_at));

    let result = issuer.issue("date=2024-01-19/AAPL.parquet").expect("issue");

    assert_eq!(
        result,
        "https://lakeaccount.blob.core.windows.net/lake/date=2024-01-19/AAPL.parquet?sv=fake-signature"
    );
}

#[test]
fn sas_issuer_rejects_long_ttl_and_invalid_object() {
    let backend: Arc<dyn AzureSasBackend> = Arc::new(FakeSasBackend::new(
        "https://lakeaccount.blob.core.windows.net",
    ));

    let error = AzureBlobSasIssuer::with_ttl_seconds(Arc::clone(&backend), "lake", 86_401)
        .expect_err("must reject long TTL");
    assert!(
        matches!(error, DataError::ValueError(ref message) if message.contains("between 1 and 86400"))
    );
    let error = AzureBlobSasIssuer::with_ttl_seconds(Arc::clone(&backend), "lake", 0)
        .expect_err("must reject zero TTL");
    assert!(
        matches!(error, DataError::ValueError(ref message) if message.contains("between 1 and 86400"))
    );
    let error =
        AzureBlobSasIssuer::new(Arc::clone(&backend), "").expect_err("must reject empty container");
    assert!(
        matches!(error, DataError::ValueError(ref message) if message.contains("container name"))
    );

    let issuer = AzureBlobSasIssuer::new(backend, "lake").expect("issuer");
    let error = issuer.issue("../outside.parquet").expect_err("must reject");
    assert!(
        matches!(error, DataError::ValueError(ref message) if message.contains("relative POSIX"))
    );
}

#[test]
fn readable_blob_store_replaces_reads_lists_and_deletes_prefixes() {
    let container = Arc::new(FakeContainer::default());
    let adapter =
        AzureReadableBlobStore::new(Arc::clone(&container) as Arc<dyn AzureBlobContainer>);

    adapter
        .replace("backtests/run-1/actions.parquet", b"actions")
        .expect("replace actions");
    adapter
        .replace("backtests/run-1/fills.parquet", b"fills")
        .expect("replace fills");
    adapter
        .replace("backtests/run-2/fills.parquet", b"other")
        .expect("replace other");

    assert_eq!(
        adapter
            .read("backtests/run-1/actions.parquet")
            .expect("read"),
        b"actions".to_vec()
    );
    assert_eq!(
        adapter.list_prefix("backtests/run-1/").expect("list"),
        vec![
            "backtests/run-1/actions.parquet",
            "backtests/run-1/fills.parquet"
        ]
    );
    assert_eq!(
        adapter.delete_prefix("backtests/run-1/").expect("delete"),
        2
    );
    assert_eq!(
        adapter
            .list_prefix("backtests/run-1/")
            .expect("list emptied"),
        Vec::<String>::new()
    );
    assert_eq!(
        adapter.list_prefix("backtests/").expect("list rest"),
        vec!["backtests/run-2/fills.parquet"]
    );
    let listings = container
        .listings
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    assert_eq!(
        *listings,
        vec![
            "backtests/run-1/",
            "backtests/run-1/",
            "backtests/run-1/",
            "backtests/"
        ]
    );
    let deletions = container
        .deletions
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    assert_eq!(
        *deletions,
        vec![
            "backtests/run-1/actions.parquet",
            "backtests/run-1/fills.parquet"
        ]
    );
    // The readable store still satisfies the replace-only boundary.
    fn assert_readable<T: ReadableBlobStore>(_: &T) {}
    assert_readable(&adapter);
}

#[test]
fn readable_blob_store_missing_reads_raise_not_found() {
    let adapter = AzureReadableBlobStore::new(Arc::new(FakeContainer::default()));

    let error = adapter
        .read("backtests/run-1/fills.parquet")
        .expect_err("must fail");
    assert!(
        matches!(error, DataError::NotFound(ref message) if message.contains("object not found")),
        "unexpected error: {error:?}"
    );
}

#[test]
fn readable_blob_store_rejects_paths_that_are_not_relative_objects() {
    let adapter = AzureReadableBlobStore::new(Arc::new(FakeContainer::default()));

    for path in ["", "/lake/file", "../file", "lake/../file", "lake\\file"] {
        let error = adapter.read(path).expect_err("must reject");
        assert!(
            matches!(error, DataError::ValueError(ref message) if message.contains("relative POSIX")),
            "unexpected error for {path:?}: {error:?}"
        );
    }
}

#[test]
fn lease_provider_acquires_renews_and_releases_once() {
    let container = Arc::new(FakeContainer::default());
    let provider = AzureBlobLeaseProvider::new(
        Arc::clone(&container) as Arc<dyn AzureBlobContainer>,
        60,
        "locks",
    )
    .expect("provider");

    let scope = SymbolDayScope::new(AssetClass::Equity, "AAPL", session_date()).expect("scope");
    let lease = provider.acquire(&scope).expect("acquire");

    assert_eq!(
        lease.lease_id(),
        Some("lease-locks/date=2024-01-19/asset_class=equity/AAPL.lock")
    );
    // The lock blob was created empty with overwrite semantics recorded.
    assert_eq!(
        container.payload_of("locks/date=2024-01-19/asset_class=equity/AAPL.lock"),
        Some(Vec::<u8>::new())
    );

    lease.renew().expect("renew");
    lease.release().expect("release");
    // Release is idempotent.
    lease.release().expect("release again");
    let error = lease.renew().expect_err("renew after release must fail");
    assert!(
        matches!(error, DataError::Conflict(ref message) if message.contains("cannot renew a released lease")),
        "unexpected error: {error:?}"
    );
}

#[test]
fn lease_provider_rejects_bad_duration_and_prefix() {
    let container: Arc<dyn AzureBlobContainer> = Arc::new(FakeContainer::default());

    for duration in [14, 61] {
        let error = AzureBlobLeaseProvider::new(Arc::clone(&container), duration, "locks")
            .expect_err("must reject duration");
        assert!(
            matches!(error, DataError::ValueError(ref message) if message.contains("between 15 and 60")),
            "unexpected error for {duration}: {error:?}"
        );
    }

    for prefix in ["", "/locks", "locks\\inner", "locks//x", "./locks"] {
        let error = AzureBlobLeaseProvider::new(Arc::clone(&container), 60, prefix)
            .expect_err("must reject prefix");
        assert!(
            matches!(error, DataError::ValueError(ref message) if message.contains("relative POSIX")),
            "unexpected error for {prefix:?}: {error:?}"
        );
    }
}
