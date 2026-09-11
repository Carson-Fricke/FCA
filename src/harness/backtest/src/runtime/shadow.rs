//! Shadow-mode observe-only broker routing and comparison.
//!
//! Port of the deterministic core of `harness.runtime.shadow`. Shadow mode
//! consumes the same events as paper while every broker intent is recorded by
//! an [`ObserveOnlyBrokerSink`] that structurally cannot transmit anything, and
//! a [`ShadowReport`] compares each intent's would-be simulated fills against
//! the live execution prints supplied by the deployment boundary. Nothing here
//! reads a wall clock, draws randomness, or opens a network client: live prints
//! enter as event-time-stamped records and every comparison is computed from
//! event-time data alone.

use std::collections::BTreeMap;

use num::Decimal18;

use crate::canonical_json::JsonValue;
use crate::runtime::paper::PaperSessionResult;
use crate::runtime::state_store::FillState;

/// The shadow comparison verdict.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShadowVerdict {
    /// Both sides exist and agree on every pinned field.
    Matched,
    /// Both sides exist and disagree on a pinned field.
    Diverged,
    /// A simulated fill exists with no live print available.
    WouldBeOnly,
    /// An intent the simulator never filled.
    Unfilled,
    /// A live print no broker intent explains.
    LiveOnly,
}

impl ShadowVerdict {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Matched => "matched",
            Self::Diverged => "diverged",
            Self::WouldBeOnly => "would_be_only",
            Self::Unfilled => "unfilled",
            Self::LiveOnly => "live_only",
        }
    }
}

/// One single-leg order the fleet would have routed to a live broker.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SingleLegOrder {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: String,
    /// String-exact quantity.
    pub quantity: String,
    /// Order type (for example `market`).
    pub order_type: String,
}

impl SingleLegOrder {
    /// Build a routable single-leg order.
    #[must_use]
    pub fn new(
        order_id: impl Into<String>,
        symbol: impl Into<String>,
        side: impl Into<String>,
        quantity: impl Into<String>,
    ) -> Self {
        Self {
            order_id: order_id.into(),
            symbol: symbol.into(),
            side: side.into(),
            quantity: quantity.into(),
            order_type: "market".to_string(),
        }
    }
}

/// One recorded broker intent with its 1-based sink ordinal.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BrokerIntent {
    /// 1-based ordinal in the owning sink's trail.
    pub sequence: u64,
    /// Order identifier.
    pub order_id: String,
    /// Owning strategy, if known.
    pub strategy_id: Option<String>,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: String,
    /// String-exact quantity.
    pub quantity: String,
    /// Order type.
    pub order_type: String,
}

/// The observe-only live-broker command boundary for shadow sessions.
///
/// The sink is the structural observe-only guarantee: it records each approved
/// single-leg order immutably and offers no transmission path of any kind.
#[derive(Clone, Debug, Default)]
pub struct ObserveOnlyBrokerSink {
    intents: Vec<BrokerIntent>,
}

impl ObserveOnlyBrokerSink {
    /// Create an empty sink.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// The complete immutable intent trail in record order.
    #[must_use]
    pub fn intents(&self) -> &[BrokerIntent] {
        &self.intents
    }

    /// How many intents the sink has recorded.
    #[must_use]
    pub fn len(&self) -> usize {
        self.intents.len()
    }

    /// Whether the sink has recorded nothing.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.intents.is_empty()
    }

    /// Record one order intent without transmitting anything.
    pub fn submit(&mut self, order: &SingleLegOrder, strategy_id: Option<&str>) -> BrokerIntent {
        let intent = BrokerIntent {
            sequence: self.intents.len() as u64 + 1,
            order_id: order.order_id.clone(),
            strategy_id: strategy_id.map(str::to_string),
            symbol: order.symbol.clone(),
            side: order.side.clone(),
            quantity: order.quantity.clone(),
            order_type: order.order_type.clone(),
        };
        self.intents.push(intent.clone());
        intent
    }
}

/// One live execution print supplied by the deployment boundary.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LivePrint {
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Order side.
    pub side: String,
    /// String-exact quantity.
    pub quantity: String,
    /// String-exact price.
    pub price: String,
    /// Exchange event time in epoch nanoseconds.
    pub timestamp_ns: i64,
}

/// One broker intent compared against its would-be and live outcomes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IntentComparison {
    /// The intent's 1-based sink ordinal.
    pub intent_sequence: u64,
    /// Order identifier.
    pub order_id: String,
    /// Comparison verdict.
    pub verdict: ShadowVerdict,
    /// Would-be simulated fills.
    pub would_be: Vec<FillState>,
    /// The matching live print, if any.
    pub live: Option<LivePrint>,
    /// Human-readable divergence detail, when diverged.
    pub detail: Option<String>,
}

