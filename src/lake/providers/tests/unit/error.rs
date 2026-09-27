use super::*;

#[test]
fn transient_classification_splits_transport_from_terminal() {
    assert!(ProviderError::Transport("boom".to_string()).is_transient());
    assert!(ProviderError::Timeout("late".to_string()).is_transient());
    assert!(!ProviderError::Http {
        status: 500,
        url: "https://x".to_string(),
    }
    .is_transient());
    assert!(!ProviderError::Http {
        status: 429,
        url: "https://x".to_string(),
    }
    .is_transient());
    assert!(!ProviderError::Protocol("bad".to_string()).is_transient());
    assert!(!ProviderError::Configuration("bad".to_string()).is_transient());
}

#[test]
fn http_error_message_matches_python() {
    let error = ProviderError::Http {
        status: 500,
        url: "https://data.alpaca.markets/v2/stocks/trades".to_string(),
    };
    assert_eq!(
        error.to_string(),
        "Alpaca request failed with HTTP 500: https://data.alpaca.markets/v2/stocks/trades"
    );
}
