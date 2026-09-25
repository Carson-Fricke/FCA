use std::sync::atomic::{AtomicU32, Ordering};

use super::*;

#[test]
fn none_selects_both_data_types_in_canonical_order() {
    assert_eq!(
        normalize_data_types(None, false).expect("default selection"),
        vec![DataType::Trade, DataType::Quote]
    );
    assert_eq!(
        normalize_data_types(None, true).expect("default crypto selection"),
        vec![DataType::Trade, DataType::Quote]
    );
}

#[test]
fn selected_data_types_are_deduplicated_and_reordered() {
    assert_eq!(
        normalize_data_types(Some(&[DataType::Quote, DataType::Trade]), false).expect("reordered"),
        vec![DataType::Trade, DataType::Quote]
    );
    assert_eq!(
        normalize_data_types(Some(&[DataType::Quote, DataType::Quote]), false).expect("deduped"),
        vec![DataType::Quote]
    );
}

#[test]
fn empty_and_unsupported_selections_are_rejected() {
    let empty = normalize_data_types(Some(&[]), false).expect_err("empty selection");
    assert_eq!(
        empty,
        DataError::value("data_types must contain trade or quote")
    );

    let unsupported =
        normalize_data_types(Some(&[DataType::Fundamentals]), false).expect_err("unsupported");
    assert_eq!(
        unsupported,
        DataError::value("data_types must contain trade or quote")
    );

    let crypto = normalize_data_types(Some(&[DataType::Classification]), true).expect_err("crypto");
    assert_eq!(
        crypto,
        DataError::value("crypto data_types must contain trades or quotes")
    );
}

#[test]
fn default_retry_budget_matches_the_python_constants() {
    let retry = TransientRetry::default();
    assert_eq!(retry.attempts, 3);
    assert_eq!(retry.base_delay_seconds, 1.0);
}

#[test]
fn provider_messages_carry_the_lake_class_name() {
    assert_eq!(
        provider_error_message(&ProviderError::Http {
            status: 429,
            url: "https://api.example/quotes".to_string(),
        }),
        "AlpacaHTTPError: Alpaca request failed with HTTP 429: https://api.example/quotes"
    );
    assert_eq!(
        provider_error_message(&ProviderError::Transport("reset".to_string())),
        "TransportError: reset"
    );
    assert_eq!(
        provider_error_message(&ProviderError::Timeout("slow".to_string())),
        "TimeoutError: slow"
    );
    assert_eq!(
        provider_error_message(&ProviderError::Protocol("bad frame".to_string())),
        "AlpacaProtocolError: bad frame"
    );
    assert_eq!(
        provider_error_message(&ProviderError::Configuration("bad request".to_string())),
        "ValueError: bad request"
    );
}

#[test]
fn data_messages_carry_the_python_class_name() {
    assert_eq!(
        data_error_message(&DataError::value("outside session")),
        "ValueError: outside session"
    );
    assert_eq!(
        data_error_message(&DataError::type_error("wrong type")),
        "TypeError: wrong type"
    );
    assert_eq!(
        data_error_message(&DataError::io("disk full")),
        "IoError: disk full"
    );
}

#[test]
fn fetch_failure_messages_join_one_entry_per_failed_type() {
    let results: Vec<Result<u32, ProviderError>> =
        vec![Err(ProviderError::Transport("reset".to_string())), Ok(1)];
    assert_eq!(
        fetch_error_message(&[DataType::Trade, DataType::Quote], &results),
        Some("trade: TransportError: reset".to_string())
    );

    let both: Vec<Result<u32, ProviderError>> = vec![
        Err(ProviderError::Timeout("slow".to_string())),
        Err(ProviderError::Protocol("bad frame".to_string())),
    ];
    assert_eq!(
        fetch_error_message(&[DataType::Trade, DataType::Quote], &both),
        Some("trade: TimeoutError: slow; quote: AlpacaProtocolError: bad frame".to_string())
    );

    let success: Vec<Result<u32, ProviderError>> = vec![Ok(1), Ok(2)];
    assert_eq!(
        fetch_error_message(&[DataType::Trade, DataType::Quote], &success),
        None
    );
}

#[test]
fn retryability_requires_at_least_one_all_transient_failure() {
    let all_transient: Vec<Result<u32, ProviderError>> = vec![
        Err(ProviderError::Transport("reset".to_string())),
        Err(ProviderError::Timeout("slow".to_string())),
    ];
    assert!(failures_all_transient(&all_transient));

    let mixed: Vec<Result<u32, ProviderError>> = vec![
        Err(ProviderError::Transport("reset".to_string())),
        Err(ProviderError::Protocol("bad frame".to_string())),
    ];
    assert!(!failures_all_transient(&mixed));

    let success: Vec<Result<u32, ProviderError>> = vec![Ok(1), Ok(2)];
    assert!(!failures_all_transient(&success));

    let one_transient_plus_success: Vec<Result<u32, ProviderError>> =
        vec![Err(ProviderError::Transport("reset".to_string())), Ok(1)];
    assert!(failures_all_transient(&one_transient_plus_success));
}

#[test]
fn canonical_symbol_trims_uppercases_and_validates() {
    assert_eq!(canonical_symbol(" aapl ").expect("symbol"), "AAPL");
    assert_eq!(canonical_symbol("brk.b").expect("symbol"), "BRK.B");
    assert!(canonical_symbol("").is_err());
    assert!(canonical_symbol("..").is_err());
}

#[tokio::test]
async fn transient_retry_recovers_and_stops_at_the_budget() {
    let calls = AtomicU32::new(0);
    let result = fetch_with_transient_retry(
        TransientRetry {
            attempts: 3,
            base_delay_seconds: 0.0,
        },
        || {
            let attempt = calls.fetch_add(1, Ordering::SeqCst) + 1;
            Box::pin(async move {
                if attempt < 3 {
                    Err(ProviderError::Transport("reset".to_string()))
                } else {
                    Ok(attempt)
                }
            })
        },
    )
    .await;
    assert_eq!(result, Ok(3));
    assert_eq!(calls.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn exhausted_transient_failure_returns_the_last_error() {
    let calls = AtomicU32::new(0);
    let result = fetch_with_transient_retry(
        TransientRetry {
            attempts: 2,
            base_delay_seconds: 0.0,
        },
        || {
            calls.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move { Err::<u32, _>(ProviderError::Transport("reset".to_string())) })
        },
    )
    .await;
    assert_eq!(result, Err(ProviderError::Transport("reset".to_string())));
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn permanent_failures_are_not_retried() {
    let calls = AtomicU32::new(0);
    let result = fetch_with_transient_retry(
        TransientRetry {
            attempts: 3,
            base_delay_seconds: 0.0,
        },
        || {
            calls.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move {
                Err::<u32, _>(ProviderError::Configuration("bad request".to_string()))
            })
        },
    )
    .await;
    assert_eq!(
        result,
        Err(ProviderError::Configuration("bad request".to_string()))
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}
