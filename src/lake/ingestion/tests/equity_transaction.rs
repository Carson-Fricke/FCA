//! Integration coverage for the equity ingest transaction.

mod support;

use std::sync::Arc;

use lake_catalog::CoverageStatus;
use lake_contracts::{AssetClass, DataError, DataType};
use lake_ingestion::{EquityIngestTransaction, PartitionWriter, StagingPartitionWriter};
use lake_providers::ProviderError;
use serde_json::Value;
use support::{
    day, equity_quote, equity_session, equity_trade, failing_retry, instant, FakeEquityProvider,
    RecordingCommitter, RecordingWriter, ScriptedUsage, ZeroUsage,
};

fn transaction(
    provider: FakeEquityProvider,
    writer: Arc<dyn lake_ingestion::PartitionWriter>,
    committer: Arc<dyn lake_ingestion::AtomicIngestCommitter>,
) -> EquityIngestTransaction {
    EquityIngestTransaction::new(Arc::new(provider), writer, committer).with_retry(failing_retry())
}

#[tokio::test]
async fn both_datasets_publish_paths_statuses_and_checksums() {
    let provider = FakeEquityProvider::new(
        vec![
            equity_trade(instant(14, 31, 0, 0), 2, "AAPL"),
            equity_trade(instant(14, 30, 0, 0), 1, "AAPL"),
        ],
        vec![equity_quote(instant(14, 30, 0, 123_456), "AAPL")],
    );
    let writer = Arc::new(StagingPartitionWriter::new());
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(provider, writer.clone(), committer.clone())
        .run(" aapl ", &equity_session(false), None)
        .await
        .expect("successful transaction");

    assert_eq!(result.symbol, "AAPL");
    assert_eq!(result.session_date, day(2024, 1, 19));
    assert!(!result.retryable);
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Trade, DataType::Quote]
    );
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Complete]
    );
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.object_path.as_deref())
            .collect::<Vec<_>>(),
        vec![
            Some("date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet"),
            Some("date=2024-01-19/asset_class=equity/data_type=quote/AAPL.parquet"),
        ]
    );
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![2, 1]
    );
    assert_eq!(committer.commit_count(), 1);
    let commits = committer.commits();
    assert_eq!(commits[0].0.len(), 2);
    assert_eq!(commits[0].1, result.coverage);
    for (record, object) in commits[0].1.iter().zip(&commits[0].0) {
        assert_eq!(
            record.object_path.as_deref(),
            Some(object.metadata.path.as_str())
        );
        assert_eq!(record.row_count, object.metadata.rows);
        assert_eq!(record.byte_size, object.metadata.bytes);
        assert_eq!(record.byte_size as usize, object.payload.len());
        assert_eq!(
            record.checksum.as_deref(),
            Some(object.metadata.checksum.as_str())
        );
        assert!(!object.payload.is_empty());
    }
    assert_eq!(
        result
            .objects
            .iter()
            .map(|metadata| metadata.path.as_str())
            .collect::<Vec<_>>(),
        vec![
            "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet",
            "date=2024-01-19/asset_class=equity/data_type=quote/AAPL.parquet",
        ]
    );
    assert!(result.objects[0].checksum.len() == 64);
}

#[tokio::test]
async fn committer_is_called_once_and_the_writer_is_drained() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
    );
    let writer = Arc::new(StagingPartitionWriter::new());
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(provider, writer.clone(), committer.clone())
        .run("AAPL", &equity_session(false), None)
        .await
        .expect("successful transaction");

    assert_eq!(committer.commit_count(), 1);
    assert_eq!(committer.commits()[0].0.len(), result.objects.len());
    assert!(writer.drain_staged().is_empty());
}

