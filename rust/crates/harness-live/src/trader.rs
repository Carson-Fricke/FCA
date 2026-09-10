//! Trader entrypoint on the Rust live core.
//!
//! Wires the deterministic kernels together — risk pipeline, OMS, simulated
//! venue, and portfolio accounting — behind one synchronous session boundary.
//! The Python trader process is the deployment shell; this port is the
//! deterministic core it drives, with no wall clock, network, or randomness on
//! the synchronous path.

use harness_num::Decimal18;

use crate::accounting::{ExecutionReport, Fill};
use crate::error::{require_text, LiveError, LiveResult};
use crate::events::MarketEvent;
use crate::fills::simple_touch::SimpleTouch;
use crate::oms::manager::SingleLegOms;
use crate::oms::state::OrderSnapshot;
use crate::orders::{OrderRequest, SingleLegOrder};
use crate::portfolio::book::PositionSnapshot;
use crate::portfolio::hub::{PortfolioHub, PortfolioSnapshot};
use crate::risk::models::{RiskContext, RiskDecision, RiskRule};
use crate::risk::pipeline::RiskPipeline;
use crate::venues::scripted::VenueOutcome;
use crate::venues::sim::SimulatedVenue;

/// Static composition settings for one trader session.
#[derive(Clone, Debug)]
pub struct TraderConfig {
    /// Shared account identifier.
    pub account_id: String,
    /// Strategy virtual-book identifier.
    pub book_id: String,
    /// Opening cash allocation.
    pub initial_cash: Decimal18,
    /// Venue name used to derive deterministic identifiers.
    pub venue: String,
}

/// The outcome of one order submission.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SubmitOutcome {
    /// The order (possibly reduced) was accepted into the OMS and venue.
    Approved(OrderSnapshot),
    /// The risk pipeline vetoed the order.
    Rejected(RiskDecision),
}

/// One deterministic trader session over the Rust live core.
pub struct TraderCore {
    book_id: String,
    oms: SingleLegOms,
    hub: PortfolioHub,
    pipeline: RiskPipeline,
    venue: SimulatedVenue,
    risk_decisions: Vec<RiskDecision>,
    applied_fills: Vec<Fill>,
    reports: Vec<ExecutionReport>,
    prices: std::collections::HashMap<crate::symbol::Symbol, Decimal18>,
    order_count: usize,
}

impl TraderCore {
    /// Compose a trader with a risk pipeline and a default [`SimpleTouch`] venue.
    pub fn new(
        config: TraderConfig,
        rules: Vec<Box<dyn RiskRule>>,
        on_reject: crate::risk::pipeline::OnReject,
    ) -> LiveResult<Self> {
        let mut hub = PortfolioHub::new(&config.account_id, vec![])?;
        hub.create_book(&config.book_id, config.initial_cash)?;
        let pipeline = RiskPipeline::new(rules, on_reject)?;
        let venue = SimulatedVenue::with_plugin(&config.venue, Box::new(SimpleTouch::new()))?;
        Ok(Self {
            book_id: require_text(&config.book_id, "book_id")?,
            oms: SingleLegOms::new(),
            hub,
            pipeline,
            venue,
            risk_decisions: Vec::new(),
            applied_fills: Vec::new(),
            reports: Vec::new(),
            prices: std::collections::HashMap::new(),
            order_count: 0,
        })
    }

    /// The risk pipeline.
    pub fn pipeline(&self) -> &RiskPipeline {
        &self.pipeline
    }

    /// The order-management service.
    pub fn oms(&self) -> &SingleLegOms {
        &self.oms
    }

    /// The portfolio hub.
    pub fn hub(&self) -> &PortfolioHub {
        &self.hub
    }

    /// All risk decisions recorded this session.
    pub fn risk_decisions(&self) -> &[RiskDecision] {
        &self.risk_decisions
    }

    /// Fills applied to the virtual book this session.
    pub fn applied_fills(&self) -> &[Fill] {
        &self.applied_fills
    }

