//! Subscription composition, capability separation, and exact routing.
use std::cell::RefCell;
use std::rc::Rc;

use harness_strategy_sdk::canonical_subscriptions;
use harness_strategy_sdk::effective_subscriptions;
use harness_strategy_sdk::route_bar;
use harness_strategy_sdk::route_quote;
use harness_strategy_sdk::route_trade;
use harness_strategy_sdk::BarEvent;
use harness_strategy_sdk::BarFrame;
use harness_strategy_sdk::Decimal;
use harness_strategy_sdk::Indicator;
use harness_strategy_sdk::IndicatorSequence;
use harness_strategy_sdk::MarketConsumer;
use harness_strategy_sdk::ObservationContext;
use harness_strategy_sdk::QuoteEvent;
use harness_strategy_sdk::Subscription;
use harness_strategy_sdk::TradeEvent;

struct Node {
    name: &'static str,
    sequence: IndicatorSequence,
    own: Vec<Subscription>,
    children: Vec<Node>,
    log: Rc<RefCell<Vec<String>>>,
}
impl MarketConsumer for Node {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        self.own.clone()
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        self.children
            .iter_mut()
            .map(|child| child as &mut dyn Indicator)
            .collect()
    }
    fn on_quote(&mut self, event: &QuoteEvent, _: &mut ObservationContext) {
        self.log
            .borrow_mut()
            .push(format!("{}:quote:{}", self.name, event.symbol));
    }
    fn on_trade(&mut self, event: &TradeEvent, _: &mut ObservationContext) {
        self.log
            .borrow_mut()
            .push(format!("{}:trade:{}", self.name, event.symbol));
    }
    fn on_bar(&mut self, frame: &BarFrame, _: &mut ObservationContext) {
        self.log.borrow_mut().push(format!(
            "{}:bars:{}:{:?}:{:?}",
            self.name,
            frame.interval,
            frame.bars_by_symbol.keys().collect::<Vec<_>>(),
            frame.missing_symbols
        ));
    }
}
impl Indicator for Node {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        None
    }
}

fn quote(symbol: &str) -> QuoteEvent<'_> {
    QuoteEvent {
        event_id: "q",
        timestamp_ns: 1,
        sequence: 1,
        symbol,
        bid: Decimal::ZERO,
        ask: Decimal::ZERO,
        bid_quantity: Decimal::ZERO,
        ask_quantity: Decimal::ZERO,
        venue: "test",
    }
}

#[test]
fn nested_requirements_are_canonical_and_routing_is_dependency_first_and_own_only() {
    let log = Rc::new(RefCell::new(Vec::new()));
    let child = Node {
        sequence: IndicatorSequence::ticks(10).unwrap(),
        name: "child",
        own: vec![
            Subscription::Quote {
                symbols: vec!["BBB".into(), "AAA".into()],
            },
            Subscription::Trade {
                symbols: vec!["CCC".into()],
            },
        ],
        children: vec![],
        log: log.clone(),
    };
    let mut parent = Node {
        sequence: IndicatorSequence::ticks(10).unwrap(),
        name: "parent",
        own: vec![
            Subscription::Quote {
                symbols: vec!["AAA".into()],
            },
            Subscription::Bars {
                symbols: vec!["AAA".into(), "MISSING".into()],
                interval: "1d".into(),
            },
        ],
        children: vec![child],
        log: log.clone(),
    };
    let inputs = effective_subscriptions(&mut parent).unwrap();
    assert_eq!(
        harness_strategy_sdk::serde_json::to_string(&inputs).unwrap(),
        r#"[{"data_type":"bars","symbols":["AAA","MISSING"],"interval":"1d"},{"data_type":"quote","symbols":["AAA","BBB"]},{"data_type":"trade","symbols":["CCC"]}]"#
    );
    route_quote(&mut parent, &quote("AAA"), &mut ObservationContext).unwrap();
    route_quote(&mut parent, &quote("BBB"), &mut ObservationContext).unwrap();
    route_quote(&mut parent, &quote("NOPE"), &mut ObservationContext).unwrap();
    let trade = TradeEvent {
        event_id: "t",
        timestamp_ns: 1,
        sequence: 2,
        symbol: "CCC",
        price: Decimal::ZERO,
        quantity: Decimal::ZERO,
        venue: "test",
        trade_id: "t",
    };
    route_trade(&mut parent, &trade, &mut ObservationContext).unwrap();
    let bar = |symbol| BarEvent {
        event_id: "b",
        timestamp_ns: 0,
        sequence: 1,
        symbol,
        open: Decimal::ZERO,
        high: Decimal::ZERO,
        low: Decimal::ZERO,
        close: Decimal::ZERO,
        volume: Decimal::ZERO,
    };
    let mut frame = BarFrame {
        interval: "1min",
        interval_start_ns: 0,
        interval_end_ns: 1,
        available_at_ns: 1,
        bars_by_symbol: [("AAA", bar("AAA")), ("BBB", bar("BBB"))].into(),
        missing_symbols: vec!["MISSING", "OTHER"],
    };
    route_bar(&mut parent, &frame, &mut ObservationContext).unwrap();
    frame.interval = "1d";
    route_bar(&mut parent, &frame, &mut ObservationContext).unwrap();
    assert_eq!(
        *log.borrow(),
        [
            "child:quote:AAA",
            "parent:quote:AAA",
            "child:quote:BBB",
            "child:trade:CCC",
            "parent:bars:1d:[\"AAA\"]:[\"MISSING\"]"
        ]
    );
}

