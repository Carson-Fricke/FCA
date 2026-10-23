//! Deterministic records shared by the synchronous risk engine and rules.

use std::collections::BTreeMap;
use std::collections::HashMap;

use num::Decimal18;

use crate::error::require_text;
use crate::error::LiveError;
use crate::error::LiveResult;
use crate::orders::OrderRequest;
use crate::portfolio::hub::PortfolioSnapshot;
use crate::symbol::Symbol;

/// The three outcomes a risk rule or pipeline can produce.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RiskAction {
    /// Permit the order.
    Allow,
    /// Permit a reduced order.
    Reduce,
    /// Veto the order.
    Reject,
}

impl RiskAction {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Reduce => "reduce",
            Self::Reject => "reject",
        }
    }
}

/// Explicit point-in-time inputs supplied to a risk evaluation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskContext {
    /// The current portfolio snapshot.
    pub(crate) portfolio: PortfolioSnapshot,
    /// Explicit market prices for unmarked symbols.
    pub(crate) prices: HashMap<Symbol, Decimal18>,
    /// Current daily loss.
    pub(crate) daily_loss: Decimal18,
    /// Current order count.
    pub(crate) order_count: usize,
    /// Optional strategy attribution.
    pub(crate) strategy_id: Option<String>,
}

impl RiskContext {
    /// Construct a validated context.
    pub fn new(
        portfolio: PortfolioSnapshot,
        prices: HashMap<Symbol, Decimal18>,
        daily_loss: Decimal18,
        order_count: usize,
        strategy_id: Option<String>,
    ) -> LiveResult<Self> {
        let mut normalized = HashMap::new();
        for (symbol, price) in prices {
            if !price.is_positive() {
                return Err(LiveError::Risk("price must be positive".into()));
            }
            normalized.insert(symbol, price);
        }
        let strategy_id = match strategy_id {
            Some(value) => Some(require_text(&value, "strategy_id")?),
            None => None,
        };
        Ok(Self {
            portfolio,
            prices: normalized,
            daily_loss,
            order_count,
            strategy_id,
        })
    }

    /// An explicit price for `symbol`, if present.
    pub fn price(&self, symbol: &Symbol) -> Option<Decimal18> {
        self.prices.get(symbol).copied()
    }
}

/// One rule's immutable result for one candidate order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuleDecision {
    /// The rule outcome.
    pub action: RiskAction,
    /// The resulting order.
    pub order: OrderRequest,
    /// Optional reason.
    pub reason: Option<String>,
    /// Stringified metrics.
    pub metrics: BTreeMap<String, String>,
    /// Whether the outcome requests a halt.
    pub halt: bool,
}

impl RuleDecision {
    /// Build an allow result without a reason.
    pub fn allow(order: OrderRequest) -> Self {
        Self {
            action: RiskAction::Allow,
            order,
            reason: None,
            metrics: BTreeMap::new(),
            halt: false,
        }
    }

    /// Build a reduction result.
    pub fn reduce(order: OrderRequest, reason: &str) -> Self {
        Self {
            action: RiskAction::Reduce,
            order,
            reason: Some(reason.to_string()),
            metrics: BTreeMap::new(),
            halt: false,
        }
    }

    /// Build a rejection result, optionally marking a halt condition.
    pub fn reject(order: OrderRequest, reason: &str, halt: bool) -> Self {
        Self {
            action: RiskAction::Reject,
            order,
            reason: Some(reason.to_string()),
            metrics: BTreeMap::new(),
            halt,
        }
    }

    /// Attach a stringified metric.
    pub fn with_metric(mut self, key: &str, value: String) -> Self {
        self.metrics.insert(key.to_string(), value);
        self
    }
}

/// Complete audit record for one ordered rule invocation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskAudit {
    /// Zero-based invocation sequence.
    pub sequence: usize,
    /// One-based pipeline pass number.
    pub pass_number: usize,
    /// The invoked rule identifier.
    pub rule_id: String,
    /// The rule outcome.
    pub action: RiskAction,
    /// The order before the rule.
    pub order_before: OrderRequest,
    /// The order after the rule.
    pub order_after: OrderRequest,
    /// Optional reason.
    pub reason: Option<String>,
    /// Stringified metrics.
    pub metrics: BTreeMap<String, String>,
    /// Whether this invocation was a recheck after a reduction.
    pub recheck: bool,
    /// Whether the outcome requested a halt.
    pub halt: bool,
}

/// Final pipeline decision plus every rule invocation that produced it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskDecision {
    /// The final outcome.
    pub action: RiskAction,
    /// The unmodified request.
    pub original_order: OrderRequest,
    /// The final order.
    pub order: OrderRequest,
    /// The ordered audit trail.
    pub audits: Vec<RiskAudit>,
    /// Optional final reason.
    pub reason: Option<String>,
    /// All reasons in invocation order.
    pub reasons: Vec<String>,
    /// Strategy attribution.
    pub strategy_id: Option<String>,
    /// Non-`reject_order` halt scope when the pipeline vetoed.
    pub halt_scope: Option<String>,
}

impl RiskDecision {
    /// Whether the final order may be submitted.
    pub fn approved(&self) -> bool {
        self.action != RiskAction::Reject
    }

    /// Whether the final order was vetoed.
    pub fn rejected(&self) -> bool {
        self.action == RiskAction::Reject
    }

    /// Whether the approved order differs from the request.
    pub fn reduced(&self) -> bool {
        self.action == RiskAction::Reduce || self.order != self.original_order
    }

    /// Whether this decision requests a strategy/runtime halt.
    pub fn halted(&self) -> bool {
        self.halt_scope.is_some() || self.audits.iter().any(|audit| audit.halt)
    }
}

/// Structural contract implemented by synchronous risk rules.
pub trait RiskRule {
    /// The stable rule identifier.
    fn rule_id(&self) -> &str;

    /// Evaluate one candidate order against one explicit context.
    fn evaluate(&self, order: &OrderRequest, context: &RiskContext) -> LiveResult<RuleDecision>;
}
