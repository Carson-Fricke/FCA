//! The screening authoring surface: serde round-trips, accessor behavior, and
//! the capability layout of the screening vtable.
//!
//! Context and rows cross the host/artifact boundary as JSON with exact
//! decimal strings and deterministic [`BTreeMap`] ordering; these tests pin
//! the exact wire shape, the empty-collection accessor semantics, and the
//! vtable's field set (exactly the four one-shot callbacks — no order
//! surface).

use std::collections::BTreeMap;
use std::mem::{offset_of, size_of};

use harness_strategy_sdk::{
    serde_json, Bar, ScreenContext, ScreenRow, ScreeningProgramVTable, SDK_ABI_VERSION,
};

fn sample_bar(date: &str, close: &str) -> Bar {
    Bar {
        date: date.to_string(),
        open: "100.00".to_string(),
        high: "101.50".to_string(),
        low: "99.75".to_string(),
        close: close.to_string(),
        volume: "123456".to_string(),
    }
}

fn sample_context() -> ScreenContext {
    ScreenContext {
        as_of: "2024-01-02".to_string(),
        universe: vec!["AAA".to_string(), "BBB".to_string()],
        bars: BTreeMap::from([(
            "AAA".to_string(),
            vec![
                sample_bar("2024-01-02", "101.00"),
                sample_bar("2024-01-03", "102.50"),
            ],
        )]),
        facts: BTreeMap::from([
            (
                "AAA".to_string(),
                BTreeMap::from([("sector".to_string(), "technology".to_string())]),
            ),
            (
                "BBB".to_string(),
                BTreeMap::from([("market_cap".to_string(), "1E+3".to_string())]),
            ),
        ]),
        indicators: BTreeMap::from([(
            "AAA".to_string(),
            BTreeMap::from([("ind:sma(20)".to_string(), "100.25".to_string())]),
        )]),
    }
}

#[test]
fn context_serializes_to_the_exact_wire_shape_and_roundtrips() {
    let context = sample_context();
    let document = serde_json::to_string(&context).expect("serialize context");
    assert_eq!(
        document,
        concat!(
            r#"{"as_of":"2024-01-02","universe":["AAA","BBB"],"#,
            r#""bars":{"AAA":[{"date":"2024-01-02","open":"100.00","high":"101.50","#,
            r#""low":"99.75","close":"101.00","volume":"123456"},"#,
            r#"{"date":"2024-01-03","open":"100.00","high":"101.50","low":"99.75","#,
            r#""close":"102.50","volume":"123456"}]},"#,
            r#""facts":{"AAA":{"sector":"technology"},"BBB":{"market_cap":"1E+3"}},"#,
            r#""indicators":{"AAA":{"ind:sma(20)":"100.25"}}}"#,
        )
    );

    let restored: ScreenContext = serde_json::from_str(&document).expect("deserialize context");
    assert_eq!(restored, context);
    // Exact decimals cross as strings: trailing zeros and exponents survive.
    assert_eq!(restored.bars("AAA")[1].close, "102.50");
    assert_eq!(
        restored.facts("BBB").get("market_cap"),
        Some(&"1E+3".to_string())
    );
}

#[test]
fn rows_serialize_to_the_exact_wire_shape_and_roundtrip() {
    let rows = vec![
        ScreenRow {
            symbol: "AAA".to_string(),
            metrics: BTreeMap::from([("momentum".to_string(), "9.50".to_string())]),
        },
        ScreenRow {
            symbol: "BBB".to_string(),
            metrics: BTreeMap::new(),
        },
    ];
    let document = serde_json::to_string(&rows).expect("serialize rows");
    assert_eq!(
        document,
        r#"[{"symbol":"AAA","metrics":{"momentum":"9.50"}},{"symbol":"BBB","metrics":{}}]"#
    );
    let restored: Vec<ScreenRow> = serde_json::from_str(&document).expect("deserialize rows");
    assert_eq!(restored, rows);
}

#[test]
fn accessors_return_data_and_empty_collections_for_absent_symbols() {
    let context = sample_context();
    assert_eq!(context.universe(), ["AAA".to_string(), "BBB".to_string()]);
    assert_eq!(context.bars("AAA").len(), 2);
    assert_eq!(context.bars("AAA")[0].open, "100.00");
    assert!(context.bars("MISSING").is_empty());
    assert_eq!(
        context.facts("AAA").get("sector").map(String::as_str),
        Some("technology")
    );
    assert!(context.facts("MISSING").is_empty());
    assert_eq!(
        context
            .indicators("AAA")
            .get("ind:sma(20)")
            .map(String::as_str),
        Some("100.25")
    );
    assert!(context.indicators("MISSING").is_empty());

    let empty = ScreenContext::default();
    assert!(empty.universe().is_empty());
    assert!(empty.bars("AAA").is_empty());
    assert!(empty.facts("AAA").is_empty());
    assert!(empty.indicators("AAA").is_empty());
}

#[test]
fn the_screening_vtable_carries_exactly_the_four_one_shot_callbacks() {
    // The capability contract is structural: the vtable is a plain C struct
    // with the ABI header and exactly `create`, `screen`, `result`,
    // `destroy`. There is no order/market callback and no extent for one, so
    // a screening program has no order surface by construction.
    assert_eq!(offset_of!(ScreeningProgramVTable, abi_version), 0);
    assert_eq!(offset_of!(ScreeningProgramVTable, reserved), 4);
    assert_eq!(offset_of!(ScreeningProgramVTable, create), 8);
    assert_eq!(
        offset_of!(ScreeningProgramVTable, screen),
        8 + size_of::<usize>()
    );
    assert_eq!(
        offset_of!(ScreeningProgramVTable, result),
        8 + 2 * size_of::<usize>()
    );
    assert_eq!(
        offset_of!(ScreeningProgramVTable, destroy),
        8 + 3 * size_of::<usize>()
    );
    assert_eq!(
        size_of::<ScreeningProgramVTable>(),
        8 + 4 * size_of::<usize>()
    );
    assert_eq!(
        SDK_ABI_VERSION, 2,
        "the shared SDK ABI version is the study bar cutover"
    );
}
