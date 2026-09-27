//! Token-authenticated Azure Blob adapters for the market lake.
//!
//! Ports `lake.storage.azure`: whole-object replacement, the readable
//! prefix-owning variant, symbol-day lease acquisition over blob leases, and
//! read-only user-delegation SAS issuing. Each adapter is generic over a
//! small seam (`AzureBlobContainer`, `AzureSasBackend`) that the real
//! `azure_storage_blobs` clients implement, so hermetic tests can inject
//! fakes exactly like the Python suite duck-typed SDK clients.

use std::sync::{Arc, Mutex, MutexGuard, OnceLock};

use crate::{BlobStore, Lease, LeaseProvider, ReadableBlobStore, SasIssuer, SymbolDayScope};
use azure_core::auth::TokenCredential;
use azure_core::request_options::{LeaseDuration, LeaseId};
use azure_core::{Body, StatusCode};
use azure_storage::shared_access_signature::service_sas::BlobSasPermissions;
use azure_storage::shared_access_signature::SasToken;
use azure_storage::StorageCredentials;
use azure_storage_blobs::prelude::*;
use lake_contracts::DataError;
use time::OffsetDateTime;

/// The container surface the blob adapters are built over.
///
/// Mirrors the `ContainerClient` operations the Python adapters duck-type:
/// overwrite-aware upload, whole-object download, prefix listing and delete,
/// and lease maintenance for one blob.
pub trait AzureBlobContainer: Send + Sync {
    /// Upload `payload` to `path`, replacing any existing blob iff `overwrite`.
    fn upload(&self, path: &str, payload: &[u8], overwrite: bool) -> Result<(), DataError>;

    /// Return the full payload of one blob, or [`DataError::NotFound`].
    fn download(&self, path: &str) -> Result<Vec<u8>, DataError>;

    /// Return the blob names below `prefix`.
    fn list(&self, prefix: &str) -> Result<Vec<String>, DataError>;

    /// Delete one blob.
    fn delete(&self, path: &str) -> Result<(), DataError>;

    /// Acquire an exclusive lease and return its lease ID.
    fn acquire_lease(&self, path: &str, lease_duration_seconds: u32) -> Result<String, DataError>;

    /// Renew an existing lease.
    fn renew_lease(&self, path: &str, lease_id: &str) -> Result<(), DataError>;

    /// Release an existing lease.
    fn release_lease(&self, path: &str, lease_id: &str) -> Result<(), DataError>;
}

/// The service surface the SAS issuer is built over.
///
/// Mirrors the `BlobServiceClient` members the Python issuer uses: the
/// account identity, the canonical service URL, and user-delegation-key
/// backed signing of one blob token.
pub trait AzureSasBackend: Send + Sync {
    /// Return the storage account name.
    fn account_name(&self) -> Result<String, DataError>;

    /// Return the blob service URL, as configured.
    fn service_url(&self) -> Result<String, DataError>;

    /// Return a signed read-only SAS query token for one blob path.
    ///
    /// The real backend fetches a user-delegation key for the window and
    /// signs the token with it; fakes capture the window instead.
    fn signed_token(
        &self,
        object_path: &str,
        start: OffsetDateTime,
        expiry: OffsetDateTime,
    ) -> Result<String, DataError>;
}

/// How SDK-backed adapters drive async SDK futures from sync ports.
///
/// A dedicated executor thread owns the Tokio runtime, so SDK calls are safe
/// from ordinary sync threads, from inside another runtime's workers, and
/// from test tasks, and the runtime is never dropped in a context where
/// blocking is not allowed.
#[derive(Clone)]
struct Executor {
    sender: std::sync::mpsc::Sender<Task>,
}

type Task = std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'static>>;

impl Executor {
    fn shared() -> Self {
        static EXECUTOR: OnceLock<Executor> = OnceLock::new();
        EXECUTOR.get_or_init(Self::spawn).clone()
    }

    fn spawn() -> Self {
        let (sender, receiver) = std::sync::mpsc::channel::<Task>();
        let _ = std::thread::Builder::new()
            .name("azure-sdk-executor".to_string())
            .spawn(move || {
                let Ok(runtime) = tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .build()
                else {
                    return;
                };
                for task in receiver {
                    runtime.block_on(task);
                }
            });
        Self { sender }
    }

    fn block_on<F>(&self, future: F) -> Result<F::Output, DataError>
    where
        F: std::future::Future + Send + 'static,
        F::Output: Send + 'static,
    {
        let (sender, receiver) = std::sync::mpsc::channel();
        let task: Task = Box::pin(async move {
            let output = future.await;
            let _ = sender.send(output);
        });
        self.sender
            .send(task)
            .map_err(|_| DataError::io("the azure sdk executor is unavailable"))?;
        receiver
            .recv()
            .map_err(|_| DataError::io("the azure sdk executor stopped"))
    }
}

