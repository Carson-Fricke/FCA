use super::*;
use crate::models::OptionType;
use lake_contracts::Decimal18;

fn day(year: i32, month: u32, day_of_month: u32) -> Date {
    Date::from_ymd(year, month, day_of_month).expect("date")
}

fn contract(listed: Option<Date>, delisted: Option<Date>) -> ContractRecord {
    ContractRecord::new(
        "AAPL240119C00150000",
        "AAPL",
        day(2024, 1, 19),
        Decimal18::parse("150").expect("strike"),
        OptionType::Call,
        100,
        listed,
        delisted,
    )
    .expect("valid contract")
}

#[test]
fn contract_activity_rule_is_inclusive_of_both_bounds() {
    let dated = contract(Some(day(2023, 1, 1)), Some(day(2024, 1, 19)));
    assert!(!contract_is_active(&dated, day(2022, 12, 31)));
    assert!(contract_is_active(&dated, day(2023, 1, 1)));
    assert!(contract_is_active(&dated, day(2024, 1, 19)));
    assert!(!contract_is_active(&dated, day(2024, 1, 20)));

    let open = contract(None, None);
    assert!(contract_is_active(&open, day(1980, 1, 1)));
    assert!(contract_is_active(&open, day(2030, 1, 1)));

    let listed_only = contract(Some(day(2023, 1, 1)), None);
    assert!(!contract_is_active(&listed_only, day(2022, 12, 31)));
    assert!(contract_is_active(&listed_only, day(2023, 1, 1)));

    let delisted_only = contract(None, Some(day(2024, 1, 19)));
    assert!(contract_is_active(&delisted_only, day(2024, 1, 19)));
    assert!(!contract_is_active(&delisted_only, day(2024, 1, 20)));
}
