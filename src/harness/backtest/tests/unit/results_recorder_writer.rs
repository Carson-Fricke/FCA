use super::*;

#[test]
fn formats_epoch_and_pre_epoch_timestamps() {
    assert_eq!(format_timestamp_ns(0), "1970-01-01T00:00:00.000000Z");
    assert_eq!(
        format_timestamp_ns(1_704_153_600_000_000_000),
        "2024-01-02T00:00:00.000000Z"
    );
    assert_eq!(
        format_timestamp_ns(1_704_153_600_123_456_000),
        "2024-01-02T00:00:00.123456Z"
    );
    assert_eq!(format_timestamp_ns(-1), "1969-12-31T23:59:59.999999Z");
}

#[test]
fn row_json_is_schema_ordered_and_sorted() {
    let row = build_row(
        Artifact::Manifest,
        [("value", Value::utf8("x")), ("key", Value::utf8("a"))],
    )
    .expect("row");
    assert_eq!(
        row_to_canonical_json(Artifact::Manifest, &row),
        r#"{"key":"a","value":"x"}"#
    );
}

#[test]
fn missing_non_nullable_field_is_rejected() {
    // `sequence` is required and not supplied.
    let result = build_row(Artifact::Actions, [("action_id", Value::utf8("a"))]);
    assert!(matches!(result, Err(WriterError::MissingField { .. })));
}

#[test]
fn null_not_allowed_is_rejected() {
    let result = build_row(
        Artifact::Actions,
        [
            ("action_id", Value::utf8("a")),
            ("sequence", Value::UInt64(1)),
            ("timestamp", Value::TimestampNs(0)),
            ("strategy_id", Value::utf8("s")),
            ("kind", Value::Null),
        ],
    );
    assert!(matches!(result, Err(WriterError::NullNotAllowed { .. })));
}

#[test]
fn append_and_close_batches_rows() {
    let directory =
        std::env::temp_dir().join(format!("harness-backtest-writer-{}", std::process::id()));
    let path = directory.join("manifest.parquet");
    let mut writer = CanonicalArtifactWriter::new(&path, Artifact::Manifest, 2);
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
    let text = std::fs::read_to_string(&path).expect("read");
    assert_eq!(text.lines().count(), 5);
    let _ = std::fs::remove_dir_all(&directory);
}