fn is_http_status(error: &azure_core::Error, status: StatusCode) -> bool {
    error
        .as_http_error()
        .is_some_and(|http_error| http_error.status() == status)
}

fn http_error(error: azure_core::Error) -> DataError {
    if is_http_status(&error, StatusCode::NotFound) {
        return DataError::NotFound("missing".to_string());
    }
    if is_http_status(&error, StatusCode::Conflict) {
        return DataError::Conflict(error.to_string());
    }
    DataError::Io(error.to_string())
}

/// Reject paths that are not non-empty relative POSIX object paths.
///
/// Ports `_object_path`: no backslashes, no leading slash, and no empty,
/// `.`, or `..` segments.
fn validate_object_path(object_path: &str) -> Result<(), DataError> {
    if object_path.is_empty()
        || object_path.contains('\\')
        || object_path.starts_with('/')
        || object_path
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(DataError::value(
            "object path must be a non-empty relative POSIX path",
        ));
    }
    Ok(())
}

/// Reject prefixes that are not non-empty relative POSIX prefixes.
///
/// Ports `_relative_prefix` from `lake.storage.leases`.
fn validate_relative_prefix(prefix: &str) -> Result<String, DataError> {
    if prefix.is_empty()
        || prefix.contains('\\')
        || prefix.starts_with('/')
        || prefix
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(DataError::value(
            "blob prefix must be a non-empty relative POSIX path",
        ));
    }
    Ok(prefix.trim_end_matches('/').to_string())
}

/// Percent-encode like Python's `quote(value, safe=safe)`.
fn quote(value: &str, safe: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric()
            || matches!(byte, b'-' | b'.' | b'_' | b'~')
            || safe.as_bytes().contains(&byte)
        {
            encoded.push(char::from(byte));
        } else {
            encoded.push('%');
            encoded.push_str(&format!("{byte:02X}"));
        }
    }
    encoded
}

/// Whole-object replacement store backed by one container.
pub struct AzureBlobStore {
    container: Arc<dyn AzureBlobContainer>,
}

impl AzureBlobStore {
    /// Build the store over an injected container client.
    pub fn new(container: Arc<dyn AzureBlobContainer>) -> Self {
        Self { container }
    }

    /// Build the store from an account URL with a caller-supplied credential.
    pub fn from_account_url(
        account_url: &str,
        container_name: &str,
        credential: Arc<dyn TokenCredential>,
    ) -> Result<Self, DataError> {
        Ok(Self {
            container: Arc::new(SdkBlobContainer::from_account_url(
                account_url,
                container_name,
                credential,
            )?),
        })
    }
}

impl BlobStore for AzureBlobStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        let path = relative_path.to_string();
        validate_object_path(&path)?;
        self.container.upload(&path, payload, true)
    }
}

/// Blob store that also reads, lists, and deletes whole prefixes.
///
/// Backs consumers owning entire object prefixes, such as the backtest
/// results store; the replace-only [`AzureBlobStore`] stays read-free for the
/// Parquet writer.
pub struct AzureReadableBlobStore {
    container: Arc<dyn AzureBlobContainer>,
}

impl AzureReadableBlobStore {
    /// Build the readable store over an injected container client.
    pub fn new(container: Arc<dyn AzureBlobContainer>) -> Self {
        Self { container }
    }

    /// Build the readable store from an account URL with a caller credential.
    pub fn from_account_url(
        account_url: &str,
        container_name: &str,
        credential: Arc<dyn TokenCredential>,
    ) -> Result<Self, DataError> {
        Ok(Self {
            container: Arc::new(SdkBlobContainer::from_account_url(
                account_url,
                container_name,
                credential,
            )?),
        })
    }
}

impl BlobStore for AzureReadableBlobStore {
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError> {
        let path = relative_path.to_string();
        validate_object_path(&path)?;
        self.container.upload(&path, payload, true)
    }
}

impl ReadableBlobStore for AzureReadableBlobStore {
    fn read(&self, relative_path: &str) -> Result<Vec<u8>, DataError> {
        let path = relative_path.to_string();
        validate_object_path(&path)?;
        match self.container.download(&path) {
            Ok(payload) => Ok(payload),
            Err(DataError::NotFound(_)) => {
                Err(DataError::NotFound(format!("object not found: {path}")))
            }
            Err(error) => Err(error),
        }
    }