/// The complete deterministic rehearsal artifact of one shadow session.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShadowReport {
    /// Run identifier.
    pub run_id: String,
    /// Deployment mode the session was built from.
    pub deployment_mode: String,
    /// Would-be session result.
    pub session: PaperSessionResult,
    /// The observe-only sink's trail.
    pub intents: Vec<BrokerIntent>,
    /// Exactly one comparison per intent.
    pub comparisons: Vec<IntentComparison>,
    /// Live prints no intent explains, sorted by order id.
    pub live_only_prints: Vec<LivePrint>,
}

impl ShadowReport {
    /// Whether live outcomes agree with would-be outcomes.
    #[must_use]
    pub fn parity_clean(&self) -> bool {
        self.live_only_prints.is_empty()
            && !self
                .comparisons
                .iter()
                .any(|comparison| comparison.verdict == ShadowVerdict::Diverged)
    }

    fn count(&self, verdict: ShadowVerdict) -> usize {
        self.comparisons
            .iter()
            .filter(|comparison| comparison.verdict == verdict)
            .count()
    }

    /// How many intents matched their live print exactly.
    #[must_use]
    pub fn matched_count(&self) -> usize {
        self.count(ShadowVerdict::Matched)
    }

    /// How many comparable pairs diverged.
    #[must_use]
    pub fn diverged_count(&self) -> usize {
        self.count(ShadowVerdict::Diverged)
    }

    /// How many intents have a simulated fill and no live print.
    #[must_use]
    pub fn would_be_only_count(&self) -> usize {
        self.count(ShadowVerdict::WouldBeOnly)
    }

    /// How many intents the simulator never filled.
    #[must_use]
    pub fn unfilled_count(&self) -> usize {
        self.count(ShadowVerdict::Unfilled)
    }

    /// How many live prints no intent explains.
    #[must_use]
    pub fn live_only_count(&self) -> usize {
        self.live_only_prints.len()
    }

    /// A one-line human-readable shadow rehearsal summary.
    #[must_use]
    pub fn summary(&self) -> String {
        if self.parity_clean() {
            return format!(
                "shadow parity clean ({}): {} intents, {} matched, {} without live prints, {} unfilled",
                self.run_id,
                self.intents.len(),
                self.matched_count(),
                self.would_be_only_count(),
                self.unfilled_count()
            );
        }
        let mut failures: Vec<String> = self
            .comparisons
            .iter()
            .filter(|comparison| comparison.verdict == ShadowVerdict::Diverged)
            .filter_map(|comparison| comparison.detail.clone())
            .collect();
        if !self.live_only_prints.is_empty() {
            failures.push(format!(
                "{} live prints without broker intents",
                self.live_only_count()
            ));
        }
        format!(
            "shadow parity diverged ({}): {}",
            self.run_id,
            failures.join("; ")
        )
    }

    /// The canonical, JSON-ready shadow report.
    #[must_use]
    pub fn artifacts(&self) -> JsonValue {
        JsonValue::object([
            ("schema", JsonValue::string("fca-shadow-report-v1")),
            ("run_id", JsonValue::String(self.run_id.clone())),
            (
                "deployment_mode",
                JsonValue::String(self.deployment_mode.clone()),
            ),
            ("parity_clean", JsonValue::Bool(self.parity_clean())),
            (
                "session",
                JsonValue::object([
                    (
                        "dispatched_count",
                        JsonValue::UInt(self.session.dispatched_count),
                    ),
                    (
                        "market_event_count",
                        JsonValue::UInt(self.session.market_event_count),
                    ),
                    ("fill_count", JsonValue::UInt(self.session.fill_count)),
                ]),
            ),
            (
                "counts",
                JsonValue::object([
                    ("intents", JsonValue::UInt(self.intents.len() as u64)),
                    ("matched", JsonValue::UInt(self.matched_count() as u64)),
                    ("diverged", JsonValue::UInt(self.diverged_count() as u64)),
                    (
                        "would_be_only",
                        JsonValue::UInt(self.would_be_only_count() as u64),
                    ),
                    ("unfilled", JsonValue::UInt(self.unfilled_count() as u64)),
                    ("live_only", JsonValue::UInt(self.live_only_count() as u64)),
                ]),
            ),
            (
                "intents",
                JsonValue::Array(self.intents.iter().map(intent_json).collect()),
            ),
            (
                "comparisons",
                JsonValue::Array(self.comparisons.iter().map(comparison_json).collect()),
            ),
            (
                "live_only_prints",
                JsonValue::Array(self.live_only_prints.iter().map(print_json).collect()),
            ),
        ])
    }
}

