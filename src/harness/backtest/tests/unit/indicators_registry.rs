use super::*;
use crate::indicators::base::{Tick, TickDriver, TickKind};
use num::Decimal18;

fn params(pairs: &[(&str, ParamValue)]) -> BTreeMap<String, ParamValue> {
    pairs
        .iter()
        .map(|(name, value)| ((*name).to_string(), value.clone()))
        .collect()
}

fn trade(sequence: u64, price: i64) -> Tick {
    Tick {
        timestamp_ns: sequence as i64,
        sequence,
        kind: TickKind::Trade {
            price: Decimal18::from_i64(price),
            quantity: Decimal18::from_i64(1),
        },
    }
}

#[test]
fn default_registry_names_are_sorted() {
    assert_eq!(
        indicator_names(),
        vec![
            "atr",
            "dollar_volume",
            "ema",
            "mid_sma",
            "obv",
            "realized_vol",
            "rsi",
            "sma",
            "vwap"
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>()
    );
}

#[test]
fn create_validates_and_builds() {
    let params = params(&[("period", ParamValue::Int(2))]);
    let configured = create_indicator("sma", &params, "spy").expect("create");
    assert_eq!(configured.symbol, "SPY");
    let mut indicator = configured.indicator;
    assert_eq!(indicator.name(), "sma");
    assert_eq!(indicator.drivers(), &[TickDriver::Trade]);
    for sequence in 0..2u64 {
        indicator.on_tick(&trade(sequence, 10)).expect("tick");
    }
    assert_eq!(indicator.value(), Some(Decimal18::from_i64(10)));
}

#[test]
fn create_builds_volatility_and_volume_builtins() {
    let configured =
        create_indicator("atr", &params(&[("period", ParamValue::Int(2))]), "spy").expect("atr");
    assert_eq!(configured.indicator.name(), "atr");
    assert_eq!(configured.symbol, "SPY");

    let configured = create_indicator(
        "realized_vol",
        &params(&[("window", ParamValue::Duration("5s".to_string()))]),
        "spy",
    )
    .expect("realized_vol");
    assert_eq!(configured.indicator.name(), "realized_vol");

    let configured = create_indicator(
        "dollar_volume",
        &params(&[("window", ParamValue::Duration("1min".to_string()))]),
        "spy",
    )
    .expect("dollar_volume");
    assert_eq!(configured.indicator.name(), "dollar_volume");

    let configured = create_indicator("vwap", &BTreeMap::new(), "spy").expect("vwap");
    assert_eq!(configured.indicator.name(), "vwap");

    let configured = create_indicator("obv", &BTreeMap::new(), "spy").expect("obv");
    assert_eq!(configured.indicator.name(), "obv");
}

#[test]
fn realized_vol_requires_a_window() {
    assert!(create_indicator("realized_vol", &BTreeMap::new(), "spy").is_err());
}

#[test]
fn rejects_unknown_and_missing_params() {
    assert!(create_indicator("nope", &BTreeMap::new(), "spy").is_err());
    assert!(create_indicator("sma", &BTreeMap::new(), "spy").is_err());
    let unknown = params(&[("nope", ParamValue::Int(1))]);
    assert!(create_indicator("sma", &unknown, "spy").is_err());
}

#[test]
fn duration_parser_handles_common_units() {
    assert_eq!(parse_duration_ns("1min").expect("min"), 60_000_000_000);
    assert_eq!(parse_duration_ns("5s").expect("s"), 5_000_000_000);
    assert_eq!(parse_duration_ns("250ms").expect("ms"), 250_000_000);
    assert!(parse_duration_ns("0s").is_err());
    assert!(parse_duration_ns("hello").is_err());
}