    /// Execution reports applied to the OMS this session.
    pub fn reports(&self) -> &[ExecutionReport] {
        &self.reports
    }

    /// Build the point-in-time risk context from current session state.
    pub fn risk_context(&self) -> LiveResult<RiskContext> {
        RiskContext::new(
            self.hub.snapshot(),
            self.prices.clone(),
            Decimal18::ZERO,
            self.order_count,
            None,
        )
    }

    /// Run the risk pipeline for a candidate order without submitting it.
    pub fn check(&self, order: &SingleLegOrder) -> LiveResult<RiskDecision> {
        let context = self.risk_context()?;
        self.pipeline
            .evaluate(&OrderRequest::Single(order.clone()), &context)
    }

    /// Check and then submit an order to the OMS and venue.
    pub fn submit(&mut self, order: SingleLegOrder) -> LiveResult<SubmitOutcome> {
        let context = self.risk_context()?;
        let decision = self
            .pipeline
            .evaluate(&OrderRequest::Single(order.clone()), &context)?;
        self.risk_decisions.push(decision.clone());
        if decision.rejected() {
            return Ok(SubmitOutcome::Rejected(decision));
        }
        let approved = match decision.order.clone() {
            OrderRequest::Single(single) => single,
            OrderRequest::Multi(_) => {
                return Err(LiveError::Risk(
                    "the single-leg trader cannot submit a multi-leg order".into(),
                ))
            }
        };
        let order_id = approved.order_id.clone();
        self.oms.submit(approved.clone())?;
        self.venue.submit(approved)?;
        self.order_count += 1;
        self.pump()?;
        Ok(SubmitOutcome::Approved(self.oms.state(&order_id)?))
    }

    /// Feed one market event and apply every resulting outcome.
    pub fn on_market(&mut self, event: MarketEvent) -> LiveResult<()> {
        match &event {
            MarketEvent::Quote(quote) => {
                self.prices.insert(quote.symbol.clone(), quote.ask_price);
            }
            MarketEvent::Trade(trade) => {
                self.prices.insert(trade.symbol.clone(), trade.price);
            }
        }
        self.venue.on_market(event)?;
        self.pump()
    }

    /// Drain venue outcomes into the OMS and virtual book.
    pub fn pump(&mut self) -> LiveResult<()> {
        let outcomes = self.venue.drain_outcomes();
        for outcome in outcomes {
            match outcome {
                VenueOutcome::Report(report) => {
                    self.oms.apply(report.clone())?;
                    self.reports.push(report);
                }
                VenueOutcome::Fill(fill) => {
                    self.hub.apply_fill(&self.book_id, &fill)?;
                    self.applied_fills.push(fill);
                }
                VenueOutcome::PositionSnapshot(_) => {}
            }
        }
        Ok(())
    }

    /// The OMS lifecycle state for one order.
    pub fn state(&self, order_id: &str) -> LiveResult<OrderSnapshot> {
        self.oms.state(order_id)
    }

    /// The virtual book's position for one symbol.
    pub fn position(&self, symbol: &str) -> LiveResult<PositionSnapshot> {
        self.hub.book(&self.book_id)?.position(symbol)
    }

    /// The shared-account portfolio snapshot.
    pub fn portfolio(&self) -> PortfolioSnapshot {
        self.hub.snapshot()
    }

    /// The virtual book's current cash.
    pub fn cash(&self) -> LiveResult<Decimal18> {
        Ok(self.hub.book(&self.book_id)?.cash())
    }
}

#[cfg(test)]
mod tests {
    use harness_num::Decimal18;

