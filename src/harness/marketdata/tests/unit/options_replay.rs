use super::*;
use std::cell::RefCell;
use std::collections::HashMap;

use crate::catalog::{DailyFrontSet, MemoryContractsCatalog, OptionType};
use crate::clock::ManualClock;
use crate::events::{EventType, QuoteEvent, TradeEvent};
use crate::lake::partition_path;
use crate::options_universe::{OptionUniverseSpec, SelectionMode};
use crate::replay::StaticReader;
use crate::time::DateTime;
use num::Decimal18;
use std::str::FromStr;

fn dec(value: &str) -> Decimal18 {
    Decimal18::from_str(value).unwrap()
}

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

fn osi_symbol(root: &str, expiry: &str, right: char, strike: u32) -> String {
    format!("{root}{expiry}{right}{:08}", strike * 1000)
}

fn contract(symbol: &str, expiry: Date) -> ContractRecord {
    let right = symbol.as_bytes()[symbol.len() - 9] as char;
    let option_type = if right == 'P' {
        OptionType::Put
    } else {
        OptionType::Call
    };
    ContractRecord::new(
        symbol,
        "SPY",
        expiry,
        Decimal18::from_str("450").unwrap(),
        option_type,
        100,
        None,
        None,
    )
    .unwrap()
}

fn front_set_spec() -> OptionUniverseSpec {
    OptionUniverseSpec::new("SPY", SelectionMode::FrontSet, Vec::new(), None, None, None).unwrap()
}

fn option_file(day: Date, symbol: &str, data_type: DataType) -> FileQueryFile {
    FileQueryFile {
        asset_class: AssetClass::Option,
        data_type,
        symbol: symbol.to_string(),
        session_date: day,
        object_path: partition_path(day, AssetClass::Option, data_type, symbol).unwrap(),
        read_url: "https://example.invalid/option".to_string(),
        byte_size: 128,
        row_count: 1,
    }
}

fn quote_event(event_id: &str, symbol: &str, timestamp: DateTime, sequence: u64) -> MarketEvent {
    MarketEvent::Quote(QuoteEvent {
        event_id: event_id.to_string(),
        timestamp,
        symbol: symbol.to_string(),
        sequence,
        bid_price: dec("2.50"),
        ask_price: dec("2.60"),
        bid_size: dec("10"),
        ask_size: dec("10"),
        venue: None,
    })
}

fn trade_event(event_id: &str, symbol: &str, timestamp: DateTime, sequence: u64) -> MarketEvent {
    MarketEvent::Trade(TradeEvent {
        event_id: event_id.to_string(),
        timestamp,
        symbol: symbol.to_string(),
        sequence,
        price: dec("2.55"),
        quantity: dec("5"),
        venue: None,
        trade_id: None,
    })
}

/// A partition port that records exactly which sessions it was consulted for.
#[derive(Default)]
struct RecordingPort {
    files: HashMap<(Date, DataType, String), FileQueryFile>,
    calls: RefCell<Vec<(Date, DataType, String)>>,
}

impl OptionPartitionPort for RecordingPort {
    fn file(
        &self,
        session_date: Date,
        data_type: DataType,
        symbol: &str,
    ) -> Result<Option<FileQueryFile>, DataError> {
        self.calls
            .borrow_mut()
            .push((session_date, data_type, symbol.to_string()));
        Ok(self
            .files
            .get(&(session_date, data_type, symbol.to_string()))
            .cloned())
    }
}

/// A reader that records the session/symbol/data-type of every file read.
#[derive(Default)]
struct RecordingReader {
    events: HashMap<String, MarketEvent>,
    calls: RefCell<Vec<(Date, String, DataType)>>,
}