#[test]
fn invalid_intervals_symbols_and_tagged_documents_are_rejected() {
    for input in [
        Subscription::Bars {
            symbols: vec!["AAA".into()],
            interval: "1m".into(),
        },
        Subscription::Quote { symbols: vec![] },
        Subscription::Trade {
            symbols: vec![" ".into()],
        },
    ] {
        assert!(canonical_subscriptions(vec![input]).is_err());
    }
    for document in [
        r#"{"data_type":"bars","symbols":["AAA"]}"#,
        r#"{"data_type":"quote","symbols":["AAA"],"interval":"1d"}"#,
        r#"{"data_type":"ticks","symbols":["AAA"]}"#,
    ] {
        assert!(harness_strategy_sdk::serde_json::from_str::<Subscription>(document).is_err());
    }
}

struct Repeated {
    child: Node,
}
impl MarketConsumer for Repeated {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.child]
    }
}

struct Cycle {
    _nonzero: u8,
    sequence: IndicatorSequence,
}
impl MarketConsumer for Cycle {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: vec!["AAA".into()],
        }]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![self]
    }
}
impl Indicator for Cycle {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        None
    }
}

#[test]
fn dependency_cycles_fail_without_recursing_forever() {
    let mut cycle = Cycle {
        _nonzero: 0,
        sequence: IndicatorSequence::default(),
    };
    assert!(effective_subscriptions(&mut cycle)
        .unwrap_err()
        .contains("cycle"));
    assert!(
        route_quote(&mut cycle, &quote("AAA"), &mut ObservationContext)
            .unwrap_err()
            .contains("cycle")
    );
}

struct AliasNode {
    calls: Rc<RefCell<usize>>,
    sequence: IndicatorSequence,
}
impl MarketConsumer for AliasNode {
    type Context = ObservationContext;
    fn consumer_identity(&self) -> (usize, &'static str) {
        (1, "shared-indicator")
    }
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: vec!["AAA".into()],
        }]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        *self.calls.borrow_mut() += 1;
    }
}
impl Indicator for AliasNode {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        None
    }
}
struct Shared {
    left: AliasNode,
    right: AliasNode,
}
impl MarketConsumer for Shared {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.left, &mut self.right]
    }
}

#[test]
fn repeated_dependency_identity_is_updated_once() {
    let calls = Rc::new(RefCell::new(0));
    let mut shared = Shared {
        left: AliasNode {
            calls: calls.clone(),
            sequence: IndicatorSequence::ticks(10).unwrap(),
        },
        right: AliasNode {
            calls: calls.clone(),
            sequence: IndicatorSequence::ticks(10).unwrap(),
        },
    };
    assert_eq!(effective_subscriptions(&mut shared).unwrap().len(), 1);
    route_quote(&mut shared, &quote("AAA"), &mut ObservationContext).unwrap();
    assert_eq!(*calls.borrow(), 1);
}