    fn list_prefix(&self, prefix: &str) -> Result<Vec<String>, DataError> {
        let mut names = self.container.list(prefix)?;
        names.sort();
        Ok(names)
    }

    fn delete_prefix(&self, prefix: &str) -> Result<u64, DataError> {
        let names = self.container.list(prefix)?;
        let count = names.len() as u64;
        for name in &names {
            self.container.delete(name)?;
        }
        Ok(count)
    }
}

/// Safe facade over one acquired blob lease.
pub struct AzureBlobLease {
    container: Arc<dyn AzureBlobContainer>,
    path: String,
    lease_id: String,
    released: Mutex<bool>,
}

impl AzureBlobLease {
    fn lock(&self) -> MutexGuard<'_, bool> {
        self.released
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Lease for AzureBlobLease {
    fn lease_id(&self) -> Option<&str> {
        Some(&self.lease_id)
    }

    fn renew(&self) -> Result<(), DataError> {
        if *self.lock() {
            return Err(DataError::Conflict("cannot renew a released lease".into()));
        }
        self.container.renew_lease(&self.path, &self.lease_id)
    }

    fn release(&self) -> Result<(), DataError> {
        let mut released = self.lock();
        if *released {
            return Ok(());
        }
        self.container.release_lease(&self.path, &self.lease_id)?;
        *released = true;
        Ok(())
    }
}

/// Acquire symbol-day leases from token-authenticated Azure Blob Storage.
pub struct AzureBlobLeaseProvider {
    container: Arc<dyn AzureBlobContainer>,
    lease_duration_seconds: u32,
    blob_prefix: String,
}

impl std::fmt::Debug for AzureBlobLeaseProvider {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AzureBlobLeaseProvider")
            .field("lease_duration_seconds", &self.lease_duration_seconds)
            .field("blob_prefix", &self.blob_prefix)
            .finish()
    }
}

const MIN_LEASE_SECONDS: u32 = 15;
const MAX_LEASE_SECONDS: u32 = 60;

impl AzureBlobLeaseProvider {
    /// Build the provider over an injected container client.
    pub fn new(
        container: Arc<dyn AzureBlobContainer>,
        lease_duration_seconds: u32,
        blob_prefix: &str,
    ) -> Result<Self, DataError> {
        if !(MIN_LEASE_SECONDS..=MAX_LEASE_SECONDS).contains(&lease_duration_seconds) {
            return Err(DataError::value(
                "lease duration must be between 15 and 60 seconds",
            ));
        }
        let blob_prefix = validate_relative_prefix(blob_prefix)?;
        Ok(Self {
            container,
            lease_duration_seconds,
            blob_prefix,
        })
    }

    /// Build the provider from an account URL with a caller credential.
    pub fn from_account_url(
        account_url: &str,
        container_name: &str,
        credential: Arc<dyn TokenCredential>,
        lease_duration_seconds: u32,
        blob_prefix: &str,
    ) -> Result<Self, DataError> {
        let container = Arc::new(SdkBlobContainer::from_account_url(
            account_url,
            container_name,
            credential,
        )?);
        Self::new(container, lease_duration_seconds, blob_prefix)
    }

    fn blob_name(&self, scope: &SymbolDayScope) -> String {
        format!("{}/{}", self.blob_prefix, scope.blob_name())
    }
}

impl LeaseProvider for AzureBlobLeaseProvider {
    fn acquire(&self, scope: &SymbolDayScope) -> Result<Box<dyn Lease>, DataError> {
        let path = self.blob_name(scope);
        // Create the lock blob if missing; an empty overwrite race is benign
        // because only the lease acquisition grants exclusivity. The Rust SDK
        // 0.21 builders expose no If-None-Match condition for put-blob, so the
        // Python `upload_blob(b"", overwrite=False)` existence check becomes a
        // properties probe.
        match self.container.download(&path) {
            Ok(_) => {}
            Err(DataError::NotFound(_)) => {
                self.container.upload(&path, b"", true)?;
            }
            Err(error) => return Err(error),
        }
        let lease_id = self
            .container
            .acquire_lease(&path, self.lease_duration_seconds)?;
        Ok(Box::new(AzureBlobLease {
            container: Arc::clone(&self.container),
            path,
            lease_id,
            released: Mutex::new(false),
        }))
    }
}

/// Issue read-only user-delegation SAS URLs for individual blob objects.
pub struct AzureBlobSasIssuer {
    backend: Arc<dyn AzureSasBackend>,
    container_name: String,
    ttl_seconds: i64,
    clock: Arc<dyn Fn() -> OffsetDateTime + Send + Sync>,
}

