//! Incremental Arrow IPC stream encoding over ensured lake partitions.
//!
//! The published read route streams the frozen dataset schema as one Arrow IPC
//! stream: the schema message first, then every record batch of every ensured
//! partition in guaranteed-file order, then the end-of-stream marker. Partitions
//! are decoded lazily through [`crate::parquet_read::visit_partition_batches`],
//! so memory stays bounded to one object payload plus one decoded batch and no
//! caller ever holds all sessions at once.

use std::io::Write;

use arrow_ipc::writer::StreamWriter;
use lake_contracts::{schema_for, AssetClass, DataError, DataType};

use crate::parquet_read::visit_partition_batches;
use crate::ports::ReadableBlobStore;

/// Write the frozen dataset schema and every ensured partition as one IPC stream.
///
/// The schema is the pinned `schema_for(asset_class, data_type)` schema with its
/// `lake.schema` and `lake.schema.version` metadata; batches are written in
/// object order (session order for a read route plan) and in stored row order.
/// The stream is uncompressed and terminated by the standard end-of-stream
/// marker.
pub fn write_dataset_ipc_stream<W: Write>(
    store: &dyn ReadableBlobStore,
    object_paths: &[String],
    asset_class: AssetClass,
    data_type: DataType,
    writer: W,
) -> Result<(), DataError> {
    let schema = schema_for(asset_class, data_type)?;
    let mut stream = StreamWriter::try_new(writer, &schema).map_err(DataError::io)?;
    for object_path in object_paths {
        visit_partition_batches(store, object_path, &schema, |batch| {
            stream.write(&batch).map_err(DataError::io)
        })?;
    }
    stream.finish().map_err(DataError::io)?;
    Ok(())
}
