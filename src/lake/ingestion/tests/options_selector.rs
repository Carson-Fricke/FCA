//! Integration coverage for the option front-set selector.
//!
//! Ports `tests/lake/ingestion/options/test_selector.py`: nearest two
//! expirations inside the inclusive spot-strike window, deterministic order,
//! invalid spot rejection, and front-set persistence through the catalog.

mod support;

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;

use lake_catalog::{
    ContractsCatalog, DailyFrontSet, DailyFrontSetRepository, MemoryContractRepository,
    MemoryDailyFrontSetRepository, OptionType,
};
use lake_configuration::UniverseSettings;
use lake_contracts::{Date, Decimal18};
use lake_ingestion::OptionChainSelector;
use support::{day, spy_option_contract};

fn selection_date() -> Date {
    day(2024, 1, 8)
}

fn universes() -> UniverseSettings {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    UniverseSettings::load(Some(&root), &BTreeMap::new()).expect("universes.yaml")
}

fn contract(
    symbol: &str,
    expiration: Date,
    strike: &str,
    option_type: OptionType,
) -> lake_catalog::ContractRecord {
    lake_catalog::ContractRecord::new(
        symbol,
        "SPY",
        expiration,
        Decimal18::parse(strike).expect("strike"),
        option_type,
        100,
        None,
        None,
    )
    .expect("valid contract")
}

fn candidates() -> Vec<lake_catalog::ContractRecord> {
    vec![
        contract(
            "SPY240119P00450000",
            day(2024, 1, 19),
            "450",
            OptionType::Put,
        ),
        contract(
            "SPY240112C00495000",
            day(2024, 1, 12),
            "495",
            OptionType::Call,
        ),
        contract(
            "SPY240112C00450000",
            day(2024, 1, 12),
            "450",
            OptionType::Call,
        ),
        contract(
            "SPY240112C00550000",
            day(2024, 1, 12),
            "550",
            OptionType::Call,
        ),
        contract(
            "SPY240119C00550000",
            day(2024, 1, 19),
            "550",
            OptionType::Call,
        ),
        contract(
            "SPY240126C00500000",
            day(2024, 1, 26),
            "500",
            OptionType::Call,
        ),
        lake_catalog::ContractRecord::new(
            "SPY240105C00500000",
            "SPY",
            day(2024, 1, 5),
            Decimal18::parse("500").expect("strike"),
            OptionType::Call,
            100,
            None,
            Some(day(2024, 1, 5)),
        )
        .expect("delisted contract"),
        lake_catalog::ContractRecord::new(
            "SPY240119C00500000",
            "SPY",
            day(2024, 1, 19),
            Decimal18::parse("500").expect("strike"),
            OptionType::Call,
            100,
            Some(day(2024, 1, 9)),
            None,
        )
        .expect("listed-later contract"),
        lake_catalog::ContractRecord::new(
            "QQQ240112C00495000",
            "QQQ",
            day(2024, 1, 12),
            Decimal18::parse("495").expect("strike"),
            OptionType::Call,
            100,
            None,
            None,
        )
        .expect("other underlying"),
    ]
}

#[test]
fn daily_selector_uses_nearest_two_expirations_and_inclusive_spot_window() {
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");
    let mut reversed = candidates();
    reversed.reverse();

    let selected = selector
        .select(
            "spy",
            selection_date(),
            Decimal18::parse("500").expect("spot"),
            &reversed,
        )
        .expect("selection");

    let symbols: Vec<&str> = selected.iter().map(|c| c.symbol.as_str()).collect();
    assert_eq!(
        symbols,
        vec![
            "SPY240112C00450000",
            "SPY240112C00495000",
            "SPY240112C00550000",
            "SPY240119P00450000",
            "SPY240119C00550000",
        ]
    );
    let expirations: std::collections::BTreeSet<Date> =
        selected.iter().map(|c| c.expiration_date).collect();
    assert_eq!(
        expirations,
        [day(2024, 1, 12), day(2024, 1, 19)].into_iter().collect()
    );
}

