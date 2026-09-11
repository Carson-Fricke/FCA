use super::*;
use crate::catalog::{MemoryContractsCatalog, OptionType};
use crate::clock::ManualClock;
use crate::events::TradeEvent;
use crate::lake::partition_path;
use crate::options_universe::{OptionUniverseSpec, SelectionMode};
use crate::replay::StaticReader;
use crate::time::DateTime;
use num::Decimal18;
use std::str::FromStr;

fn date(year: i32, month: u32, day: u32) -> Date {
    Date::from_ymd(year, month, day).unwrap()
}

fn session(day: Date) -> ExchangeSession {
    ExchangeSession {
        session_date: day,
        open_time: DateTime::midnight(day),
        close_time: DateTime::midnight(day.add_days(1)),
    }
}

#[test]
fn options_replay_plans_selected_contracts() {
    let day = date(2024, 1, 2);
    let contract = crate::catalog::ContractRecord::new(
        "AAPL240119C00150000",
        "AAPL",
        date(2024, 1, 19),
        Decimal18::from_str("150").unwrap(),
        OptionType::Call,
        100,
        None,
        None,
    )
    .unwrap();
    let catalog = Rc::new(MemoryContractsCatalog {
        contracts: vec![contract.clone()],
        front_sets: Vec::new(),
    });
    let spec = OptionUniverseSpec::new(
        "AAPL",
        SelectionMode::StaticOsi,
        vec!["AAPL240119C00150000".to_string()],
        None,
        None,
        None,
    )
    .unwrap();
    let universe = Rc::new(
        OptionUniverseView::new(
            Box::new(ManualClock::new(DateTime::midnight(day))),
            catalog,
            spec,
            None,
        )
        .unwrap(),
    );
    let file = FileQueryFile {
        asset_class: AssetClass::Option,
        data_type: DataType::Trade,
        symbol: "AAPL240119C00150000".to_string(),
        session_date: day,
        object_path: partition_path(
            day,
            AssetClass::Option,
            DataType::Trade,
            "AAPL240119C00150000",
        )
        .unwrap(),
        read_url: "https://example.invalid/option".to_string(),
        byte_size: 1,
        row_count: 1,
    };
    let port = Rc::new(MemoryOptionPartitionPort { files: vec![file] });
    let reader = Rc::new(StaticReader {
        events: vec![MarketEvent::Trade(TradeEvent::new(
            "AAPL240119C00150000".to_string(),
            DateTime::midnight(day),
            1,
            Decimal18::from_str("1").unwrap(),
            Decimal18::from_str("1").unwrap(),
        ))],
    });
    let mut feed = OptionsReplayFeed::new(
        vec![session(day)],
        universe,
        port,
        reader,
        vec![DataType::Trade],
        Vec::new(),
    )
    .unwrap();
    let plan = feed.plan(&session(day)).unwrap();
    assert_eq!(plan.symbols(), vec!["AAPL240119C00150000"]);
    let events = feed.events().unwrap();
    assert_eq!(events.len(), 1);
    assert!(!feed.fingerprints().data.is_empty());
}
