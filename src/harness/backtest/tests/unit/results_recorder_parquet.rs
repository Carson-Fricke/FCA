use std::io::Read as _;

use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;

use super::*;
use crate::results::recorder::writer::build_row;

fn temp_dir(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "harness-backtest-parquet-{}-{name}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&path).expect("temp dir");
    path
}

#[test]
fn writes_arrow_readable_parquet_with_schema_metadata() {
    let directory = temp_dir("roundtrip");
    let path = directory.join("manifest.parquet");
    let mut writer = ParquetArtifactWriter::new(&path, Artifact::Manifest, 2);
    for index in 0..5u64 {
        let row = build_row(
            Artifact::Manifest,
            [
                ("key", Value::utf8(format!("k{index}"))),
                ("value", Value::utf8(index.to_string())),
            ],
        )
        .expect("row");
        writer.append(&row).expect("append");
    }
    assert_eq!(writer.row_count(), 4);
    assert_eq!(writer.pending_rows(), 1);
    writer.close().expect("close");
    assert_eq!(writer.row_count(), 5);

    let mut bytes = Vec::new();
    std::fs::File::open(&path)
        .expect("open")
        .read_to_end(&mut bytes)
        .expect("read");
    assert_eq!(&bytes[..4], b"PAR1");

    let builder =
        ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
            .expect("arrow reader");
    assert_eq!(builder.metadata().file_metadata().num_rows(), 5);
    let names: Vec<&str> = builder
        .schema()
        .fields()
        .iter()
        .map(|field| field.name().as_str())
        .collect();
    assert_eq!(names, vec!["key", "value"]);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn empty_artifact_still_materializes() {
    let directory = temp_dir("empty");
    let path = directory.join("events.parquet");
    let mut writer = ParquetArtifactWriter::new(&path, Artifact::Events, 8);
    writer.close().expect("close");
    assert!(path.exists());
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
            .expect("arrow reader");
    assert_eq!(builder.metadata().file_metadata().num_rows(), 0);
    let _ = std::fs::remove_dir_all(&directory);
}

#[test]
fn schema_carries_the_harness_metadata() {
    let directory = temp_dir("metadata");
    let path = directory.join("equity.parquet");
    let mut writer = ParquetArtifactWriter::new(&path, Artifact::Equity, 8);
    let row = build_row(
        Artifact::Equity,
        [
            ("snapshot_id", Value::utf8("s")),
            ("sequence", Value::UInt64(0)),
            ("timestamp", Value::TimestampNs(0)),
            ("account_id", Value::utf8("a")),
            ("source", Value::utf8("portfolio")),
            ("cash", Value::utf8("0")),
            ("realized_pnl", Value::utf8("0")),
            ("position_count", Value::UInt32(0)),
            ("fill_count", Value::UInt64(0)),
        ],
    )
    .expect("row");
    writer.append(&row).expect("append");
    writer.close().expect("close");
    let builder =
        ParquetRecordBatchReaderBuilder::try_new(std::fs::File::open(&path).expect("open"))
            .expect("arrow reader");
    let metadata = builder.metadata().file_metadata().key_value_metadata();
    let keys: Vec<&str> = metadata
        .map(|entries| entries.iter().map(|entry| entry.key.as_str()).collect())
        .unwrap_or_default();
    assert!(keys.contains(&"harness.schema"), "{keys:?}");
    assert!(keys.contains(&"harness.decimal.encoding"), "{keys:?}");
    let _ = std::fs::remove_dir_all(&directory);
}