fn intent_json(intent: &BrokerIntent) -> JsonValue {
    JsonValue::object([
        ("sequence", JsonValue::UInt(intent.sequence)),
        ("order_id", JsonValue::String(intent.order_id.clone())),
        (
            "strategy_id",
            match &intent.strategy_id {
                Some(value) => JsonValue::String(value.clone()),
                None => JsonValue::Null,
            },
        ),
        ("symbol", JsonValue::String(intent.symbol.clone())),
        ("side", JsonValue::String(intent.side.clone())),
        ("quantity", JsonValue::String(intent.quantity.clone())),
        ("order_type", JsonValue::String(intent.order_type.clone())),
    ])
}

fn comparison_json(comparison: &IntentComparison) -> JsonValue {
    JsonValue::object([
        (
            "intent_sequence",
            JsonValue::UInt(comparison.intent_sequence),
        ),
        ("order_id", JsonValue::String(comparison.order_id.clone())),
        ("verdict", JsonValue::string(comparison.verdict.value())),
        (
            "detail",
            match &comparison.detail {
                Some(value) => JsonValue::String(value.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "would_be_fill_ids",
            JsonValue::Array(
                comparison
                    .would_be
                    .iter()
                    .map(|fill| JsonValue::String(fill.fill_id.clone()))
                    .collect(),
            ),
        ),
        (
            "live",
            match &comparison.live {
                Some(print) => print_json(print),
                None => JsonValue::Null,
            },
        ),
    ])
}

fn print_json(print: &LivePrint) -> JsonValue {
    JsonValue::object([
        ("order_id", JsonValue::String(print.order_id.clone())),
        ("symbol", JsonValue::String(print.symbol.clone())),
        ("side", JsonValue::String(print.side.clone())),
        ("quantity", JsonValue::String(print.quantity.clone())),
        ("price", JsonValue::String(print.price.clone())),
        ("timestamp_ns", JsonValue::Int(print.timestamp_ns)),
    ])
}

fn parse_decimal(value: &str) -> Decimal18 {
    value.parse::<Decimal18>().unwrap_or(Decimal18::ZERO)
}

/// Build the complete shadow report from an intent trail and live prints.
#[must_use]
pub fn build_report(
    run_id: impl Into<String>,
    deployment_mode: impl Into<String>,
    session: PaperSessionResult,
    intents: &[BrokerIntent],
    live_prints: &[LivePrint],
) -> ShadowReport {
    let mut fills_by_order: BTreeMap<&str, Vec<FillState>> = BTreeMap::new();
    for fill in &session.fills {
        fills_by_order
            .entry(fill.order_id.as_str())
            .or_default()
            .push(fill.clone());
    }
    let mut prints_by_order: BTreeMap<&str, &LivePrint> = BTreeMap::new();
    for print in live_prints {
        prints_by_order.insert(print.order_id.as_str(), print);
    }
    let intent_orders: Vec<&str> = intents
        .iter()
        .map(|intent| intent.order_id.as_str())
        .collect();
    let comparisons: Vec<IntentComparison> = intents
        .iter()
        .map(|intent| {
            let would_be = fills_by_order
                .get(intent.order_id.as_str())
                .map_or_else(Vec::new, Clone::clone);
            let live = prints_by_order
                .get(intent.order_id.as_str())
                .map(|print| (*print).clone());
            compare(intent, would_be, live)
        })
        .collect();
    let mut live_only: Vec<LivePrint> = live_prints
        .iter()
        .filter(|print| !intent_orders.contains(&print.order_id.as_str()))
        .cloned()
        .collect();
    live_only.sort_by(|left, right| left.order_id.cmp(&right.order_id));
    ShadowReport {
        run_id: run_id.into(),
        deployment_mode: deployment_mode.into(),
        session,
        intents: intents.to_vec(),
        comparisons,
        live_only_prints: live_only,
    }
}

fn compare(
    intent: &BrokerIntent,
    would_be: Vec<FillState>,
    live: Option<LivePrint>,
) -> IntentComparison {
    let make = |verdict, would_be, live, detail| IntentComparison {
        intent_sequence: intent.sequence,
        order_id: intent.order_id.clone(),
        verdict,
        would_be,
        live,
        detail,
    };
    if would_be.is_empty() && live.is_none() {
        return make(
            ShadowVerdict::Unfilled,
            Vec::new(),
            None,
            Some("no simulated fill and no live print".to_string()),
        );
    }
    if would_be.is_empty() {
        return make(
            ShadowVerdict::Diverged,
            Vec::new(),
            live,
            Some("no simulated fill; live printed".to_string()),
        );
    }
    let Some(live) = live else {
        return make(
            ShadowVerdict::WouldBeOnly,
            would_be,
            None,
            Some("no live print available".to_string()),
        );
    };
    let detail = first_would_be_difference(intent, &would_be, &live);
    let verdict = if detail.is_some() {
        ShadowVerdict::Diverged
    } else {
        ShadowVerdict::Matched
    };
    make(verdict, would_be, Some(live), detail)
}

/// Return the first pinned-field disagreement between the two outcomes.
#[must_use]
pub fn first_would_be_difference(
    intent: &BrokerIntent,
    would_be: &[FillState],
    live: &LivePrint,
) -> Option<String> {
    let mut quantity = Decimal18::ZERO;
    for fill in would_be {
        quantity = quantity
            .checked_add(parse_decimal(&fill.quantity))
            .unwrap_or(quantity);
    }
    if intent.symbol != live.symbol {
        return Some(format!("symbol {} != {}", intent.symbol, live.symbol));
    }
    if intent.side != live.side {
        return Some(format!("side {} != {}", intent.side, live.side));
    }
    if quantity != parse_decimal(&live.quantity) {
        return Some(format!("quantity {} != {}", quantity, live.quantity));
    }
    if would_be.len() == 1 && parse_decimal(&would_be[0].price) != parse_decimal(&live.price) {
        return Some(format!("price {} != {}", would_be[0].price, live.price));
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fill(fill_id: &str, order_id: &str, side: &str, quantity: &str, price: &str) -> FillState {
        FillState {
            event_id: "e".to_string(),
            timestamp: "2024-01-02T14:30:00.000000Z".to_string(),
            sequence: 0,
            fill_id: fill_id.to_string(),
            order_id: order_id.to_string(),
            symbol: "SPY".to_string(),
            side: side.to_string(),
            quantity: quantity.to_string(),
            price: price.to_string(),
            fee: "0".to_string(),
            liquidity: None,
        }
    }

    #[test]
    fn sink_records_intents_without_transmitting() {
        let mut sink = ObserveOnlyBrokerSink::new();
        let order = SingleLegOrder::new("o1", "SPY", "buy", "10");
        let intent = sink.submit(&order, Some("momentum"));
        assert_eq!(intent.sequence, 1);
        assert_eq!(sink.len(), 1);
        assert_eq!(sink.intents()[0].strategy_id.as_deref(), Some("momentum"));
    }

    #[test]
    fn matches_identical_live_prints() {
        let mut sink = ObserveOnlyBrokerSink::new();
        sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
        let mut session = PaperSessionResult::default();
        session.fills.push(fill("f1", "o1", "buy", "10", "100"));
        let prints = vec![LivePrint {
            order_id: "o1".to_string(),
            symbol: "SPY".to_string(),
            side: "buy".to_string(),
            quantity: "10".to_string(),
            price: "100".to_string(),
            timestamp_ns: 5,
        }];
        let report = build_report("run", "shadow", session, sink.intents(), &prints);
        assert!(report.parity_clean());
        assert_eq!(report.matched_count(), 1);
        assert_eq!(report.comparisons[0].verdict, ShadowVerdict::Matched);
    }

    #[test]
    fn reports_divergence_and_live_only_prints() {
        let mut sink = ObserveOnlyBrokerSink::new();
        sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
        let mut session = PaperSessionResult::default();
        session.fills.push(fill("f1", "o1", "buy", "10", "100"));
        let prints = vec![
            LivePrint {
                order_id: "o1".to_string(),
                symbol: "SPY".to_string(),
                side: "buy".to_string(),
                quantity: "10".to_string(),
                price: "101".to_string(),
                timestamp_ns: 5,
            },
            LivePrint {
                order_id: "o2".to_string(),
                symbol: "QQQ".to_string(),
                side: "sell".to_string(),
                quantity: "1".to_string(),
                price: "5".to_string(),
                timestamp_ns: 6,
            },
        ];
        let report = build_report("run", "shadow", session, sink.intents(), &prints);
        assert!(!report.parity_clean());
        assert_eq!(report.diverged_count(), 1);
        assert_eq!(report.live_only_count(), 1);
        assert!(report.summary().contains("diverged"));
    }

    #[test]
    fn unfilled_and_would_be_only_are_informational() {
        let mut sink = ObserveOnlyBrokerSink::new();
        sink.submit(&SingleLegOrder::new("o1", "SPY", "buy", "10"), None);
        sink.submit(&SingleLegOrder::new("o2", "SPY", "buy", "5"), None);
        let mut session = PaperSessionResult::default();
        session.fills.push(fill("f1", "o2", "buy", "5", "100"));
        let report = build_report("run", "shadow", session, sink.intents(), &[]);
        assert!(report.parity_clean());
        assert_eq!(report.unfilled_count(), 1);
        assert_eq!(report.would_be_only_count(), 1);
    }
}
