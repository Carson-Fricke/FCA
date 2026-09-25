//! Arrow/Parquet writing, atomic object replacement, leases, and the capacity guard.
//!
//! `lake-storage` owns the value layer of the Python `lake.storage` package:
//! provider-independent blob ports, the atomic local filesystem store, the
//! schema-pinned Parquet writer, the storage capacity guard, symbol-day
//! lease scopes, and the token-authenticated Azure Blob adapters.

pub mod azure;
pub mod capacity;
pub mod leases;
pub mod local;
pub mod parquet;
pub mod ports;

pub use azure::{
    sas_issuer_from_account_url, AzureBlobContainer, AzureBlobLease, AzureBlobLeaseProvider,
    AzureBlobSasIssuer, AzureBlobStore, AzureReadableBlobStore, AzureSasBackend,
};

pub use capacity::{
    sum_byte_sizes, StorageCapGuard, StorageCapPolicy, StorageCapState, StorageCapStatus,
    StorageOperation, StorageUsageProvider,
};
pub use leases::{Lease, LeaseProvider, SymbolDayScope};
pub use local::LocalFilesystemStore;
pub use parquet::{normalize_records, ParquetMetadata, ParquetWriter};
pub use ports::{BlobStore, ObjectStore, QueueSender, ReadableBlobStore, SasIssuer};