#[tokio::test]
async fn partial_session_is_partial_and_closed_empty_session_is_empty() {
    let partial_writer = Arc::new(StagingPartitionWriter::new());
    let partial_committer = Arc::new(RecordingCommitter::new());
    let partial = transaction(
        FakeEquityProvider::new(Vec::new(), Vec::new()),
        partial_writer,
        partial_committer.clone(),
    )
    .run("AAPL", &equity_session(true), None)
    .await
    .expect("partial session");

    assert_eq!(
        partial
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Partial, CoverageStatus::Partial]
    );
    assert_eq!(
        partial
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![0, 0]
    );
    assert!(partial
        .coverage
        .iter()
        .all(|record| record.object_path.is_some()));
    for object in &partial_committer.commits()[0].0 {
        assert!(!object.payload.is_empty());
    }

    let empty_committer = Arc::new(RecordingCommitter::new());
    let empty = transaction(
        FakeEquityProvider::new(Vec::new(), Vec::new()),
        Arc::new(StagingPartitionWriter::new()),
        empty_committer.clone(),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("empty session");

    assert_eq!(
        empty
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Empty, CoverageStatus::Empty]
    );
    assert!(empty.coverage.iter().all(|record| record.row_count == 0));
    assert!(!empty_committer.commits()[0].0.is_empty());
}

