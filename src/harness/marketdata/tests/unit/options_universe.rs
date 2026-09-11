use super::*;
use crate::catalog::{MemoryContractsCatalog, OptionType};
use crate::clock::ManualClock;
use std::str::FromStr;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

fn instant(year: i32, month: u32, day: u32) -> DateTime {
    DateTime::from_ymd_hms(year, month, day, 12, 0, 0, 0).unwrap()
}

fn contract(symbol: &str, expiry: Date, strike: &str, option_type: OptionType) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "AAPL",
        expiry,
        Decimal18::from_str(strike).unwrap(),
        option_type,
        100,
        None,
        None,
    )
    .unwrap()
}

#[allow(clippy::too_many_arguments)]
fn contract_full(
    symbol: &str,
    expiry: Date,
    strike: &str,
    option_type: OptionType,
    listed: Option<Date>,
    delisted: Option<Date>,
) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "AAPL",
        expiry,
        Decimal18::from_str(strike).unwrap(),
        option_type,
        100,
        listed,
        delisted,
    )
    .unwrap()
}

/// Golden ordering and visibility from `options_universe._contract_sort_key`
/// and `_is_visible` evaluated on the same catalog rows.
#[test]
fn snapshot_order_matches_python_sort_key() {
    let records = vec![
        contract(
            "AAPL240119C00150000",
            date(2024, 1, 19),
            "150",
            OptionType::Call,
        ),
        contract_full(
            "AAPL250119C00150000",
            date(2025, 1, 19),
            "150",
            OptionType::Call,
            Some(date(2025, 1, 1)),
            None,
        ),
        contract_full(
            "AAPL250119P00140000",
            date(2025, 1, 19),
            "140",
            OptionType::Put,
            None,
            Some(date(2024, 5, 1)),
        ),
        contract(
            "AAPL250119C00160000",
            date(2025, 1, 19),
            "160",
            OptionType::Call,
        ),
        contract(
            "AAPL250119C00140000",
            date(2025, 1, 19),
            "140",
            OptionType::Call,
        ),
        contract(
            "AAPL241220C00150000",
            date(2024, 12, 20),
            "150",
            OptionType::Call,
        ),
        contract(
            "AAPL250119P00140000",
            date(2025, 1, 19),
            "140",
            OptionType::Put,
        ),
    ];
    let snapshot =
        OptionUniverseSnapshot::new("AAPL", date(2024, 6, 1), SelectionMode::StaticOsi, records)
            .unwrap();
    assert_eq!(
        snapshot.symbols(),
        vec![
            "AAPL241220C00150000",
            "AAPL250119C00140000",
            "AAPL250119P00140000",
            "AAPL250119C00160000",
        ]
    );
    assert_eq!(snapshot.front_expiry(), Some(date(2024, 12, 20)));
}

#[test]
fn static_universe_clips_to_engine_date() {
    let catalog = MemoryContractsCatalog {
        contracts: vec![
            contract(
                "AAPL240119C00150000",
                date(2024, 1, 19),
                "150",
                OptionType::Call,
            ),
            contract(
                "AAPL250119C00150000",
                date(2025, 1, 19),
                "150",
                OptionType::Call,
            ),
        ],
        front_sets: Vec::new(),
    };
    let spec = OptionUniverseSpec::new(
        "AAPL",
        SelectionMode::StaticOsi,
        vec![
            "AAPL240119C00150000".to_string(),
            "AAPL250119C00150000".to_string(),
        ],
        None,
        None,
        None,
    )
    .unwrap();
    let view = OptionUniverseView::new(
        Box::new(ManualClock::new(instant(2024, 6, 1))),
        Rc::new(catalog),
        spec,
        None,
    )
    .unwrap();
    let snapshot = view.resolve(None).unwrap();
    assert_eq!(snapshot.symbols(), vec!["AAPL250119C00150000"]);
    assert_eq!(snapshot.front_expiry(), Some(date(2025, 1, 19)));
}
