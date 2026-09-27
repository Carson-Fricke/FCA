use super::*;

#[test]
fn defaults_match_the_python_client() {
    let config = ProviderConfig::default();
    assert_eq!(config.base_url, "https://data.alpaca.markets");
    assert_eq!(config.api_key, None);
    assert_eq!(config.api_secret, None);
    assert_eq!(config.max_concurrency, 4);
    assert_eq!(config.timeout_seconds, 30.0);
    assert_eq!(config.max_attempts, 5);
    assert!(config.honor_retry_after);
    assert_eq!(config.backoff_seconds, 0.25);
    assert_eq!(config.max_backoff_seconds, 30.0);
    assert_eq!(config.requests_per_minute, None);
    config.validate().expect("defaults are valid");
}

#[test]
fn new_trims_the_base_url_and_keeps_credentials() {
    let config = ProviderConfig::new(
        "https://data.alpaca.markets/",
        Some("key".to_string()),
        Some("secret".to_string()),
    );
    assert_eq!(config.base_url, "https://data.alpaca.markets");
    assert_eq!(config.api_key.as_deref(), Some("key"));
    assert_eq!(config.api_secret.as_deref(), Some("secret"));
    assert_eq!(config.max_attempts, 5);
}

#[test]
fn validate_names_each_invalid_bound() {
    let cases = [
        (
            ProviderConfig {
                max_concurrency: 0,
                ..ProviderConfig::default()
            },
            "max_concurrency must be at least 1",
        ),
        (
            ProviderConfig {
                timeout_seconds: 0.0,
                ..ProviderConfig::default()
            },
            "timeout_seconds must be positive",
        ),
        (
            ProviderConfig {
                timeout_seconds: f64::NAN,
                ..ProviderConfig::default()
            },
            "timeout_seconds must be positive",
        ),
        (
            ProviderConfig {
                max_attempts: 0,
                ..ProviderConfig::default()
            },
            "max_attempts must be at least 1",
        ),
        (
            ProviderConfig {
                backoff_seconds: -0.1,
                ..ProviderConfig::default()
            },
            "backoff_seconds cannot be negative",
        ),
        (
            ProviderConfig {
                max_backoff_seconds: f64::INFINITY,
                ..ProviderConfig::default()
            },
            "max_backoff_seconds cannot be negative",
        ),
        (
            ProviderConfig {
                requests_per_minute: Some(0),
                ..ProviderConfig::default()
            },
            "requests_per_minute must be at least 1",
        ),
    ];
    for (config, expected) in cases {
        match config.validate() {
            Err(ProviderError::Configuration(message)) => assert_eq!(message, expected),
            other => panic!("expected configuration error {expected:?}, got {other:?}"),
        }
    }
}

#[test]
fn zero_backoff_and_zero_max_backoff_are_valid() {
    let config = ProviderConfig {
        backoff_seconds: 0.0,
        max_backoff_seconds: 0.0,
        requests_per_minute: Some(1),
        ..ProviderConfig::default()
    };
    config.validate().expect("zero backoff is valid");
}