#[tokio::test]
async fn fetch_failure_publishes_error_coverage_and_marks_retryable() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
    )
    .with_quote_error(ProviderError::Http {
        status: 500,
        url: "https://api.example/v2/stocks/quotes".to_string(),
    });
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(
        provider,
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("error coverage is a result, not an error");

    assert!(result.objects.is_empty());
    assert!(!result.retryable);
    assert_eq!(committer.commit_count(), 1);
    assert!(committer.commits()[0].0.is_empty());
    assert!(result
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert!(result.coverage.iter().all(|record| {
        record.error_message.as_deref()
            == Some(
                "quote: AlpacaHTTPError: Alpaca request failed with HTTP 500: \
                 https://api.example/v2/stocks/quotes",
            )
    }));
    assert!(result
        .coverage
        .iter()
        .all(|record| record.object_path.is_none()));
}

#[tokio::test]
async fn transient_blip_is_retried_and_recovers() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
    )
    .failing_trades(2);
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(
        provider,
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("retry recovers");

    assert!(!result.retryable);
    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Complete, CoverageStatus::Complete]
    );
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn exhausted_transient_failure_is_retryable_and_permanent_failure_is_not() {
    let committer = Arc::new(RecordingCommitter::new());
    let provider = FakeEquityProvider::new(Vec::new(), Vec::new()).failing_trades(3);
    let retryable = transaction(
        provider,
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("error coverage");
    assert!(retryable.retryable);
    assert!(retryable
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert!(retryable.coverage[0]
        .error_message
        .as_deref()
        .expect("message")
        .starts_with("trade: TransportError: temporary DNS failure"));
    assert!(retryable.objects.is_empty());

    let provider = FakeEquityProvider::new(Vec::new(), Vec::new())
        .with_trade_error(ProviderError::Configuration("bad request".to_string()));
    let permanent = transaction(
        provider,
        Arc::new(StagingPartitionWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("error coverage");
    assert!(!permanent.retryable);
    assert_eq!(
        permanent.coverage[0].error_message.as_deref(),
        Some("trade: ValueError: bad request")
    );
}

#[tokio::test]
async fn row_validation_errors_become_error_coverage() {
    let committer = Arc::new(RecordingCommitter::new());
    let mismatch = transaction(
        FakeEquityProvider::new(
            vec![equity_trade(instant(14, 30, 0, 0), 1, "MSFT")],
            vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
        ),
        Arc::new(StagingPartitionWriter::new()),
        committer.clone(),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("error coverage");
    assert!(mismatch.objects.is_empty());
    assert!(committer.commits()[0].0.is_empty());
    assert!(mismatch
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert_eq!(
        mismatch.coverage[0].error_message.as_deref(),
        Some("ValueError: trade symbol does not match the requested symbol")
    );

    let after_close = transaction(
        FakeEquityProvider::new(
            vec![equity_trade(instant(21, 0, 0, 500_000), 1, "AAPL")],
            vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
        ),
        Arc::new(StagingPartitionWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("error coverage");
    assert!(after_close
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert_eq!(
        after_close.coverage[0].error_message.as_deref(),
        Some("ValueError: AAPL record timestamp is outside session 2024-01-19")
    );
}

#[tokio::test]
async fn the_closing_print_at_the_session_close_is_inclusive() {
    let result = transaction(
        FakeEquityProvider::new(
            vec![equity_trade(instant(21, 0, 0, 0), 1, "AAPL")],
            vec![equity_quote(instant(21, 0, 0, 0), "AAPL")],
        ),
        Arc::new(StagingPartitionWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("inclusive close");

    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![1, 1]
    );
    assert_eq!(result.symbol, "AAPL");
}

#[tokio::test]
async fn a_storage_guard_refusal_before_fetch_prevents_every_write() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
    );
    let writer = Arc::new(RecordingWriter::new());
    let committer = Arc::new(RecordingCommitter::new());
    let error = EquityIngestTransaction::new(Arc::new(provider), writer.clone(), committer.clone())
        .with_cap_guard(support::zero_usage_guard(0))
        .run("AAPL", &equity_session(false), None)
        .await
        .expect_err("guard pauses the transaction");

    assert!(matches!(error, DataError::Conflict(_)));
    assert!(error.to_string().contains("paused ingest"));
    assert!(writer.writes().is_empty());
    assert_eq!(writer.staged_len(), 0);
    assert_eq!(committer.commit_count(), 0);
}

#[tokio::test]
async fn a_storage_guard_refusal_before_commit_discards_staged_objects() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        vec![equity_quote(instant(14, 30, 0, 0), "AAPL")],
    );
    let writer = Arc::new(RecordingWriter::new());
    let committer = Arc::new(RecordingCommitter::new());
    let guard = Arc::new(lake_storage::StorageCapGuard::new(
        lake_storage::StorageCapPolicy {
            capacity_bytes: 64,
            pause_enqueue_at_capacity: false,
            pause_ingest_at_capacity: true,
        },
        Arc::new(ScriptedUsage::new([0, 1024])),
    ));
    let error = EquityIngestTransaction::new(Arc::new(provider), writer.clone(), committer.clone())
        .with_cap_guard(guard)
        .run("AAPL", &equity_session(false), None)
        .await
        .expect_err("guard pauses the commit");

    assert!(matches!(error, DataError::Conflict(_)));
    assert_eq!(writer.writes().len(), 2);
    assert_eq!(writer.staged_len(), 0);
    assert_eq!(committer.commit_count(), 0);
}

#[tokio::test]
async fn canonical_sort_order_is_applied_to_trades_and_quotes() {
    let mut late = equity_trade(instant(14, 31, 0, 0), 1, "AAPL");
    late.update = Some("T".to_string());
    let mut two_conditions = equity_trade(instant(14, 31, 0, 0), 1, "AAPL");
    two_conditions.conditions = vec!["@".to_string(), "F".to_string()];
    let mut second_exchange = equity_quote(instant(14, 30, 0, 0), "AAPL");
    second_exchange.bid_exchange = "ARCX".to_string();
    second_exchange.ask_exchange = "ARCX".to_string();

    let provider = FakeEquityProvider::new(
        vec![
            late,
            two_conditions,
            equity_trade(instant(14, 30, 0, 0), 2, "AAPL"),
            equity_trade(instant(14, 31, 0, 0), 1, "AAPL"),
            equity_trade(instant(14, 30, 0, 0), 1, "AAPL"),
        ],
        vec![
            equity_quote(instant(14, 30, 0, 0), "AAPL"),
            equity_quote(instant(14, 31, 0, 0), "AAPL"),
            second_exchange,
        ],
    );
    let writer = Arc::new(RecordingWriter::new());
    let result = transaction(
        provider,
        writer.clone(),
        Arc::new(RecordingCommitter::new()),
    )
    .run("AAPL", &equity_session(false), None)
    .await
    .expect("successful transaction");

    assert_eq!(result.coverage[0].row_count, 5);
    let trades = writer.records_for(DataType::Trade);
    assert_eq!(trades.len(), 5);
    assert_eq!(
        trades
            .iter()
            .map(|row| row["trade_id"].as_u64().expect("id"))
            .collect::<Vec<_>>(),
        vec![1, 2, 1, 1, 1]
    );
    assert_eq!(trades[2]["conditions"], serde_json::json!(["@"]));
    assert_eq!(trades[2]["update"], Value::Null);
    assert_eq!(trades[3]["conditions"], serde_json::json!(["@"]));
    assert_eq!(trades[3]["update"], serde_json::json!("T"));
    assert_eq!(trades[4]["conditions"], serde_json::json!(["@", "F"]));

    let quotes = writer.records_for(DataType::Quote);
    assert_eq!(quotes.len(), 3);
    assert_eq!(
        quotes
            .iter()
            .map(|row| row["bid_exchange"].as_str().expect("exchange"))
            .collect::<Vec<_>>(),
        vec!["ARCX", "XNAS", "XNAS"]
    );
    assert_eq!(
        quotes[2]["timestamp"],
        Value::from(instant(14, 31, 0, 0).to_nanos())
    );
}

#[tokio::test]
async fn data_type_selection_limits_fetch_and_publication() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        Vec::new(),
    );
    let writer = Arc::new(StagingPartitionWriter::new());
    let result = transaction(provider, writer, Arc::new(RecordingCommitter::new()))
        .run(
            "AAPL",
            &equity_session(false),
            Some(&[DataType::Quote, DataType::Trade]),
        )
        .await
        .expect("selected data types");

    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Trade, DataType::Quote]
    );

    let provider = FakeEquityProvider::new(Vec::new(), Vec::new());
    let writer = Arc::new(RecordingWriter::new());
    let result = transaction(
        provider,
        writer.clone(),
        Arc::new(RecordingCommitter::new()),
    )
    .run("AAPL", &equity_session(false), Some(&[DataType::Quote]))
    .await
    .expect("quote only");

    assert_eq!(
        result
            .coverage
            .iter()
            .map(|record| record.data_type)
            .collect::<Vec<_>>(),
        vec![DataType::Quote]
    );
    assert!(writer
        .writes()
        .iter()
        .all(|call| call.data_type == DataType::Quote));
    assert!(writer
        .writes()
        .iter()
        .all(|call| call.asset_class == AssetClass::Equity));
}

#[tokio::test]
async fn unsupported_data_types_are_rejected_before_any_fetch() {
    let provider = FakeEquityProvider::new(Vec::new(), Vec::new());
    let error = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run(
        "AAPL",
        &equity_session(false),
        Some(&[DataType::Classification]),
    )
    .await
    .expect_err("unsupported selection");

    assert_eq!(
        error,
        DataError::value("data_types must contain trade or quote")
    );
}

#[tokio::test]
async fn zero_usage_guard_uses_the_default_retry_budget() {
    let provider = FakeEquityProvider::new(
        vec![equity_trade(instant(14, 30, 0, 0), 1, "AAPL")],
        Vec::new(),
    );
    let transaction = EquityIngestTransaction::new(
        Arc::new(provider),
        Arc::new(StagingPartitionWriter::new()),
        Arc::new(RecordingCommitter::new()),
    );
    let guard = Arc::new(lake_storage::StorageCapGuard::new(
        lake_storage::StorageCapPolicy {
            capacity_bytes: u64::MAX,
            pause_enqueue_at_capacity: false,
            pause_ingest_at_capacity: true,
        },
        Arc::new(ZeroUsage),
    ));
    let result = transaction
        .with_cap_guard(guard)
        .run("AAPL", &equity_session(false), Some(&[DataType::Trade]))
        .await
        .expect("capacity is available");
    assert_eq!(result.coverage.len(), 1);
}