#[test]
fn selector_is_deterministic_and_rejects_invalid_spot() {
    let selection = universes().options.selection;
    let selector = OptionChainSelector::new(selection).expect("selector");
    let mut reversed = candidates();
    reversed.reverse();

    let forward = selector
        .select(
            "SPY",
            selection_date(),
            Decimal18::parse("500").expect("spot"),
            &candidates(),
        )
        .expect("forward");
    let backward = selector
        .select(
            "SPY",
            selection_date(),
            Decimal18::parse("500").expect("spot"),
            &reversed,
        )
        .expect("backward");
    assert_eq!(forward, backward, "candidate order must not matter");

    let error = selector
        .select("SPY", selection_date(), Decimal18::ZERO, &candidates())
        .expect_err("zero spot");
    assert!(
        error
            .to_string()
            .contains("spot must be a finite positive number"),
        "{error}"
    );
}

#[test]
fn catalog_persists_and_queries_daily_front_sets_by_underlying_date() {
    let catalog = ContractsCatalog::new(
        Arc::new(MemoryContractRepository::from_records(candidates())),
        Arc::new(MemoryDailyFrontSetRepository::new()),
    );
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");

    let persisted = selector
        .select_and_persist(
            &catalog,
            "SPY",
            selection_date(),
            Decimal18::parse("500").expect("spot"),
        )
        .expect("persisted");

    assert_eq!(
        catalog
            .get_front_set("spy", selection_date())
            .expect("front set"),
        Some(persisted.clone())
    );
    assert_eq!(
        catalog
            .list_front_sets(Some("SPY"), None, None)
            .expect("front sets"),
        vec![persisted.clone()]
    );
    assert_eq!(
        catalog
            .list_front_sets(Some("SPY"), Some(selection_date()), Some(selection_date()))
            .expect("front sets"),
        vec![persisted]
    );
    let stored = catalog.get("spy240112c00495000").expect("contract");
    assert_eq!(
        stored.map(|c| c.symbol),
        Some("SPY240112C00495000".to_string())
    );
}

#[test]
fn daily_front_set_replaces_same_underlying_date_and_sorts_contracts() {
    let first = contract(
        "SPY240112P00495000",
        day(2024, 1, 12),
        "495",
        OptionType::Put,
    );
    let second = contract(
        "SPY240112C00495000",
        day(2024, 1, 12),
        "495",
        OptionType::Call,
    );
    let repository = MemoryDailyFrontSetRepository::new();

    repository
        .upsert(
            &DailyFrontSet::new("spy", selection_date(), vec![first, second.clone()]).expect("set"),
        )
        .expect("upsert");
    let replacement = DailyFrontSet::new("SPY", selection_date(), vec![second]).expect("set");
    repository.upsert(&replacement).expect("replace");

    let stored = repository.get("SPY", selection_date()).expect("get");
    assert_eq!(
        stored.map(|set| set.symbols()),
        Some(vec!["SPY240112C00495000".to_string()])
    );
    assert_eq!(repository.list(None, None, None).expect("list").len(), 1);
}

#[test]
fn front_set_rejects_mixed_underlyings_and_duplicate_symbols() {
    let spy = spy_option_contract();
    let qqq = lake_catalog::ContractRecord::new(
        "QQQ240112C00495000",
        "QQQ",
        day(2024, 1, 12),
        Decimal18::parse("495").expect("strike"),
        OptionType::Call,
        100,
        None,
        None,
    )
    .expect("qqq");

    let error = DailyFrontSet::new("SPY", selection_date(), vec![spy.clone(), qqq])
        .expect_err("mixed underlyings");
    assert!(error.to_string().contains("share the selected underlying"));
    let error = DailyFrontSet::new("SPY", selection_date(), vec![spy.clone(), spy])
        .expect_err("duplicate symbols");
    assert!(error.to_string().contains("duplicate symbols"));
}

#[test]
fn selection_excludes_expired_contracts_and_respects_lifecycle() {
    let selector = OptionChainSelector::from_universes(&universes()).expect("selector");
    let selected = selector
        .select(
            "SPY",
            day(2024, 1, 8),
            Decimal18::parse("500").expect("spot"),
            &candidates(),
        )
        .expect("selection");
    let symbols: Vec<&str> = selected.iter().map(|c| c.symbol.as_str()).collect();
    assert!(!symbols.contains(&"SPY240105C00500000"), "expired excluded");
    assert!(
        !symbols.contains(&"SPY240119C00500000"),
        "listed after the selection date excluded"
    );
}
