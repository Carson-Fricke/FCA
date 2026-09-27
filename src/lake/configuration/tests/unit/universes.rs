use std::collections::BTreeMap;

use super::*;
use crate::yaml::parse_yaml;

pub(crate) const VALID_UNIVERSES: &str = "\
schema_version: 1
equities:
  soak:
    symbols:
      - AAPL
      - SPY
    data_types:
      - trades
      - quotes
    feed: sip
    adjustment: raw
    history:
      trades:
        policy: all_available
        maximum_liquidity_rank: null
      quotes:
        policy: all_available
        maximum_liquidity_rank: null
    backfill_order: newest_first
options:
  feed: opra
  data_types:
    - trades
    - quotes
  selection:
    frequency: daily
    persist_front_set: true
    expiration_order: nearest
    expiration_count: 2
    strike_window:
      basis: underlying_spot
      percent_below: 10
      percent_above: 10
  soak:
    underlyings:
      - SPY
      - QQQ
      - AAPL
    lookback_years: 2
  expansion:
    enabled: false
    maximum_underlyings: 10
crypto:
  pairs:
    - BTC/USD
    - ETH/USD
  data_types:
    - trades
    - quotes
  rollout:
    enabled: false
    target_pair_count: 20
    scheduling_priority: after_equities_and_options
    quota_policy: shared_backfill_quota
";

fn load_with(text: &str, overrides: &[(&str, &str)]) -> Result<UniverseSettings, ConfigError> {
    let mut value = parse_yaml(text).expect("yaml");
    let environ: BTreeMap<String, String> = overrides
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect();
    env::apply(&mut value, &environ, ENV_SCHEMA)?;
    UniverseSettings::from_value(value)
}

#[test]
fn loads_and_normalizes_every_universe() {
    let universes = load_with(VALID_UNIVERSES, &[]).expect("universes");
    assert_eq!(universes.equities.soak.symbols, vec!["AAPL", "SPY"]);
    assert_eq!(universes.equities.soak.data_types, vec!["trade", "quote"]);
    assert_eq!(
        universes.equities.soak.default_symbol().expect("default"),
        "AAPL"
    );
    assert!(universes
        .equities
        .soak
        .history
        .trade
        .maximum_liquidity_rank
        .is_none());
    assert_eq!(universes.options.selection.expiration_count, 2);
    assert_eq!(
        universes.options.soak.underlyings,
        vec!["SPY", "QQQ", "AAPL"]
    );
    assert_eq!(universes.crypto.pairs, vec!["BTC/USD", "ETH/USD"]);
    assert_eq!(universes.crypto.rollout.target_pair_count, 20);
}

#[test]
fn data_type_aliases_dedupe_and_preserve_order() {
    let text = VALID_UNIVERSES.replace(
        "      - trades\n      - quotes\n    feed: sip",
        "      - QUOTES\n      - trade\n      - quotes\n    feed: sip",
    );
    let universes = load_with(&text, &[]).expect("universes");
    assert_eq!(universes.equities.soak.data_types, vec!["quote", "trade"]);
}

#[test]
fn environment_overrides_replace_sequences() {
    let universes = load_with(
        VALID_UNIVERSES,
        &[("LAKE_EQUITIES__SOAK__SYMBOLS", "[\"msft\", \"aapl\"]")],
    )
    .expect("universes");
    assert_eq!(universes.equities.soak.symbols, vec!["MSFT", "AAPL"]);
}

#[test]
fn invalid_pairs_and_symbols_are_rejected() {
    let text = VALID_UNIVERSES.replace("- BTC/USD", "- btcusd");
    let error = load_with(&text, &[]).expect_err("pair");
    assert!(error.message.contains("invalid crypto pair"));
    let text = VALID_UNIVERSES.replace("- AAPL\n      - SPY", "- AAPL$\n      - SPY");
    assert!(load_with(&text, &[]).is_err());
}

#[test]
fn equity_history_requires_both_data_types() {
    let text = VALID_UNIVERSES.replace(
        "    history:\n      trades:\n        policy: all_available\n        maximum_liquidity_rank: null\n      quotes:\n        policy: all_available\n        maximum_liquidity_rank: null\n",
        "    history:\n      trades:\n        policy: all_available\n        maximum_liquidity_rank: null\n",
    );
    let error = load_with(&text, &[]).expect_err("history");
    assert!(error
        .message
        .contains("equity history must define both trades and quotes"));
}

#[test]
fn unsupported_data_types_are_rejected() {
    let text = VALID_UNIVERSES.replace(
        "      - trades\n      - quotes\n    feed: sip",
        "      - classifications\n    feed: sip",
    );
    let error = load_with(&text, &[]).expect_err("data type");
    assert!(error.message.contains("unsupported data type"));
}
