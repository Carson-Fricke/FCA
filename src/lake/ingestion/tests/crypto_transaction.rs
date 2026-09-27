//! Integration coverage for the crypto ingest transaction.

mod support;

use std::sync::Arc;

use lake_catalog::CoverageStatus;
use lake_contracts::{DataError, DataType, DateTime};
use lake_ingestion::CryptoIngestTransaction;
use lake_providers::ProviderError;
use serde_json::{json, Value};
use support::{
    crypto_day, crypto_quote, crypto_trade, day, failing_retry, instant, FakeCryptoProvider,
    FixedClock, RecordingCommitter, RecordingWriter,
};

fn transaction(
    provider: FakeCryptoProvider,
    writer: Arc<dyn lake_ingestion::PartitionWriter>,
    committer: Arc<dyn lake_ingestion::AtomicIngestCommitter>,
) -> CryptoIngestTransaction {
    CryptoIngestTransaction::new(Arc::new(provider), writer, committer).with_retry(failing_retry())
}

#[tokio::test]
async fn partial_day_fetch_window_ends_at_the_injected_clock() {
    let session_date = day(2024, 1, 19);
    let clock = FixedClock::new(instant(12, 0, 0, 0));
    let provider = Arc::new(FakeCryptoProvider::new(
        vec![crypto_trade(instant(0, 1, 0, 0), Some(1), "BTC/USD")],
        vec![crypto_quote(instant(0, 1, 0, 0), "BTC/USD")],
    ));
    let committer = Arc::new(RecordingCommitter::new());
    let transaction = CryptoIngestTransaction::new(
        provider.clone(),
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    )
    .with_clock(clock.clock())
    .with_retry(failing_retry());

    let first = transaction
        .run(" btc/usd ", &crypto_day(session_date, true), None)
        .await
        .expect("partial day");

    assert_eq!(first.symbol, "BTC/USD");
    assert_eq!(first.session_date, session_date);
    assert_eq!(
        first
            .coverage
            .iter()
            .map(|record| record.status)
            .collect::<Vec<_>>(),
        vec![CoverageStatus::Partial, CoverageStatus::Partial]
    );
    assert_eq!(
        first
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![1, 1]
    );
    assert_eq!(committer.commit_count(), 1);
    let calls = committer.commits();
    assert_eq!(calls[0].0.len(), 2);
    assert!(calls[0].1.iter().all(|record| record.object_path.is_some()));

    let fetched = provider.calls();
    assert_eq!(fetched[0].symbols, vec!["BTC/USD".to_string()]);
    assert_eq!(fetched[0].start, DateTime::midnight(session_date));
    assert_eq!(fetched[0].end, instant(12, 0, 0, 0));
    assert_eq!(fetched[1].end, instant(12, 0, 0, 0));

    let provider = FakeCryptoProvider::new(
        vec![
            crypto_trade(instant(0, 1, 0, 0), Some(1), "BTC/USD"),
            crypto_trade(instant(0, 2, 0, 0), Some(2), "BTC/USD"),
        ],
        vec![
            crypto_quote(instant(0, 1, 0, 0), "BTC/USD"),
            crypto_quote(instant(0, 2, 0, 0), "BTC/USD"),
        ],
    );
    let transaction = CryptoIngestTransaction::new(
        Arc::new(provider),
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .with_clock(clock.clock())
    .with_retry(failing_retry());
    clock.set(instant(18, 0, 0, 0));
    let second = transaction
        .run("BTC/USD", &crypto_day(session_date, true), None)
        .await
        .expect("refreshed partial day");

    assert_eq!(
        second
            .coverage
            .iter()
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![2, 2]
    );
}

#[tokio::test]
async fn closed_day_includes_the_final_print_at_utc_midnight() {
    let session_date = day(2024, 1, 18);
    let close = DateTime::midnight(day(2024, 1, 19));
    let provider = FakeCryptoProvider::new(
        vec![crypto_trade(close, Some(1), "BTC/USD")],
        vec![crypto_quote(close, "BTC/USD")],
    );
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    )
    .run("BTC/USD", &crypto_day(session_date, false), None)
    .await
    .expect("closed day");

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
            .map(|record| record.row_count)
            .collect::<Vec<_>>(),
        vec![1, 1]
    );
    assert_eq!(committer.commit_count(), 1);
}