#[test]
fn equal_addresses_of_parent_and_first_field_do_not_hide_dependencies() {
    let log = Rc::new(RefCell::new(Vec::new()));
    let mut parent = Repeated {
        child: Node {
            sequence: IndicatorSequence::ticks(10).unwrap(),
            name: "child",
            own: vec![Subscription::Quote {
                symbols: vec!["AAA".into()],
            }],
            children: vec![],
            log: log.clone(),
        },
    };
    assert_eq!(effective_subscriptions(&mut parent).unwrap().len(), 1);
    route_quote(&mut parent, &quote("AAA"), &mut ObservationContext).unwrap();
    assert_eq!(log.borrow().len(), 1);
}

struct ZeroIndicator;
impl MarketConsumer for ZeroIndicator {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
}
impl Indicator for ZeroIndicator {
    fn sequence(&self) -> &IndicatorSequence {
        unreachable!()
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        unreachable!()
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        None
    }
}
struct ZeroOwner {
    dependencies: [ZeroIndicator; 2],
}
impl MarketConsumer for ZeroOwner {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        self.dependencies
            .iter_mut()
            .map(|dependency| dependency as &mut dyn Indicator)
            .collect()
    }
}
#[test]
fn distinct_zero_sized_indicators_are_rejected_not_silently_deduplicated() {
    let mut root = ZeroOwner {
        dependencies: [ZeroIndicator, ZeroIndicator],
    };
    assert!(effective_subscriptions(&mut root)
        .unwrap_err()
        .contains("zero-sized"));
    assert!(
        route_quote(&mut root, &quote("AAA"), &mut ObservationContext)
            .unwrap_err()
            .contains("zero-sized")
    );
}

struct Mutating {
    child: Node,
    changed: bool,
}
impl MarketConsumer for Mutating {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: if self.changed {
                vec!["AAA".into(), "DEP".into()]
            } else {
                vec!["AAA".into()]
            },
        }]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.child]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        self.changed = true;
    }
}
#[test]
fn callback_mutation_fails_even_when_effective_union_is_unchanged() {
    let mut root = Mutating {
        changed: false,
        child: Node {
            sequence: IndicatorSequence::ticks(10).unwrap(),
            name: "child",
            own: vec![Subscription::Quote {
                symbols: vec!["DEP".into()],
            }],
            children: vec![],
            log: Rc::new(RefCell::new(vec![])),
        },
    };
    let before = effective_subscriptions(&mut root).unwrap();
    assert!(
        route_quote(&mut root, &quote("AAA"), &mut ObservationContext)
            .unwrap_err()
            .contains("changed")
    );
    assert_eq!(before, effective_subscriptions(&mut root).unwrap());
}

struct DependencyMutation {
    own: Vec<Subscription>,
    sequence: IndicatorSequence,
}
impl MarketConsumer for DependencyMutation {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        self.own.clone()
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        self.own.push(Subscription::Quote {
            symbols: vec!["BBB".into()],
        });
    }
}
impl Indicator for DependencyMutation {
    fn sequence(&self) -> &IndicatorSequence {
        &self.sequence
    }
    fn sequence_mut(&mut self) -> &mut IndicatorSequence {
        &mut self.sequence
    }
    fn value(&self, _: &str) -> Option<Decimal> {
        None
    }
}
struct ParentMutation {
    dependency: DependencyMutation,
    calls: usize,
}
impl MarketConsumer for ParentMutation {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote {
            symbols: vec!["AAA".into(), "BBB".into()],
        }]
    }
    fn indicators(&mut self) -> Vec<&mut dyn Indicator> {
        vec![&mut self.dependency]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {
        self.calls += 1;
    }
}
#[test]
fn dependency_mutation_fails_before_parent_dispatch_with_unchanged_union() {
    let mut root = ParentMutation {
        dependency: DependencyMutation {
            sequence: IndicatorSequence::ticks(10).unwrap(),
            own: vec![Subscription::Quote {
                symbols: vec!["AAA".into()],
            }],
        },
        calls: 0,
    };
    let before = effective_subscriptions(&mut root).unwrap();
    assert!(
        route_quote(&mut root, &quote("AAA"), &mut ObservationContext)
            .unwrap_err()
            .contains("changed")
    );
    assert_eq!(root.calls, 0);
    assert_eq!(before, effective_subscriptions(&mut root).unwrap());
}