const MAX_SAS_TTL_SECONDS: i64 = 86_400;
const DEFAULT_SAS_TTL_SECONDS: i64 = 900;

impl std::fmt::Debug for AzureBlobSasIssuer {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("AzureBlobSasIssuer")
            .field("container_name", &self.container_name)
            .field("ttl_seconds", &self.ttl_seconds)
            .finish()
    }
}

impl AzureBlobSasIssuer {
    /// Build the issuer with the default 900-second TTL.
    pub fn new(backend: Arc<dyn AzureSasBackend>, container_name: &str) -> Result<Self, DataError> {
        Self::build(backend, container_name, DEFAULT_SAS_TTL_SECONDS)
    }

    /// Build the issuer with an explicit TTL in seconds.
    pub fn with_ttl_seconds(
        backend: Arc<dyn AzureSasBackend>,
        container_name: &str,
        ttl_seconds: i64,
    ) -> Result<Self, DataError> {
        Self::build(backend, container_name, ttl_seconds)
    }

    fn build(
        backend: Arc<dyn AzureSasBackend>,
        container_name: &str,
        ttl_seconds: i64,
    ) -> Result<Self, DataError> {
        if !(1..=MAX_SAS_TTL_SECONDS).contains(&ttl_seconds) {
            return Err(DataError::value(
                "SAS TTL must be between 1 and 86400 seconds",
            ));
        }
        if container_name.is_empty() {
            return Err(DataError::value("container name must not be empty"));
        }
        Ok(Self {
            backend,
            container_name: container_name.to_string(),
            ttl_seconds,
            clock: Arc::new(OffsetDateTime::now_utc),
        })
    }

    /// Replace the issuance clock.
    #[must_use]
    pub fn with_clock(mut self, clock: Arc<dyn Fn() -> OffsetDateTime + Send + Sync>) -> Self {
        self.clock = clock;
        self
    }
}

impl SasIssuer for AzureBlobSasIssuer {
    fn issue(&self, object_path: &str) -> Result<String, DataError> {
        validate_object_path(object_path)?;
        let issued_at = (self.clock)();
        let expires_at = issued_at + time::Duration::seconds(self.ttl_seconds);
        let token = self
            .backend
            .signed_token(object_path, issued_at, expires_at)?;
        let base = self.backend.service_url()?;
        let container = quote(&self.container_name, "-._~");
        let blob = quote(object_path, "/-._~=");
        Ok(format!(
            "{}/{}/{}?{}",
            base.trim_end_matches('/'),
            container,
            blob,
            token
        ))
    }
}

/// Extract the storage account name from a blob endpoint URL.
fn account_from_url(account_url: &str) -> Result<String, DataError> {
    let host = url_host(account_url)?;
    let host = host
        .strip_suffix(".blob.core.windows.net")
        .unwrap_or(&host)
        .to_string();
    if host.is_empty() {
        return Err(DataError::value(
            "blob service URL must contain an account name",
        ));
    }
    Ok(host)
}

fn url_host(account_url: &str) -> Result<String, DataError> {
    let trimmed = account_url.trim().trim_end_matches('/');
    let rest = trimmed
        .split_once("://")
        .map(|(_, rest)| rest)
        .unwrap_or(trimmed);
    let host = rest
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default()
        .to_string();
    if host.is_empty() {
        return Err(DataError::value("blob service URL must contain a host"));
    }
    Ok(host)
}

/// SDK-backed container seam over `azure_storage_blobs`.
struct SdkBlobContainer {
    container: ContainerClient,
    executor: Executor,
}

impl SdkBlobContainer {
    fn from_account_url(
        account_url: &str,
        container_name: &str,
        credential: Arc<dyn TokenCredential>,
    ) -> Result<Self, DataError> {
        let account = account_from_url(account_url)?;
        let credentials = StorageCredentials::token_credential(credential);
        let container = BlobServiceClient::builder(account, credentials)
            .blob_service_client()
            .container_client(container_name);
        Ok(Self {
            container,
            executor: Executor::shared(),
        })
    }
}

impl AzureBlobContainer for SdkBlobContainer {
    fn upload(&self, path: &str, payload: &[u8], _overwrite: bool) -> Result<(), DataError> {
        let client = self.container.blob_client(path);
        let future = client
            .put_block_blob(Body::from(payload.to_vec()))
            .into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }

    fn download(&self, path: &str) -> Result<Vec<u8>, DataError> {
        let client = self.container.blob_client(path);
        // `get_content` borrows the client, so the future owns it.
        self.executor
            .block_on(async move { client.get_content().await })?
            .map_err(http_error)
    }

