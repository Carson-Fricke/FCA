//! Arrow/Parquet writing, atomic object replacement, leases, and the capacity guard.
//!
//! `lake-storage` owns the value layer of the Python `lake.storage` package:
//! provider-independent blob ports, the atomic local filesystem store, the
//! schema-pinned Parquet writer, the storage capacity guard, and symbol-day
//! lease scopes. Provider-specific stores (Azure Blob, queues, tables) stay out
//! of this crate.

pub mod capacity;
pub mod leases;
pub mod local;
pub mod parquet;
pub mod ports;

pub use capacity::{
    sum_byte_sizes, StorageCapGuard, StorageCapPolicy, StorageCapState, StorageCapStatus,
    StorageOperation, StorageUsageProvider,
};
pub use leases::{Lease, LeaseProvider, SymbolDayScope};
pub use local::LocalFilesystemStore;
pub use parquet::{normalize_records, ParquetMetadata, ParquetWriter};
pub use ports::{BlobStore, ObjectStore, ReadableBlobStore, SasIssuer};