#[tokio::test]
async fn print_after_the_utc_day_close_is_rejected() {
    let session_date = day(2024, 1, 18);
    let after_close = instant(0, 0, 0, 500_000);
    let result = transaction(
        FakeCryptoProvider::new(
            vec![crypto_trade(after_close, Some(1), "BTC/USD")],
            vec![crypto_quote(instant(0, 1, 0, 0), "BTC/USD")],
        ),
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("BTC/USD", &crypto_day(session_date, false), None)
    .await
    .expect("error coverage");

    assert!(result.objects.is_empty());
    assert!(result
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert_eq!(
        result.coverage[0].error_message.as_deref(),
        Some("ValueError: BTC/USD record timestamp is outside UTC day 2024-01-18")
    );
}

#[tokio::test]
async fn session_must_be_exactly_one_utc_calendar_day() {
    let session_date = day(2024, 1, 18);
    let session = lake_coverage::CoverageSession {
        session_date,
        open_time: DateTime::from_micros(DateTime::midnight(session_date).micros() + 3_600_000_000),
        close_time: DateTime::midnight(day(2024, 1, 19)),
        is_partial: false,
    };
    let error = transaction(
        FakeCryptoProvider::new(Vec::new(), Vec::new()),
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("BTC/USD", &session, None)
    .await
    .expect_err("non-UTC-day session");

    assert_eq!(
        error,
        DataError::value("crypto session must be one UTC calendar day")
    );
}

#[tokio::test]
async fn invalid_pairs_are_rejected_before_any_fetch() {
    let provider = FakeCryptoProvider::new(Vec::new(), Vec::new());
    let error = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("BTC-USD", &crypto_day(day(2024, 1, 19), false), None)
    .await
    .expect_err("invalid pair");

    assert_eq!(error, DataError::value("invalid crypto pair: \"BTC-USD\""));
}

#[tokio::test]
async fn fetch_failure_publishes_error_coverage_and_retryable_flag() {
    let provider = FakeCryptoProvider::new(Vec::new(), Vec::new()).failing_trades(3);
    let committer = Arc::new(RecordingCommitter::new());
    let retryable = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    )
    .run("BTC/USD", &crypto_day(day(2024, 1, 19), false), None)
    .await
    .expect("error coverage");

    assert!(retryable.retryable);
    assert!(retryable.objects.is_empty());
    assert!(committer.commits()[0].0.is_empty());
    assert!(retryable
        .coverage
        .iter()
        .all(|record| record.status == CoverageStatus::Error));
    assert_eq!(
        retryable.coverage[0].error_message.as_deref(),
        Some("trade: TransportError: temporary DNS failure")
    );

    let provider = FakeCryptoProvider::new(Vec::new(), Vec::new())
        .with_quote_error(ProviderError::Protocol("bad frame".to_string()));
    let permanent = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        Arc::new(RecordingCommitter::new()),
    )
    .run("BTC/USD", &crypto_day(day(2024, 1, 19), false), None)
    .await
    .expect("error coverage");

    assert!(!permanent.retryable);
    assert_eq!(
        permanent.coverage[0].error_message.as_deref(),
        Some("quote: AlpacaProtocolError: bad frame")
    );
}

#[tokio::test]
async fn transient_retry_recovers_for_crypto() {
    let provider = FakeCryptoProvider::new(
        vec![crypto_trade(instant(0, 1, 0, 0), Some(1), "BTC/USD")],
        vec![crypto_quote(instant(0, 1, 0, 0), "BTC/USD")],
    )
    .failing_trades(2);
    let committer = Arc::new(RecordingCommitter::new());
    let result = transaction(
        provider,
        Arc::new(RecordingWriter::new()),
        committer.clone(),
    )
    .run("BTC/USD", &crypto_day(day(2024, 1, 19), false), None)
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
async fn crypto_rows_are_sorted_and_mapped_like_the_python_transaction() {
    let mut without_id = crypto_trade(instant(0, 1, 0, 0), None, "BTC/USD");
    without_id.exchange = None;
    let mut first_quote = crypto_quote(instant(0, 1, 0, 0), "BTC/USD");
    first_quote.bid_exchange = None;
    let mut second_quote = crypto_quote(instant(0, 1, 0, 0), "BTC/USD");
    second_quote.bid_exchange = Some("CBSE".to_string());

    let provider = FakeCryptoProvider::new(
        vec![
            crypto_trade(instant(0, 2, 0, 0), Some(2), "BTC/USD"),
            without_id,
            crypto_trade(instant(0, 1, 0, 0), Some(7), "BTC/USD"),
        ],
        vec![second_quote, first_quote],
    );
    let writer = Arc::new(RecordingWriter::new());
    let result = transaction(
        provider,
        writer.clone(),
        Arc::new(RecordingCommitter::new()),
    )
    .run("BTC/USD", &crypto_day(day(2024, 1, 19), false), None)
    .await
    .expect("successful transaction");

    assert_eq!(result.coverage[0].row_count, 3);
    let trades = writer.records_for(DataType::Trade);
    assert_eq!(trades.len(), 3);
    let mut trade_keys: Vec<String> = trades[0]
        .as_object()
        .expect("object")
        .keys()
        .cloned()
        .collect();
    trade_keys.sort();
    assert_eq!(
        trade_keys,
        vec![
            "exchange",
            "price",
            "size",
            "symbol",
            "taker_side",
            "timestamp",
            "trade_id",
        ]
    );
    assert_eq!(trades[0]["trade_id"], Value::Null);
    assert_eq!(trades[1]["trade_id"], json!(7));
    assert_eq!(trades[2]["trade_id"], json!(2));

    let quotes = writer.records_for(DataType::Quote);
    assert_eq!(quotes.len(), 2);
    assert_eq!(quotes[0]["bid_exchange"], Value::Null);
    assert_eq!(quotes[1]["bid_exchange"], json!("CBSE"));
}