    fn list(&self, prefix: &str) -> Result<Vec<String>, DataError> {
        use futures_util::StreamExt;
        let stream = self
            .container
            .list_blobs()
            .prefix(prefix.to_string())
            .into_stream();
        let pages = self.executor.block_on(stream.collect::<Vec<_>>())?;
        let mut names = Vec::new();
        for page in pages {
            let page = page.map_err(http_error)?;
            names.extend(page.blobs.blobs().map(|blob| blob.name.clone()));
        }
        Ok(names)
    }

    fn delete(&self, path: &str) -> Result<(), DataError> {
        let client = self.container.blob_client(path);
        let future = client.delete().into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }

    fn acquire_lease(&self, path: &str, lease_duration_seconds: u32) -> Result<String, DataError> {
        let client = self.container.blob_client(path);
        let future = client
            .acquire_lease(LeaseDuration::Seconds(lease_duration_seconds as u8))
            .into_future();
        let response = self.executor.block_on(future)?.map_err(http_error)?;
        Ok(response.lease_id.to_string())
    }

    fn renew_lease(&self, path: &str, lease_id: &str) -> Result<(), DataError> {
        let lease = self.lease_client(path, lease_id)?;
        let future = lease.renew().into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }

    fn release_lease(&self, path: &str, lease_id: &str) -> Result<(), DataError> {
        let lease = self.lease_client(path, lease_id)?;
        let future = lease.release().into_future();
        self.executor
            .block_on(future)?
            .map(|_| ())
            .map_err(http_error)
    }
}

impl SdkBlobContainer {
    fn lease_client(&self, path: &str, lease_id: &str) -> Result<BlobLeaseClient, DataError> {
        let parsed: LeaseId = lease_id
            .parse()
            .map_err(|error| DataError::value(format!("invalid lease id: {error}")))?;
        Ok(self.container.blob_client(path).blob_lease_client(parsed))
    }
}

/// SDK-backed SAS backend over `azure_storage_blobs`.
struct SdkSasBackend {
    service: BlobServiceClient,
    container_name: String,
    executor: Executor,
}

impl SdkSasBackend {
    fn from_account_url(
        account_url: &str,
        container_name: &str,
        credential: Arc<dyn TokenCredential>,
    ) -> Result<Self, DataError> {
        let account = account_from_url(account_url)?;
        let credentials = StorageCredentials::token_credential(credential);
        let service = BlobServiceClient::builder(account, credentials).blob_service_client();
        Ok(Self {
            service,
            container_name: container_name.to_string(),
            executor: Executor::shared(),
        })
    }
}

impl AzureSasBackend for SdkSasBackend {
    fn account_name(&self) -> Result<String, DataError> {
        Ok(self.service.account().to_string())
    }

    fn service_url(&self) -> Result<String, DataError> {
        let url = self
            .service
            .url()
            .map_err(|error| DataError::Io(error.to_string()))?;
        Ok(url.to_string())
    }

    fn signed_token(
        &self,
        object_path: &str,
        start: OffsetDateTime,
        expiry: OffsetDateTime,
    ) -> Result<String, DataError> {
        let service = self.service.clone();
        let container_name = self.container_name.clone();
        let blob = object_path.to_string();
        let token = self.executor.block_on(async move {
            let response = service
                .get_user_deligation_key(start, expiry)
                .await
                .map_err(http_error)?;
            let key = response.user_deligation_key;
            let permissions = BlobSasPermissions {
                read: true,
                ..BlobSasPermissions::default()
            };
            let signature = service
                .container_client(container_name)
                .blob_client(blob)
                .user_delegation_shared_access_signature(permissions, &key)
                .await
                .map_err(http_error)?;
            signature
                .token()
                .map_err(|error| DataError::Io(error.to_string()))
        })??;
        Ok(token)
    }
}

/// Convenience constructor wiring the SDK SAS backend into the issuer.
///
/// Mirrors `AzureBlobSasIssuer.from_account_url`.
pub fn sas_issuer_from_account_url(
    account_url: &str,
    container_name: &str,
    credential: Arc<dyn TokenCredential>,
    ttl_seconds: i64,
) -> Result<AzureBlobSasIssuer, DataError> {
    let backend = Arc::new(SdkSasBackend::from_account_url(
        account_url,
        container_name,
        credential,
    )?);
    AzureBlobSasIssuer::with_ttl_seconds(backend, container_name, ttl_seconds)
}

#[cfg(test)]
#[path = "../tests/unit/azure.rs"]
mod tests;
