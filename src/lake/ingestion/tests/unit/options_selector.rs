use super::*;
use lake_catalog::OptionType;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("valid date")
}

fn contract(
    symbol: &str,
    expiration: Date,
    strike: &str,
    option_type: OptionType,
) -> ContractRecord {
    ContractRecord::new(
        symbol,
        "SPY",
        expiration,
        Decimal18::parse(strike).expect("strike"),
        option_type,
        100,
        None,
        None,
    )
    .expect("contract")
}

#[test]
fn active_on_respects_listed_and_delisted_bounds() {
    let listed = ContractRecord::new(
        "SPY240119C00500000",
        "SPY",
        day(2024, 1, 19),
        Decimal18::parse("500").expect("strike"),
        OptionType::Call,
        100,
        Some(day(2024, 1, 9)),
        Some(day(2024, 1, 26)),
    )
    .expect("contract");

    assert!(!active_on(&listed, day(2024, 1, 8)));
    assert!(active_on(&listed, day(2024, 1, 9)));
    assert!(active_on(&listed, day(2024, 1, 26)));
    assert!(!active_on(&listed, day(2024, 1, 27)));

    let unbounded = contract(
        "SPY240119C00500000",
        day(2024, 1, 19),
        "500",
        OptionType::Call,
    );
    assert!(active_on(&unbounded, day(2030, 1, 1)));
}

#[test]
fn contracts_compare_on_the_pinned_selection_order() {
    let call = contract(
        "SPY240112C00495000",
        day(2024, 1, 12),
        "495",
        OptionType::Call,
    );
    let put = contract(
        "SPY240112P00495000",
        day(2024, 1, 12),
        "495",
        OptionType::Put,
    );
    assert_eq!(compare_contracts(&call, &call), Ordering::Equal);
    assert_eq!(
        compare_contracts(&call, &put),
        Ordering::Less,
        "call before put"
    );

    let later = contract(
        "SPY240119C00495000",
        day(2024, 1, 19),
        "495",
        OptionType::Call,
    );
    assert_eq!(compare_contracts(&call, &later), Ordering::Less);

    let cheaper = contract(
        "SPY240112C00450000",
        day(2024, 1, 12),
        "450",
        OptionType::Call,
    );
    assert_eq!(compare_contracts(&call, &cheaper), Ordering::Greater);

    let other_symbol = contract(
        "SPY240112C00495001",
        day(2024, 1, 12),
        "495",
        OptionType::Call,
    );
    assert_eq!(compare_contracts(&call, &other_symbol), Ordering::Less);
}

#[test]
fn strike_window_bounds_are_inclusive_and_exact() {
    let selection = OptionSelection {
        frequency: "daily".to_string(),
        persist_front_set: true,
        expiration_order: "nearest".to_string(),
        expiration_count: 2,
        strike_window: lake_configuration::StrikeWindow {
            basis: "underlying_spot".to_string(),
            percent_below: 10.0,
            percent_above: 10.0,
        },
    };
    let selector = OptionChainSelector::new(selection).expect("selector");
    let (lower, upper) = selector
        .strike_bounds(Decimal18::parse("500").expect("spot"))
        .expect("bounds");
    assert_eq!(lower, Decimal18::parse("450").expect("lower"));
    assert_eq!(upper, Decimal18::parse("550").expect("upper"));
}

#[test]
fn selector_rejects_non_spot_basis_and_bad_spot() {
    let mut selection = OptionSelection {
        frequency: "daily".to_string(),
        persist_front_set: true,
        expiration_order: "nearest".to_string(),
        expiration_count: 2,
        strike_window: lake_configuration::StrikeWindow {
            basis: "underlying_spot".to_string(),
            percent_below: 10.0,
            percent_above: 10.0,
        },
    };
    OptionChainSelector::new(selection.clone()).expect("selector");

    selection.frequency = "weekly".to_string();
    let error = OptionChainSelector::new(selection.clone()).expect_err("frequency");
    assert_eq!(
        error.to_string(),
        "option selection frequency must be daily"
    );
    selection.frequency = "daily".to_string();

    selection.expiration_order = "farthest".to_string();
    let error = OptionChainSelector::new(selection.clone()).expect_err("order");
    assert_eq!(error.to_string(), "option selection order must be nearest");
    selection.expiration_order = "nearest".to_string();

    selection.strike_window.basis = "delta".to_string();
    let error = OptionChainSelector::new(selection).expect_err("basis");
    assert_eq!(
        error.to_string(),
        "strike window basis must be underlying_spot"
    );

    let selector = OptionChainSelector::new(daily_selection()).expect("selector");
    let error = selector
        .select("SPY", day(2024, 1, 8), Decimal18::ZERO, &[])
        .expect_err("zero spot");
    assert_eq!(error.to_string(), "spot must be a finite positive number");
    let error = selector
        .select(
            "SPY",
            day(2024, 1, 8),
            Decimal18::parse("-1").expect("spot"),
            &[],
        )
        .expect_err("negative spot");
    assert!(error.to_string().contains("finite positive"));

    let error = selector
        .select(
            "   ",
            day(2024, 1, 8),
            Decimal18::parse("500").expect("spot"),
            &[],
        )
        .expect_err("empty underlying");
    assert_eq!(error.to_string(), "underlying must not be empty");
}

fn daily_selection() -> OptionSelection {
    OptionSelection {
        frequency: "daily".to_string(),
        persist_front_set: true,
        expiration_order: "nearest".to_string(),
        expiration_count: 2,
        strike_window: lake_configuration::StrikeWindow {
            basis: "underlying_spot".to_string(),
            percent_below: 10.0,
            percent_above: 10.0,
        },
    }
}

#[test]
fn canonicalizes_the_underlying_before_filtering() {
    let selector = OptionChainSelector::new(daily_selection()).expect("selector");
    let candidates = vec![contract(
        "SPY240112C00495000",
        day(2024, 1, 12),
        "495",
        OptionType::Call,
    )];
    let selected = selector
        .select(
            "  spy ",
            day(2024, 1, 8),
            Decimal18::parse("500").expect("spot"),
            &candidates,
        )
        .expect("selection");
    assert_eq!(selected.len(), 1);
}
