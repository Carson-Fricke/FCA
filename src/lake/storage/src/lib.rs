//! Arrow/Parquet writing, atomic object replacement, leases, the capacity
//! guard, and the streaming Arrow IPC read helper.
//!
//! `lake-storage` owns the value layer of the Python `lake.storage` package:
//! provider-independent blob ports, the atomic local filesystem store, the
//! schema-pinned Parquet writer, the storage capacity guard, symbol-day
//! lease scopes, the token-authenticated Azure Blob adapters, and the
//! incremental Arrow IPC stream encoder over ensured partitions.

pub mod azure;
pub mod capacity;
pub mod leases;
pub mod local;
pub mod parquet;
pub mod parquet_read;
pub mod ports;
pub mod stream;

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
pub use parquet_read::{
    read_crypto_trades, read_equity_trades, visit_partition_batches, CryptoTradeRecord,
    EquityTradeRecord, TradePartition,
};
pub use ports::{BlobStore, QueueSender, ReadableBlobStore, SasIssuer};
pub use stream::write_dataset_ipc_stream;