impl ParquetReader for RecordingReader {
    fn read(&self, file: &FileQueryFile) -> Result<Vec<MarketEvent>, DataError> {
        self.calls
            .borrow_mut()
            .push((file.session_date, file.symbol.clone(), file.data_type));
        Ok(self
            .events
            .get(&file.object_path)
            .cloned()
            .into_iter()
            .collect())
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

#[test]
fn front_set_days_replay_only_their_own_contracts() {
    let days = [date(2024, 1, 17), date(2024, 1, 18), date(2024, 1, 19)];
    let jan_expiry = date(2024, 1, 19);
    let symbols = [
        osi_symbol("SPY", "240119", 'C', 450),
        osi_symbol("SPY", "240119", 'P', 450),
    ];
    let contracts = vec![
        contract(&symbols[0], jan_expiry),
        contract(&symbols[1], jan_expiry),
    ];
    let front_sets: Vec<DailyFrontSet> = days
        .iter()
        .map(|day| DailyFrontSet {
            underlying: "SPY".to_string(),
            selection_date: *day,
            contracts: contracts.clone(),
        })
        .collect();
    let catalog = Rc::new(MemoryContractsCatalog {
        contracts: contracts.clone(),
        front_sets,
    });
    let universe = Rc::new(
        OptionUniverseView::new(
            Box::new(ManualClock::new(DateTime::midnight(date(2024, 1, 25)))),
            catalog,
            front_set_spec(),
            None,
        )
        .unwrap(),
    );

    let mut port = RecordingPort::default();
    let mut reader = RecordingReader::default();
    let mut expected = Vec::new();
    let mut ordinal = 0u64;
    for day in days {
        for symbol in &symbols {
            for data_type in [DataType::Quote, DataType::Trade] {
                ordinal += 1;
                let timestamp = DateTime::from_micros(
                    DateTime::midnight(day).micros() + 14 * 3_600 * 1_000_000 + ordinal as i64,
                );
                let event_id = format!("{symbol}-{}-{ordinal}", data_type.value());
                let event = if data_type == DataType::Quote {
                    quote_event(&event_id, symbol, timestamp, ordinal)
                } else {
                    trade_event(&event_id, symbol, timestamp, ordinal)
                };
                let file = option_file(day, symbol, data_type);
                reader
                    .events
                    .insert(file.object_path.clone(), event.clone());
                port.files.insert((day, data_type, symbol.clone()), file);
            }
        }
    }
    // Rebuild the event ids in deterministic partition order for comparison.
    let mut ordinal = 0u64;
    for _day in days {
        for symbol in &symbols {
            for data_type in [DataType::Quote, DataType::Trade] {
                ordinal += 1;
                expected.push(format!("{symbol}-{}-{ordinal}", data_type.value()));
            }
        }
    }

    let port = Rc::new(port);
    let reader = Rc::new(reader);
    let mut feed = OptionsReplayFeed::new(
        days.iter().map(|day| session(*day)).collect(),
        universe,
        port.clone(),
        reader.clone(),
        vec![DataType::Quote, DataType::Trade],
        Vec::new(),
    )
    .unwrap();

    let plans: Vec<OptionsDayPlan> = feed.days().map(|plan| plan.unwrap()).collect();
    assert_eq!(
        plans
            .iter()
            .map(|plan| plan.session.session_date)
            .collect::<Vec<_>>(),
        days.to_vec()
    );
    let expected_calls: Vec<(Date, DataType, String)> = days
        .iter()
        .flat_map(|day| {
            symbols.iter().flat_map(move |symbol| {
                [DataType::Quote, DataType::Trade]
                    .map(|data_type| (*day, data_type, symbol.clone()))
            })
        })
        .collect();
    assert_eq!(*port.calls.borrow(), expected_calls);
    for plan in &plans {
        assert_eq!(plan.symbols(), symbols.to_vec());
        assert_eq!(plan.files.len(), 4);
    }

    assert_eq!(
        feed.events()
            .unwrap()
            .iter()
            .map(|event| event.event_id().to_string())
            .collect::<Vec<_>>(),
        expected
    );
}

#[test]
fn two_year_front_set_run_stays_day_bounded() {
    let start = date(2024, 1, 15);
    let days: Vec<Date> = (0..504).map(|index| start.add_days(index)).collect();
    let far_expiry = date(2026, 12, 18);
    let symbols = [
        osi_symbol("SPY", "261218", 'C', 450),
        osi_symbol("SPY", "261218", 'P', 450),
    ];
    let contracts = vec![
        contract(&symbols[0], far_expiry),
        contract(&symbols[1], far_expiry),
    ];
    let front_sets: Vec<DailyFrontSet> = days
        .iter()
        .map(|day| DailyFrontSet {
            underlying: "SPY".to_string(),
            selection_date: *day,
            contracts: contracts.clone(),
        })
        .collect();
    let catalog = Rc::new(MemoryContractsCatalog {
        contracts: contracts.clone(),
        front_sets,
    });
    let universe = Rc::new(
        OptionUniverseView::new(
            Box::new(ManualClock::new(DateTime::midnight(date(2026, 12, 31)))),
            catalog,
            front_set_spec(),
            None,
        )
        .unwrap(),
    );

    let mut port = RecordingPort::default();
    let mut reader = RecordingReader::default();
    let mut ordinal = 0u64;
    for day in &days {
        for symbol in &symbols {
            for data_type in [DataType::Quote, DataType::Trade] {
                ordinal += 1;
                let timestamp = DateTime::from_micros(
                    DateTime::midnight(*day).micros() + 14 * 3_600 * 1_000_000 + ordinal as i64,
                );
                let event_id = format!("{symbol}-{}", data_type.value());
                let event = if data_type == DataType::Quote {
                    quote_event(&event_id, symbol, timestamp, ordinal)
                } else {
                    trade_event(&event_id, symbol, timestamp, ordinal)
                };
                let file = option_file(*day, symbol, data_type);
                reader.events.insert(file.object_path.clone(), event);
                port.files.insert((*day, data_type, symbol.clone()), file);
            }
        }
    }
    let port = Rc::new(port);
    let reader = Rc::new(reader);

    let mut feed = OptionsReplayFeed::new(
        days.iter().map(|day| session(*day)).collect(),
        universe,
        port.clone(),
        reader.clone(),
        vec![DataType::Quote, DataType::Trade],
        Vec::new(),
    )
    .unwrap();
    // Nothing is read until the stream is advanced.
    assert!(reader.calls.borrow().is_empty());
    assert!(port.calls.borrow().is_empty());

    let mut stream = feed.stream();
    for _ in 0..4 {
        assert!(stream.next().unwrap().is_ok());
    }
    let first_day_reads: Vec<Date> = reader.calls.borrow().iter().map(|call| call.0).collect();
    assert_eq!(first_day_reads, vec![days[0]; 4]);
    let remaining: Vec<MarketEvent> = stream.map(|event| event.unwrap()).collect();

    assert_eq!(4 + remaining.len(), days.len() * 4);
    for event in &remaining {
        let suffix = match event.event_type() {
            EventType::Quote => "quote",
            EventType::Trade => "trade",
            _ => unreachable!("options replay only emits quotes and trades"),
        };
        assert!(event.event_id().ends_with(suffix));
    }
    let expected_reads: Vec<Date> = days
        .iter()
        .flat_map(|day| std::iter::repeat_n(*day, 4))
        .collect();
    let reader_days: Vec<Date> = reader.calls.borrow().iter().map(|call| call.0).collect();
    assert_eq!(reader_days, expected_reads);
    let port_days: Vec<Date> = port.calls.borrow().iter().map(|call| call.0).collect();
    assert_eq!(port_days, expected_reads);
}

#[test]
fn repeated_runs_and_separate_feeds_are_deterministic() {
    let days = [date(2024, 1, 17), date(2024, 1, 18)];
    let symbol = osi_symbol("SPY", "240119", 'C', 450);
    let expiry = date(2024, 1, 19);
    let contracts = vec![contract(&symbol, expiry)];
    let front_sets: Vec<DailyFrontSet> = days
        .iter()
        .map(|day| DailyFrontSet {
            underlying: "SPY".to_string(),
            selection_date: *day,
            contracts: contracts.clone(),
        })
        .collect();
    let catalog = Rc::new(MemoryContractsCatalog {
        contracts,
        front_sets,
    });
    let mut port = RecordingPort::default();
    let mut reader = RecordingReader::default();
    for (index, day) in days.iter().enumerate() {
        let timestamp = DateTime::from_micros(
            DateTime::midnight(*day).micros() + 14 * 3_600 * 1_000_000 + index as i64,
        );
        let event = quote_event("option-quote", &symbol, timestamp, index as u64 + 1);
        let file = option_file(*day, &symbol, DataType::Quote);
        reader.events.insert(file.object_path.clone(), event);
        port.files
            .insert((*day, DataType::Quote, symbol.clone()), file);
    }
    let port = Rc::new(port);
    let reader = Rc::new(reader);

    let run = |port: Rc<RecordingPort>, reader: Rc<RecordingReader>| {
        let universe = Rc::new(
            OptionUniverseView::new(
                Box::new(ManualClock::new(DateTime::midnight(date(2024, 1, 25)))),
                catalog.clone(),
                front_set_spec(),
                None,
            )
            .unwrap(),
        );
        OptionsReplayFeed::new(
            days.iter().map(|day| session(*day)).collect(),
            universe,
            port,
            reader,
            vec![DataType::Quote],
            Vec::new(),
        )
        .unwrap()
    };

    let mut first = run(port.clone(), reader.clone());
    let mut second = run(port.clone(), reader.clone());
    assert_eq!(
        first
            .events()
            .unwrap()
            .iter()
            .map(|event| event.event_id().to_string())
            .collect::<Vec<_>>(),
        second
            .events()
            .unwrap()
            .iter()
            .map(|event| event.event_id().to_string())
            .collect::<Vec<_>>()
    );
    assert_eq!(first.fingerprints(), second.fingerprints());
    assert_eq!(first.fingerprints().file_fingerprint().len(), 64);
    assert_eq!(first.fingerprints().row_fingerprint().len(), 64);
    assert_eq!(first.fingerprints().data_fingerprint().len(), 64);
}

#[test]
fn underlying_streams_merge_with_option_files() {
    let day = date(2024, 1, 17);
    let symbol = osi_symbol("SPY", "240119", 'C', 450);
    let expiry = date(2024, 1, 19);
    let contracts = vec![contract(&symbol, expiry)];
    let catalog = Rc::new(MemoryContractsCatalog {
        contracts: contracts.clone(),
        front_sets: vec![DailyFrontSet {
            underlying: "SPY".to_string(),
            selection_date: day,
            contracts,
        }],
    });
    let universe = Rc::new(
        OptionUniverseView::new(
            Box::new(ManualClock::new(DateTime::midnight(date(2024, 1, 25)))),
            catalog,
            front_set_spec(),
            None,
        )
        .unwrap(),
    );
    let option_event = quote_event(
        "option-quote",
        &symbol,
        DateTime::from_micros(DateTime::midnight(day).micros() + 14 * 3_600 * 1_000_000 + 30),
        2,
    );
    let underlying = quote_event(
        "underlying-quote",
        "SPY",
        DateTime::from_micros(DateTime::midnight(day).micros() + 14 * 3_600 * 1_000_000 - 60),
        1,
    );
    let mut port = RecordingPort::default();
    let mut reader = RecordingReader::default();
    let file = option_file(day, &symbol, DataType::Quote);
    reader.events.insert(file.object_path.clone(), option_event);
    port.files
        .insert((day, DataType::Quote, symbol.clone()), file);

    let mut feed = OptionsReplayFeed::new(
        vec![session(day)],
        universe,
        Rc::new(port),
        Rc::new(reader),
        vec![DataType::Quote],
        vec![ReplayStream::new("SPY", Some(DataType::Quote), vec![underlying]).unwrap()],
    )
    .unwrap();
    assert_eq!(
        feed.events()
            .unwrap()
            .iter()
            .map(|event| event.event_id().to_string())
            .collect::<Vec<_>>(),
        vec!["underlying-quote", "option-quote"]
    );
}
