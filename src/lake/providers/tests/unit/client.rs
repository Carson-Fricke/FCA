use super::*;
use crate::transport::BoxFuture;
use serde_json::json;

struct NoopTransport;

impl AlpacaTransport for NoopTransport {
    fn get<'a>(
        &'a self,
        _url: &'a str,
        _params: &'a [(String, String)],
        _headers: &'a [(String, String)],
    ) -> BoxFuture<'a, Result<TransportResponse, TransportFailure>> {
        Box::pin(async { unreachable!("the unit-test transport is never called") })
    }
}

fn client(config: ProviderConfig) -> AlpacaClient {
    AlpacaClient::new(config, Arc::new(NoopTransport), RequestGovernor::new(None))
        .expect("valid client")
}

fn micros(value: i64) -> DateTime {
    DateTime::from_micros(value)
}

#[test]
fn plan_time_slices_divides_the_span_and_shares_boundaries() {
    let start = micros(0);
    let end = micros(10);
    let slices = plan_time_slices(start, end, 3);
    assert_eq!(
        slices,
        vec![
            (micros(0), micros(3)),
            (micros(3), micros(6)),
            (micros(6), micros(10)),
        ]
    );
    for pair in slices.windows(2) {
        assert_eq!(pair[0].1, pair[1].0, "no gap or overlap between slices");
    }
}

#[test]
fn plan_time_slices_one_slice_is_the_whole_window() {
    let start = micros(1_000);
    let end = micros(2_000);
    assert_eq!(plan_time_slices(start, end, 1), vec![(start, end)]);
}

#[test]
fn plan_time_slices_zero_slices_is_empty() {
    assert!(plan_time_slices(micros(0), micros(10), 0).is_empty());
}

#[test]
fn plan_time_slices_remainders_stay_inside_the_window() {
    let start = micros(-7);
    let end = micros(7);
    let slices = plan_time_slices(start, end, 4);
    assert_eq!(slices.len(), 4);
    assert_eq!(slices.first().expect("first").0, start);
    assert_eq!(slices.last().expect("last").1, end);
    for (lower, upper) in slices {
        assert!(lower <= upper);
        assert!(lower >= start && upper <= end);
    }
}

#[test]
fn retry_after_accepts_numeric_seconds() {
    assert_eq!(retry_after_seconds(Some("2"), 0), Some(2.0));
    assert_eq!(retry_after_seconds(Some(" 1.5 "), 0), Some(1.5));
    assert_eq!(retry_after_seconds(Some("-3"), 0), Some(0.0));
    assert_eq!(retry_after_seconds(Some("nan"), 0), None);
    assert_eq!(retry_after_seconds(Some("inf"), 0), None);
    assert_eq!(retry_after_seconds(Some("not-a-date"), 0), None);
    assert_eq!(retry_after_seconds(None, 0), None);
}

#[test]
fn retry_after_parses_imf_fixdate() {
    let now = DateTime::parse_iso_utc("1994-11-06T08:49:37Z")
        .expect("now")
        .micros();
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 08:49:47 GMT"), now),
        Some(10.0)
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 08:49:37 GMT"), now),
        Some(0.0)
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 08:49:37 +0000"), now),
        Some(0.0)
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 08:49:37 -0100"), now),
        Some(3_600.0)
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 09:49:37 GMT"), now),
        Some(3_600.0)
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Xxx 1994 08:49:37 GMT"), now),
        None
    );
    assert_eq!(
        retry_after_seconds(Some("Sun, 06 Nov 1994 25:49:37 GMT"), now),
        None
    );
}

#[test]
fn backoff_doubles_until_the_ceiling() {
    let config = ProviderConfig {
        max_attempts: 10,
        backoff_seconds: 0.25,
        max_backoff_seconds: 30.0,
        ..ProviderConfig::default()
    };
    let client = client(config);
    assert_eq!(client.backoff_seconds(1), 0.25);
    assert_eq!(client.backoff_seconds(2), 0.5);
    assert_eq!(client.backoff_seconds(8), 32.0_f64.min(30.0));
    assert_eq!(client.backoff_seconds(30), 30.0);
}

#[test]
fn client_rejects_invalid_configuration() {
    let config = ProviderConfig {
        max_concurrency: 0,
        ..ProviderConfig::default()
    };
    let result = AlpacaClient::new(config, Arc::new(NoopTransport), RequestGovernor::new(None));
    assert!(matches!(result, Err(ProviderError::Configuration(_))));
}

#[test]
fn client_normalizes_a_trailing_slash() {
    let config = ProviderConfig::new("https://data.alpaca.markets/", None, None);
    let client = client(config);
    assert_eq!(client.config().base_url, "https://data.alpaca.markets");
}

#[test]
fn slice_window_is_half_open_except_the_last() {
    let start = micros(10);
    let end = micros(20);
    let half_open = SliceWindow {
        start,
        end,
        inclusive_end: false,
    };
    assert!(half_open.contains(start));
    assert!(half_open.contains(micros(19)));
    assert!(!half_open.contains(end));
    assert!(!half_open.contains(micros(9)));

    let inclusive = SliceWindow {
        start,
        end,
        inclusive_end: true,
    };
    assert!(inclusive.contains(end));
}

#[test]
fn parser_selection_matches_expected_record_shape() {
    let symbol = "AAPL";
    let row = json!({
        "t": "2024-11-27T14:30:00Z",
        "i": 1,
        "x": "V",
        "p": "1",
        "s": 1,
        "c": [],
        "z": "C",
    });
    let parsed = parse::parse_trade(&row, symbol).expect("trade");
    assert_eq!(parsed.symbol, symbol);
}

#[test]
fn partition_inclusive_covers_every_nanosecond_exactly_once() {
    let end = 7_000_000_000i64;
    let units = partition_inclusive(0, end, 7);
    assert_eq!(units.len(), 7);
    assert_eq!(units.first().expect("first").0, 0);
    assert_eq!(units.last().expect("last").1, end);
    for pair in units.windows(2) {
        assert_eq!(
            pair[1].0,
            pair[0].1 + 1,
            "adjacent units differ by exactly one nanosecond"
        );
        assert!(pair[0].0 <= pair[0].1);
    }
}

#[test]
fn partition_inclusive_never_overruns_a_tiny_window() {
    let units = partition_inclusive(5, 6, 64);
    assert_eq!(units, vec![(5, 6)]);
    let degenerate = partition_inclusive(9, 9, 4);
    assert_eq!(degenerate, vec![(9, 9)]);
}

#[test]
fn partition_inclusive_single_unit_is_the_whole_window() {
    assert_eq!(partition_inclusive(10, 20, 1), vec![(10, 20)]);
}