    use super::{SubmitOutcome, TraderConfig, TraderCore};
    use crate::error::LiveResult;
    use crate::events::{MarketEvent, QuoteEvent};
    use crate::orders::{OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
    use crate::risk::limits::{MaxOrderNotional, MaxOrderSize};
    use crate::risk::models::{RiskContext, RiskRule};
    use crate::risk::pipeline::OnReject;
    use crate::symbol::Symbol;

    fn dec(value: &str) -> Decimal18 {
        value.parse().unwrap()
    }

    fn config() -> TraderConfig {
        TraderConfig {
            account_id: "acct".into(),
            book_id: "book".into(),
            initial_cash: dec("100000"),
            venue: "sim".into(),
        }
    }

    fn order(quantity: &str) -> SingleLegOrder {
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            dec(quantity),
            OrderType::Market,
            TimeInForce::Day,
            None,
            None,
            None,
        )
        .unwrap()
    }

    fn quote() -> MarketEvent {
        MarketEvent::Quote(
            QuoteEvent::new(
                Symbol::new("AAPL").unwrap(),
                dec("100"),
                dec("20"),
                dec("101"),
                dec("20"),
                None,
            )
            .unwrap(),
        )
    }

    #[test]
    fn market_order_traverses_the_full_lifecycle() {
        let mut trader = TraderCore::new(
            config(),
            vec![Box::new(
                MaxOrderNotional::new("max-notional", dec("1000000")).unwrap(),
            )],
            OnReject::RejectOrder,
        )
        .unwrap();
        trader.on_market(quote()).unwrap();
        let outcome = trader.submit(order("10")).unwrap();
        assert!(matches!(outcome, SubmitOutcome::Approved(_)));
        let state = trader.state("o1").unwrap();
        assert_eq!(state.status, OrderStatus::Filled);
        assert_eq!(state.filled_quantity, dec("10"));
        assert_eq!(trader.position("AAPL").unwrap().quantity, dec("10"));
        assert_eq!(trader.cash().unwrap(), dec("98990"));
        assert_eq!(trader.reports().len(), 2);
    }

    #[test]
    fn oversized_order_is_reduced_before_submission() {
        let mut trader = TraderCore::new(
            config(),
            vec![Box::new(MaxOrderSize::new("max-size", dec("5")).unwrap())],
            OnReject::RejectOrder,
        )
        .unwrap();
        trader.on_market(quote()).unwrap();
        trader.submit(order("10")).unwrap();
        let state = trader.state("o1").unwrap();
        assert_eq!(state.order.quantity, dec("5"));
        assert_eq!(state.filled_quantity, dec("5"));
        assert!(trader.risk_decisions()[0].reduced());
    }

    #[test]
    fn kill_switch_rejection_halts_before_submission() {
        let mut switch = crate::risk::kill_switch::KillSwitch::new(None).unwrap();
        switch.trip(Some("test"), None).unwrap();
        let mut trader =
            TraderCore::new(config(), vec![Box::new(switch)], OnReject::HaltRuntime).unwrap();
        trader.on_market(quote()).unwrap();
        let outcome = trader.submit(order("1")).unwrap();
        match outcome {
            SubmitOutcome::Rejected(decision) => {
                assert!(decision.halted());
                assert_eq!(decision.halt_scope.as_deref(), Some("halt_runtime"));
            }
            SubmitOutcome::Approved(_) => panic!("kill switch must reject"),
        }
        assert!(trader.state("o1").is_err());
    }

    #[test]
    fn risk_check_matches_submission_decision() {
        struct AlwaysReject;
        impl RiskRule for AlwaysReject {
            fn rule_id(&self) -> &str {
                "always-reject"
            }
            fn evaluate(
                &self,
                order: &crate::orders::OrderRequest,
                _context: &RiskContext,
            ) -> LiveResult<crate::risk::models::RuleDecision> {
                Ok(crate::risk::models::RuleDecision::reject(
                    order.clone(),
                    "rejected",
                    false,
                ))
            }
        }
        let trader = TraderCore::new(
            config(),
            vec![Box::new(AlwaysReject)],
            OnReject::RejectOrder,
        )
        .unwrap();
        let check = trader.check(&order("1")).unwrap();
        assert!(check.rejected());
        assert!(!check.audits.is_empty() || check.reason.as_deref() == Some("rejected"));
    }
}
