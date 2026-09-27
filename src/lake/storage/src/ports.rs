//! Provider-independent storage ports.
//!
//! The ports mirror the Python `lake.storage.ports` protocols: whole-object
//! replacement for the Parquet writer, an extended read/prefix boundary for
//! consumers that own whole prefixes, the read-only SAS issuer used by the
//! service boundary, and the queue send boundary used by dead-letter transfer
//! and operator replay.

use futures_util::future::BoxFuture;
use lake_contracts::DataError;
use serde_json::Value;

/// Whole-object replacement boundary used by the Parquet writer.
///
/// `replace` has whole-object semantics: a successful call makes the supplied
/// payload the complete contents at `relative_path`.
pub trait BlobStore: Send + Sync {
    /// Atomically make `payload` the complete contents at `relative_path`.
    fn replace(&self, relative_path: &str, payload: &[u8]) -> Result<(), DataError>;
}

/// Blob boundary that also owns whole prefixes.
///
/// Extends the replace-only [`BlobStore`] used by the Parquet writer with the
/// read, prefix-list, and prefix-delete operations required by consumers that
/// manage entire object prefixes, such as the backtest results store.
pub trait ReadableBlobStore: BlobStore {
    /// Return the full payload of one object.
    fn read(&self, relative_path: &str) -> Result<Vec<u8>, DataError>;

    /// Return the object paths below `prefix`.
    fn list_prefix(&self, prefix: &str) -> Result<Vec<String>, DataError>;

    /// Remove every object below `prefix` and return how many went away.
    fn delete_prefix(&self, prefix: &str) -> Result<u64, DataError>;
}

/// Compatibility alias for the Python `ObjectStore` naming.
pub trait ObjectStore: BlobStore {}

impl<T: BlobStore + ?Sized> ObjectStore for T {}

/// Boundary for issuing a read-only URL for one object.
pub trait SasIssuer: Send + Sync {
    /// Issue a read-only access URL for `object_path`.
    fn issue(&self, object_path: &str) -> Result<String, DataError>;
}

/// Send boundary for one queue message.
///
/// Mirrors the Python `lake.storage.ports.QueueSender` protocol: one JSON
/// object message per call. The Azure queue adapter arrives with the hosted
/// phase.
pub trait QueueSender: Send + Sync {
    /// Send one JSON-object queue message.
    fn send<'a>(&'a self, message: &'a Value) -> BoxFuture<'a, Result<(), DataError>>;
}
