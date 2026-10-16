//! Canonical Parquet writing and provider-independent blob, lease and capacity adapters. Market-data decoding belongs to harness-market-reader.

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
pub use parquet::{ParquetMetadata, ParquetWriter};
pub use ports::{BlobStore, ReadableBlobStore, SasIssuer};
