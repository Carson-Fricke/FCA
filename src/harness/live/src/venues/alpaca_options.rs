//! Alpaca options capability matrix, rejection taxonomy, and the consolidated
//! Alpaca venue capability document.
//!
//! Unsupported features are rejected before broker submission with an explicit
//! reason; nothing degrades silently. The rendered document is
//! `src/harness/venues/CAPABILITY_MATRIX.md` and must carry the same version
//! as [`CAPABILITY_MATRIX_VERSION`].

use std::collections::BTreeMap;
use std::collections::VecDeque;

use num::Decimal18;
use num::RoundingMode;

use crate::venues::alpaca::AlpacaClientOrderIdSource;
use crate::venues::alpaca::AlpacaCommandReceipt;
use crate::venues::alpaca::AsyncBrokerTransport;
use crate::venues::alpaca_crypto::is_crypto_pair;
use crate::venues::scripted::VenueOutcome;
use harness_trading::accounting::ExecutionReport;
use harness_trading::accounting::Fill;
use harness_trading::error::require_text;
use harness_trading::error::LiveError;
use harness_trading::error::LiveResult;
use harness_trading::orders::ExecutionType;
use harness_trading::orders::MultiLegOrder;
use harness_trading::orders::OrderLeg;
use harness_trading::orders::OrderRequest;
use harness_trading::orders::OrderStatus;
use harness_trading::orders::OrderType;
use harness_trading::orders::Side;
use harness_trading::orders::SingleLegOrder;
use harness_trading::orders::TimeInForce;
use harness_trading::symbol::is_osi_symbol;
use harness_trading::symbol::OsiSymbol;
use harness_trading::symbol::Symbol;

/// Version shared by the in-code matrix and the rendered capability document.
pub const CAPABILITY_MATRIX_VERSION: &str = "2026-09.1";

/// Path of the rendered capability document.
pub const CAPABILITY_MATRIX_PATH: &str = "src/harness/venues/CAPABILITY_MATRIX.md";

const ATTACHMENT_FEATURE_KEYS: [&str; 4] =
    ["take_profit", "stop_loss", "trailing_stop", "trail_price"];

/// Explicit rejection taxonomy for unsupported Alpaca options features.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum UnsupportedFeatureReason {
    /// Extended-hours flag.
    ExtendedHours,
    /// Bracket/OTO/OCO order class.
    OrderClass,
    /// Unsupported order type.
    OrderType,
    /// Unsupported time in force.
    TimeInForce,
    /// Leg-count violation.
    LegCount,
    /// Fractional ratio.
    LegRatio,
    /// Leg-shape violation.
    LegStructure,
    /// Non-integral quantity.
    Quantity,
    /// Order modification.
    OrderModification,
    /// Non-option asset class.
    AssetClass,
    /// Unknown feature key.
    UnknownFeature,
}

impl UnsupportedFeatureReason {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ExtendedHours => "extended_hours",
            Self::OrderClass => "order_class",
            Self::OrderType => "order_type",
            Self::TimeInForce => "time_in_force",
            Self::LegCount => "leg_count",
            Self::LegRatio => "leg_ratio",
            Self::LegStructure => "leg_structure",
            Self::Quantity => "quantity",
            Self::OrderModification => "order_modification",
            Self::AssetClass => "asset_class",
            Self::UnknownFeature => "unknown_feature",
        }
    }
}

/// One explicit unsupported-capability decision.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CapabilityRejection {
    /// The rejection category.
    pub reason: UnsupportedFeatureReason,
    /// The offending feature.
    pub feature: String,
    /// A human-readable detail.
    pub detail: String,
}

/// Whether a capability is supported.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum VenueCapabilitySupport {
    /// Supported.
    Supported,
    /// Unsupported.
    Unsupported,
}

impl VenueCapabilitySupport {
    /// The canonical wire spelling.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Supported => "supported",
            Self::Unsupported => "rejected",
        }
    }
}

/// One advertised capability.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VenueCapability {
    /// The feature name.
    pub feature: String,
    /// Support state.
    pub support: VenueCapabilitySupport,
    /// A human-readable detail.
    pub detail: String,
}

/// The versioned Alpaca options capability matrix.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaOptionsCapabilityMatrix {
    /// Version tag.
    pub version: String,
    /// Minimum accepted legs.
    pub min_legs: usize,
    /// Maximum number of legs accepted.
    pub max_legs: usize,
    /// Supported order types.
    pub order_types: Vec<OrderType>,
    /// Supported time-in-force values.
    pub supported_time_in_force: Vec<TimeInForce>,
    /// Whether extended-hours orders are supported.
    pub extended_hours: bool,
    /// Supported Alpaca order classes.
    pub order_classes: Vec<String>,
}

impl AlpacaOptionsCapabilityMatrix {
    /// The default Alpaca options matrix (multi-leg, day/GTC, no stops/IOC).
    pub fn alpaca_options_default() -> Self {
        Self {
            version: CAPABILITY_MATRIX_VERSION.to_string(),
            min_legs: 2,
            max_legs: 4,
            order_types: vec![OrderType::Market, OrderType::Limit],
            supported_time_in_force: vec![TimeInForce::Day, TimeInForce::Gtc],
            extended_hours: false,
            order_classes: vec!["mleg".to_string()],
        }
    }

    /// The matrix version tag.
    pub fn version(&self) -> &str {
        &self.version
    }

    /// Explicit rejections for one multi-leg order.
    pub fn check(&self, order: &MultiLegOrder) -> Vec<CapabilityRejection> {
        self.check_with_features(order, &BTreeMap::new())
    }

    /// Explicit rejections for one multi-leg order plus requested features.
    pub fn check_with_features(
        &self,
        order: &MultiLegOrder,
        features: &BTreeMap<String, String>,
    ) -> Vec<CapabilityRejection> {
        let mut rejections = self.feature_rejections(features);
        if !self.order_types.contains(&order.order_type) {
            rejections.push(rejection(
                UnsupportedFeatureReason::OrderType,
                "order_type",
                "order type is not supported for Alpaca options multi-leg orders",
            ));
        }
        if !self.supported_time_in_force.contains(&order.time_in_force) {
            rejections.push(rejection(
                UnsupportedFeatureReason::TimeInForce,
                order.time_in_force.as_str(),
                "time in force is not supported for multi-leg options",
            ));
        }
        if order.legs.len() < self.min_legs || order.legs.len() > self.max_legs {
            rejections.push(rejection(
                UnsupportedFeatureReason::LegCount,
                "legs",
                "leg count is outside the supported range",
            ));
        }
        if self.leg_structure_rejections(order, &mut rejections) {
            self.ratio_rejections(order, &mut rejections);
            self.quantity_rejections(order, &mut rejections);
        }
        rejections
    }

    /// Reject the order if any capability check fails.
    pub fn require(&self, order: &MultiLegOrder) -> LiveResult<()> {
        self.require_with_features(order, &BTreeMap::new())
    }

    /// Reject the order if any capability or requested-feature check fails.
    pub fn require_with_features(
        &self,
        order: &MultiLegOrder,
        features: &BTreeMap<String, String>,
    ) -> LiveResult<()> {
        let rejections = self.check_with_features(order, features);
        required(&rejections)
    }

    /// Render the advertised capabilities.
    pub fn to_capabilities(&self) -> Vec<VenueCapability> {
        vec![
            VenueCapability {
                feature: "options_multi_leg_submit".into(),
                support: VenueCapabilitySupport::Supported,
                detail: format!(
                    "mleg orders with {} to {} legs on one underlying",
                    self.min_legs, self.max_legs
                ),
            },
            VenueCapability {
                feature: "options_order_types".into(),
                support: VenueCapabilitySupport::Supported,
                detail: "market and limit orders; net-debit limits map to a positive wire \
                         limit_price and net-credit limits to a negative wire limit_price"
                    .into(),
            },
            VenueCapability {
                feature: "options_time_in_force".into(),
                support: VenueCapabilitySupport::Supported,
                detail: "day and gtc".into(),
            },
            VenueCapability {
                feature: "options_time_in_force_ioc".into(),
                support: VenueCapabilitySupport::Unsupported,
                detail: "ioc is rejected before broker submission".into(),
            },
            VenueCapability {
                feature: "options_cancel".into(),
                support: VenueCapabilitySupport::Supported,
                detail: "group-level cancel through the bound parent client order id".into(),
            },
            VenueCapability {
                feature: "options_replace".into(),
                support: VenueCapabilitySupport::Unsupported,
                detail: "Alpaca cannot modify multi-leg orders; cancel and resubmit instead".into(),
            },
            VenueCapability {
                feature: "options_extended_hours".into(),
                support: if self.extended_hours {
                    VenueCapabilitySupport::Supported
                } else {
                    VenueCapabilitySupport::Unsupported
                },
                detail: "extended-hours flags are rejected before broker submission".into(),
            },
            VenueCapability {
                feature: "options_brackets".into(),
                support: VenueCapabilitySupport::Unsupported,
                detail: "bracket, oto, oco, take-profit, and stop-loss attachments are rejected"
                    .into(),
            },
            VenueCapability {
                feature: "options_position_snapshot".into(),
                support: VenueCapabilitySupport::Supported,
                detail: "served by the shared AlpacaBroker equity positions path".into(),
            },
        ]
    }

    fn feature_rejections(&self, features: &BTreeMap<String, String>) -> Vec<CapabilityRejection> {
        let mut rejections = Vec::new();
        for (key, value) in features {
            match key.as_str() {
                "order_class" => {
                    if !self.order_classes.contains(value) {
                        rejections.push(rejection(
                            UnsupportedFeatureReason::OrderClass,
                            key,
                            "Alpaca options support the mleg class only",
                        ));
                    }
                }
                "extended_hours" => rejections.push(rejection(
                    UnsupportedFeatureReason::ExtendedHours,
                    key,
                    "extended-hours flags are not supported for Alpaca options",
                )),
                key if ATTACHMENT_FEATURE_KEYS.contains(&key) => rejections.push(rejection(
                    UnsupportedFeatureReason::OrderClass,
                    key,
                    "bracket and attachment legs are not supported",
                )),
                _ => rejections.push(rejection(
                    UnsupportedFeatureReason::UnknownFeature,
                    key,
                    "feature is not a known Alpaca options capability",
                )),
            }
        }
        rejections
    }

    fn leg_structure_rejections(
        &self,
        order: &MultiLegOrder,
        rejections: &mut Vec<CapabilityRejection>,
    ) -> bool {
        let mut structure_ok = true;
        let mut roots: Vec<String> = Vec::new();
        let mut symbols: Vec<String> = Vec::new();
        for leg in &order.legs {
            let symbol_text = leg.symbol.as_str();
            let Ok(osi) = OsiSymbol::new(symbol_text) else {
                rejections.push(rejection(
                    UnsupportedFeatureReason::LegStructure,
                    "legs",
                    "every leg must be an OSI option contract",
                ));
                structure_ok = false;
                continue;
            };
            let root = osi.root().to_string();
            if !roots.contains(&root) {
                roots.push(root);
            }
            if symbols.iter().any(|existing| existing == symbol_text) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::LegStructure,
                    "legs",
                    "the same contract appears more than once",
                ));
                structure_ok = false;
            }
            symbols.push(symbol_text.to_string());
        }
        if roots.len() > 1 {
            rejections.push(rejection(
                UnsupportedFeatureReason::LegStructure,
                "legs",
                "multi-leg options orders must share one underlying",
            ));
            structure_ok = false;
        }
        structure_ok
    }

    fn ratio_rejections(&self, order: &MultiLegOrder, rejections: &mut Vec<CapabilityRejection>) {
        for leg in &order.legs {
            if !is_integral(leg.ratio) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::LegRatio,
                    "ratio",
                    "leg ratios must be positive integers",
                ));
            }
        }
    }

    fn quantity_rejections(
        &self,
        order: &MultiLegOrder,
        rejections: &mut Vec<CapabilityRejection>,
    ) {
        let mut counts: Vec<Decimal18> = Vec::new();
        for leg in &order.legs {
            let count = leg.quantity / leg.ratio;
            if !is_integral(count) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::Quantity,
                    "quantity",
                    "spread count must be a positive integer",
                ));
            }
            if !counts.contains(&count) {
                counts.push(count);
            }
        }
        if counts.len() > 1 {
            rejections.push(rejection(
                UnsupportedFeatureReason::LegStructure,
                "legs",
                "legs must share one common spread count",
            ));
        }
    }
}

fn required(rejections: &[CapabilityRejection]) -> LiveResult<()> {
    if rejections.is_empty() {
        return Ok(());
    }
    let detail = rejections
        .iter()
        .map(|rejection| rejection.reason.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    Err(LiveError::Venue(format!(
        "unsupported alpaca options features: {detail}"
    )))
}

fn rejection(reason: UnsupportedFeatureReason, feature: &str, detail: &str) -> CapabilityRejection {
    CapabilityRejection {
        reason,
        feature: feature.to_string(),
        detail: detail.to_string(),
    }
}

fn is_integral(value: Decimal18) -> bool {
    value
        .round_to(RoundingMode::Truncate, 0)
        .map(|rounded| rounded == value)
        .unwrap_or(false)
}

fn capability(feature: &str, support: VenueCapabilitySupport, detail: &str) -> VenueCapability {
    VenueCapability {
        feature: feature.to_string(),
        support,
        detail: detail.to_string(),
    }
}

/// The versioned capability matrix consolidating the Alpaca venue family.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaCapabilityMatrix {
    /// Version tag.
    pub version: String,
    /// The options matrix.
    pub options: AlpacaOptionsCapabilityMatrix,
}

impl AlpacaCapabilityMatrix {
    /// The shipped consolidated Alpaca capability matrix.
    pub fn alpaca_default() -> Self {
        Self {
            version: CAPABILITY_MATRIX_VERSION.to_string(),
            options: AlpacaOptionsCapabilityMatrix::alpaca_options_default(),
        }
    }

    /// Every unsupported combination for one Alpaca order.
    pub fn check(&self, order: &OrderRequest) -> Vec<CapabilityRejection> {
        match order {
            OrderRequest::Multi(order) => self.options.check(order),
            OrderRequest::Single(order) => self.check_single(order),
        }
    }

    /// Reject the order when any capability check fails.
    pub fn require(&self, order: &OrderRequest) -> LiveResult<()> {
        required(&self.check(order))
    }

    /// The consolidated capability rows in document order.
    pub fn to_capabilities(&self) -> Vec<VenueCapability> {
        let mut rows = equity_capabilities();
        rows.extend(crypto_capabilities());
        rows.extend(self.options.to_capabilities());
        rows
    }

    /// Render the consolidated matrix as the capability document.
    pub fn to_markdown(&self) -> String {
        let rows = self
            .to_capabilities()
            .into_iter()
            .map(|capability| {
                format!(
                    "| {} | {} | {} |",
                    capability.feature,
                    capability.support.as_str(),
                    capability.detail
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        format!(
            "# Alpaca venue capability matrix\n\
             \n\
             Version: {}\n\
             \n\
             Consolidated capability policy for the Alpaca venue family. Unsupported\n\
             combinations are rejected explicitly before broker submission; nothing\n\
             degrades silently. This document is rendered from\n\
             `AlpacaCapabilityMatrix::to_markdown()` and must carry the\n\
             `CAPABILITY_MATRIX_VERSION` {}.\n\
             \n\
             | Feature | Support | Detail |\n\
             | --- | --- | --- |\n\
             {rows}\n",
            self.version, self.version
        )
    }

    fn check_single(&self, order: &SingleLegOrder) -> Vec<CapabilityRejection> {
        let symbol = order.symbol.as_str();
        let mut rejections = Vec::new();
        if is_osi_symbol(symbol) {
            rejections.push(rejection(
                UnsupportedFeatureReason::LegCount,
                "legs",
                "single-leg option orders are not supported; submit multi-leg spreads",
            ));
        } else if is_crypto_pair(symbol) {
            rejections.push(rejection(
                UnsupportedFeatureReason::AssetClass,
                "symbol",
                "crypto pairs route to the deterministic AlpacaCryptoVenue",
            ));
            if !matches!(order.order_type, OrderType::Market | OrderType::Limit) {
                rejections.push(rejection(
                    UnsupportedFeatureReason::OrderType,
                    "order_type",
                    "crypto orders support market and limit types only",
                ));
            }
        }
        rejections
    }
}

/// Command-side identity of one multi-leg group this adapter dispatched.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaOptionsOrderBinding {
    /// The kernel group order identifier.
    pub order_id: String,
    /// The client order identifier sent to the provider.
    pub client_order_id: String,
    /// The common integral spread count.
    pub spread_count: Decimal18,
    /// The bound group legs.
    pub legs: Vec<OrderLeg>,
}

/// Maps kernel group order ids to the Alpaca identities this adapter minted.
///
/// Alpaca cannot modify multi-leg orders, so unlike the equity directory there
/// are no replacement chains to link.
#[derive(Clone, Debug, Default)]
pub struct AlpacaOptionsOrderDirectory {
    by_client: BTreeMap<String, AlpacaOptionsOrderBinding>,
    client_for_order: BTreeMap<String, String>,
}

impl AlpacaOptionsOrderDirectory {
    /// Construct an empty directory.
    pub fn new() -> Self {
        Self::default()
    }

    /// Record an initial submit binding; identical rebinds are idempotent.
    pub fn bind(&mut self, binding: AlpacaOptionsOrderBinding) -> LiveResult<()> {
        if let Some(existing) = self.by_client.get(&binding.client_order_id) {
            if existing != &binding {
                return Err(LiveError::Venue(format!(
                    "client order id {:?} is already bound to a different order",
                    binding.client_order_id
                )));
            }
            return Ok(());
        }
        self.client_for_order
            .insert(binding.order_id.clone(), binding.client_order_id.clone());
        self.by_client
            .insert(binding.client_order_id.clone(), binding);
        Ok(())
    }

    /// Return the binding for a client order id, if this adapter minted it.
    pub fn binding_for_client(&self, client_order_id: &str) -> Option<&AlpacaOptionsOrderBinding> {
        self.by_client.get(client_order_id)
    }

    /// Return the client order id bound to a kernel group order id.
    pub fn client_order_id_for(&self, order_id: &str) -> Option<&str> {
        self.client_for_order.get(order_id).map(String::as_str)
    }
}

/// One leg identity available for leg-level report selection.
#[derive(Clone, Debug, PartialEq, Eq)]
struct FrameLeg {
    symbol: Symbol,
    side: Side,
    quantity: Decimal18,
}

/// Deterministic inbox that normalizes Alpaca options trade updates.
///
/// Group-level frames become group execution reports in spread counts;
/// leg-level frames become leg-scoped reports plus per-leg fills so each
/// virtual-book leg updates exactly once per leg fill. The inbox performs no
/// network, clock, or random access of its own.
#[derive(Clone, Debug)]
pub struct AlpacaOptionsExecutionInbox {
    directory: AlpacaOptionsOrderDirectory,
    outcomes: VecDeque<VenueOutcome>,
    sequence: i64,
    max_future_skew_millis: i64,
    max_event_age_millis: i64,
}

impl Default for AlpacaOptionsExecutionInbox {
    fn default() -> Self {
        Self {
            directory: AlpacaOptionsOrderDirectory::new(),
            outcomes: VecDeque::new(),
            sequence: 0,
            max_future_skew_millis: 5_000,
            max_event_age_millis: 300_000,
        }
    }
}

impl AlpacaOptionsExecutionInbox {
    /// Construct an empty inbox with the default timestamp tolerances.
    pub fn new() -> Self {
        Self::default()
    }

    /// The shared command-side identity directory.
    pub fn directory(&self) -> &AlpacaOptionsOrderDirectory {
        &self.directory
    }

    /// Mutable access to the identity directory.
    pub fn directory_mut(&mut self) -> &mut AlpacaOptionsOrderDirectory {
        &mut self.directory
    }

    /// The number of queued outcomes.
    pub fn len(&self) -> usize {
        self.outcomes.len()
    }

    /// Whether the inbox is empty.
    pub fn is_empty(&self) -> bool {
        self.outcomes.is_empty()
    }

    /// The next sequence number.
    pub fn next_sequence(&mut self) -> i64 {
        self.sequence += 1;
        self.sequence
    }

    /// Pop the next queued outcome.
    pub fn pop(&mut self) -> Option<VenueOutcome> {
        self.outcomes.pop_front()
    }

    /// Drain up to `limit` queued outcomes.
    pub fn drain(&mut self, limit: Option<usize>) -> Vec<VenueOutcome> {
        match limit {
            None => self.outcomes.drain(..).collect(),
            Some(limit) => self
                .outcomes
                .drain(..limit.min(self.outcomes.len()))
                .collect(),
        }
    }

    /// Normalize and atomically enqueue one decoded options trade-update frame.
    pub fn push_frame(
        &mut self,
        frame: &str,
        observed_at_millis: i64,
    ) -> LiveResult<Vec<VenueOutcome>> {
        let parsed: serde_json::Value = serde_json::from_str(frame).map_err(|error| {
            LiveError::Venue(format!("Alpaca broker frame is not valid JSON: {error}"))
        })?;
        let object = parsed
            .as_object()
            .ok_or_else(|| LiveError::Venue("Alpaca broker frame must be a JSON object".into()))?;
        match object.get("stream").and_then(serde_json::Value::as_str) {
            Some("trade_updates") => {}
            other => {
                return Err(LiveError::Venue(format!(
                    "unsupported Alpaca broker stream: {other:?}"
                )))
            }
        }
        let data = required_object_field(object, "data")?;
        let outcomes = self.normalize_trade_update(data, observed_at_millis)?;
        self.outcomes.extend(outcomes.iter().cloned());
        self.sequence += outcomes.len() as i64;
        Ok(outcomes)
    }

    fn normalize_trade_update(
        &self,
        data: &serde_json::Map<String, serde_json::Value>,
        observed_at_millis: i64,
    ) -> LiveResult<Vec<VenueOutcome>> {
        let event_name = required_text_entry(data, "event")?;
        let (status, execution_type) = trade_update_outcome(&event_name).ok_or_else(|| {
            LiveError::Venue(format!(
                "unsupported Alpaca broker trade update event: {event_name:?}"
            ))
        })?;
        let order = required_object_field(data, "order")?;
        let execution = match data.get("execution") {
            None | Some(serde_json::Value::Null) => None,
            Some(serde_json::Value::Object(object)) => Some(object),
            Some(_) => {
                return Err(LiveError::Venue(
                    "Alpaca field execution must be an object".into(),
                ))
            }
        };
        let event_millis = update_timestamp_millis(data, order, execution)?;
        self.check_timestamp(event_millis, observed_at_millis)?;

        let client_order_id = required_text_entry(order, "client_order_id")?;
        let binding = self.directory.binding_for_client(&client_order_id);
        let order_id = binding
            .map(|binding| binding.order_id.clone())
            .unwrap_or_else(|| client_order_id.clone());

        let Some(execution) = execution else {
            if is_fill_execution(execution_type) {
                return Err(LiveError::Venue(format!(
                    "Alpaca {event_name} update requires an execution object"
                )));
            }
            let order_quantity = required_positive_decimal(order, "qty")?;
            let filled_quantity = required_nonnegative_decimal(order, "filled_qty")?;
            if filled_quantity > order_quantity {
                return Err(LiveError::Venue(
                    "Alpaca field filled_qty cannot exceed qty".into(),
                ));
            }
            let report = ExecutionReport::new(
                &format!("alpaca:options-report:{client_order_id}:{}", self.sequence),
                &order_id,
                status,
                execution_type,
                order_quantity,
                filled_quantity,
                order_quantity - filled_quantity,
                optional_net_average(order, "filled_avg_price")?,
                Decimal18::ZERO,
                None,
                Some(format!("alpaca:{event_name}")),
            )?;
            return Ok(vec![VenueOutcome::Report(report)]);
        };

        let execution_id = required_text_entry(execution, "id")?;
        let legs = frame_legs(order, binding)?;
        let leg = select_frame_leg(execution, &legs)?;
        let fill_quantity = required_positive_decimal(execution, "qty")?;
        if fill_quantity > leg.quantity {
            return Err(LiveError::Venue(
                "Alpaca execution qty exceeds the leg quantity".into(),
            ));
        }
        let fill_price = required_positive_decimal(execution, "price")?;
        let report = ExecutionReport::new(
            &format!("alpaca:options-report:{client_order_id}:{}", self.sequence),
            &order_id,
            status,
            execution_type,
            leg.quantity,
            fill_quantity,
            leg.quantity - fill_quantity,
            optional_positive_decimal(execution, "avg_price")?,
            fill_quantity,
            Some(fill_price),
            Some(format!("alpaca:{event_name}:{}", leg.symbol.as_str())),
        )?;
        let mut outcomes = vec![VenueOutcome::Report(report)];
        if is_fill_execution(execution_type) {
            outcomes.push(VenueOutcome::Fill(Fill::new(
                &format!("alpaca:options-fill:{execution_id}"),
                &order_id,
                leg.symbol,
                leg.side,
                fill_quantity,
                fill_price,
                Decimal18::ZERO,
                None,
            )?));
        }
        Ok(outcomes)
    }

    fn check_timestamp(&self, moment_millis: i64, observed_at_millis: i64) -> LiveResult<()> {
        if moment_millis > observed_at_millis.saturating_add(self.max_future_skew_millis) {
            return Err(LiveError::Venue(
                "Alpaca broker timestamp exceeds the allowed future skew".into(),
            ));
        }
        if moment_millis < observed_at_millis.saturating_sub(self.max_event_age_millis) {
            return Err(LiveError::Venue(
                "Alpaca broker timestamp exceeds the maximum event age".into(),
            ));
        }
        Ok(())
    }
}

fn frame_legs(
    order: &serde_json::Map<String, serde_json::Value>,
    binding: Option<&AlpacaOptionsOrderBinding>,
) -> LiveResult<Vec<FrameLeg>> {
    if let Some(binding) = binding {
        return Ok(binding
            .legs
            .iter()
            .map(|leg| FrameLeg {
                symbol: leg.symbol.clone(),
                side: leg.side,
                quantity: leg.quantity,
            })
            .collect());
    }
    let Some(raw_legs) = order.get("legs") else {
        return Ok(Vec::new());
    };
    let array = raw_legs
        .as_array()
        .ok_or_else(|| LiveError::Venue("Alpaca order legs must be an array".into()))?;
    let mut legs = Vec::new();
    let mut symbols: Vec<String> = Vec::new();
    for raw_leg in array {
        let raw_leg = raw_leg
            .as_object()
            .ok_or_else(|| LiveError::Venue("Alpaca order legs must be objects".into()))?;
        let symbol_text = required_text_entry(raw_leg, "symbol")?;
        if !is_osi_symbol(&symbol_text) {
            return Err(LiveError::Venue(
                "Alpaca leg symbol is not an OSI contract".into(),
            ));
        }
        if symbols.contains(&symbol_text) {
            return Err(LiveError::Venue("Alpaca order repeats a leg symbol".into()));
        }
        symbols.push(symbol_text.clone());
        let side = parse_side(&required_text_entry(raw_leg, "side")?)?;
        legs.push(FrameLeg {
            symbol: Symbol::new(&symbol_text)?,
            side,
            quantity: required_positive_decimal(raw_leg, "qty")?,
        });
    }
    Ok(legs)
}

fn select_frame_leg(
    execution: &serde_json::Map<String, serde_json::Value>,
    legs: &[FrameLeg],
) -> LiveResult<FrameLeg> {
    let symbol_text = required_text_entry(execution, "symbol")?;
    legs.iter()
        .find(|leg| leg.symbol.as_str() == symbol_text)
        .cloned()
        .ok_or_else(|| {
            LiveError::Venue(
                "Alpaca execution references a contract that is not a leg of the order".into(),
            )
        })
}

fn trade_update_outcome(event: &str) -> Option<(OrderStatus, ExecutionType)> {
    let outcome = match event {
        "new" => (OrderStatus::New, ExecutionType::Acknowledged),
        "accept" => (OrderStatus::Accepted, ExecutionType::Acknowledged),
        "partial_fill" => (OrderStatus::PartiallyFilled, ExecutionType::PartialFill),
        "fill" => (OrderStatus::Filled, ExecutionType::Fill),
        "canceled" => (OrderStatus::Canceled, ExecutionType::Canceled),
        "expired" | "done_for_day" => (OrderStatus::Expired, ExecutionType::Expired),
        "replaced" => (OrderStatus::Replaced, ExecutionType::Replaced),
        "pending_replace" | "pending_cancel" => (OrderStatus::CancelPending, ExecutionType::Status),
        "rejected" => (OrderStatus::Rejected, ExecutionType::Rejected),
        "calculated" => (OrderStatus::Filled, ExecutionType::Status),
        _ => return None,
    };
    Some(outcome)
}

fn is_fill_execution(execution_type: ExecutionType) -> bool {
    matches!(
        execution_type,
        ExecutionType::PartialFill | ExecutionType::Fill
    )
}

fn parse_side(value: &str) -> LiveResult<Side> {
    match value {
        "buy" => Ok(Side::Buy),
        "sell" => Ok(Side::Sell),
        _ => Err(LiveError::Venue(format!(
            "Alpaca side {value:?} is invalid"
        ))),
    }
}

fn required_object_field<'a>(
    object: &'a serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<&'a serde_json::Map<String, serde_json::Value>> {
    match object.get(field) {
        None => Err(LiveError::Venue(format!(
            "Alpaca broker frame is missing {field:?}"
        ))),
        Some(serde_json::Value::Object(inner)) => Ok(inner),
        Some(_) => Err(LiveError::Venue(format!(
            "Alpaca field {field} must be an object"
        ))),
    }
}

fn required_text_entry(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<String> {
    match object.get(field) {
        Some(serde_json::Value::String(value)) if !value.trim().is_empty() => {
            Ok(value.trim().to_string())
        }
        _ => Err(LiveError::Venue(format!(
            "Alpaca field {field} must be non-empty text"
        ))),
    }
}

fn decimal_value(value: &serde_json::Value, field: &str) -> LiveResult<Decimal18> {
    let text = match value {
        serde_json::Value::String(value) => value.clone(),
        serde_json::Value::Number(value) => value.to_string(),
        _ => {
            return Err(LiveError::Venue(format!(
                "Alpaca field {field} must be an exact number"
            )))
        }
    };
    text.parse::<Decimal18>()
        .map_err(|_| LiveError::Venue(format!("Alpaca field {field} must be an exact number")))
}

fn required_decimal(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<Decimal18> {
    match object.get(field) {
        Some(value) => decimal_value(value, field),
        None => Err(LiveError::Venue(format!(
            "Alpaca broker frame is missing {field:?}"
        ))),
    }
}

fn required_positive_decimal(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<Decimal18> {
    let value = required_decimal(object, field)?;
    if !value.is_positive() {
        return Err(LiveError::Venue(format!(
            "Alpaca field {field} must be a positive number"
        )));
    }
    Ok(value)
}

fn required_nonnegative_decimal(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<Decimal18> {
    let value = required_decimal(object, field)?;
    if value.is_negative() {
        return Err(LiveError::Venue(format!(
            "Alpaca field {field} must not be negative"
        )));
    }
    Ok(value)
}

fn optional_positive_decimal(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<Option<Decimal18>> {
    match object.get(field) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(value) => {
            let parsed = decimal_value(value, field)?;
            if !parsed.is_positive() {
                return Err(LiveError::Venue(format!(
                    "Alpaca field {field} must be a positive number or null"
                )));
            }
            Ok(Some(parsed))
        }
    }
}

fn optional_net_average(
    object: &serde_json::Map<String, serde_json::Value>,
    field: &str,
) -> LiveResult<Option<Decimal18>> {
    match object.get(field) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(value) => {
            let parsed = decimal_value(value, field)?;
            Ok(parsed.is_positive().then_some(parsed))
        }
    }
}

fn provider_timestamp_millis(value: &serde_json::Value, field: &str) -> LiveResult<i64> {
    let text = value
        .as_str()
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(|| {
            LiveError::Venue(format!("Alpaca field {field} must be an ISO timestamp"))
        })?;
    chrono::DateTime::parse_from_rfc3339(text.trim())
        .map(|moment| moment.timestamp_millis())
        .map_err(|_| {
            LiveError::Venue(format!(
                "Alpaca field {field} must be a timezone-aware ISO timestamp"
            ))
        })
}

fn update_timestamp_millis(
    data: &serde_json::Map<String, serde_json::Value>,
    order: &serde_json::Map<String, serde_json::Value>,
    execution: Option<&serde_json::Map<String, serde_json::Value>>,
) -> LiveResult<i64> {
    let candidates = [
        data.get("at"),
        execution.and_then(|execution| execution.get("timestamp")),
        order.get("updated_at"),
    ];
    for candidate in candidates.into_iter().flatten() {
        if candidate.is_string() {
            if let Ok(millis) = provider_timestamp_millis(candidate, "at") {
                return Ok(millis);
            }
        }
    }
    Err(LiveError::Venue(
        "Alpaca broker trade update requires a usable ISO timestamp".into(),
    ))
}

/// The common integral spread count of one validated leg tuple.
pub fn spread_count(legs: &[OrderLeg]) -> LiveResult<Decimal18> {
    let mut counts: Vec<Decimal18> = Vec::new();
    for leg in legs {
        if !is_integral(leg.ratio) {
            return Err(LiveError::Venue(
                "leg ratios must be positive integers".into(),
            ));
        }
        let count = leg.quantity / leg.ratio;
        if !is_integral(count) {
            return Err(LiveError::Venue(
                "legs must form an integral spread count".into(),
            ));
        }
        if !counts.contains(&count) {
            counts.push(count);
        }
    }
    if counts.len() != 1 {
        return Err(LiveError::Venue(
            "legs must share one common spread count".into(),
        ));
    }
    Ok(counts[0])
}

fn mleg_order_payload(order: &MultiLegOrder, client_order_id: &str) -> LiveResult<String> {
    let spread = spread_count(&order.legs)?;
    let spread_i64 = spread
        .to_i64(RoundingMode::Truncate)
        .map_err(|_| LiveError::Venue("spread count does not fit an integer".into()))?;
    let first_side = spread_side(&order.legs);
    let mut payload = serde_json::json!({
        "qty": spread_i64.to_string(),
        "side": first_side.as_str(),
        "type": order.order_type.as_str(),
        "time_in_force": order.time_in_force.as_str(),
        "order_class": "mleg",
        "client_order_id": client_order_id,
    });
    let legs: Vec<serde_json::Value> = order
        .legs
        .iter()
        .map(|leg| {
            let ratio = leg.ratio.to_i64(RoundingMode::Truncate).unwrap_or(1);
            serde_json::json!({
                "symbol": leg.symbol.as_str(),
                "ratio_qty": ratio,
                "side": leg.side.as_str(),
                "position_intent": if leg.side == Side::Buy {
                    "buy_to_open"
                } else {
                    "sell_to_open"
                },
            })
        })
        .collect();
    payload["legs"] = serde_json::Value::Array(legs);
    if let Some(limit) = order.limit_price {
        let wire = if first_side == Side::Buy {
            limit
        } else {
            -limit
        };
        payload["limit_price"] = serde_json::Value::String(wire.to_string());
    }
    serde_json::to_string(&payload)
        .map_err(|error| LiveError::Venue(format!("failed to encode mleg order: {error}")))
}

/// The async Alpaca options multi-leg broker shell.
pub struct AlpacaOptionsBroker<T: AsyncBrokerTransport> {
    transport: T,
    base_url: String,
    id_source: AlpacaClientOrderIdSource,
    directory: AlpacaOptionsOrderDirectory,
    matrix: AlpacaOptionsCapabilityMatrix,
}

impl<T: AsyncBrokerTransport> AlpacaOptionsBroker<T> {
    /// Construct a broker over an injected transport and capability matrix.
    pub fn new(
        transport: T,
        base_url: &str,
        id_source: AlpacaClientOrderIdSource,
        matrix: AlpacaOptionsCapabilityMatrix,
    ) -> LiveResult<Self> {
        Ok(Self {
            transport,
            base_url: require_text(base_url, "base_url")?,
            id_source,
            directory: AlpacaOptionsOrderDirectory::new(),
            matrix,
        })
    }

    /// The identity directory.
    pub fn directory(&self) -> &AlpacaOptionsOrderDirectory {
        &self.directory
    }

    /// Submit a multi-leg group and bind its client identifier.
    pub async fn submit(&mut self, order: &MultiLegOrder) -> LiveResult<AlpacaCommandReceipt> {
        self.matrix.require(order)?;
        let client_order_id = match &order.client_order_id {
            Some(value) => value.clone(),
            None => self.id_source.next_client_order_id(),
        };
        self.directory.bind(AlpacaOptionsOrderBinding {
            order_id: order.order_id.clone(),
            client_order_id: client_order_id.clone(),
            spread_count: spread_count(&order.legs)?,
            legs: order.legs.clone(),
        })?;
        let body = mleg_order_payload(order, &client_order_id)?;
        let url = format!("{}/v2/orders", self.base_url);
        let response = self.transport.request("POST", &url, Some(body)).await?;
        if !response.is_success() {
            return Err(LiveError::Venue(format!(
                "alpaca options submit failed with status {}",
                response.status
            )));
        }
        Ok(AlpacaCommandReceipt {
            command: "submit".into(),
            client_order_id: Some(client_order_id),
            order_id: Some(order.order_id.clone()),
            status_code: response.status,
        })
    }

    /// Cancel a group by kernel order identifier.
    pub async fn cancel(&mut self, order_id: &str) -> LiveResult<AlpacaCommandReceipt> {
        let client = self
            .directory
            .client_order_id_for(order_id)
            .map(str::to_string)
            .ok_or_else(|| {
                LiveError::Venue(format!("unknown alpaca options group {order_id:?}"))
            })?;
        let url = format!("{}/v2/orders/{client}", self.base_url);
        let response = self.transport.request("DELETE", &url, None).await?;
        Ok(AlpacaCommandReceipt {
            command: "cancel".into(),
            client_order_id: Some(client),
            order_id: Some(order_id.to_string()),
            status_code: response.status,
        })
    }

    /// Alpaca cannot modify multi-leg orders, so replacement is rejected.
    pub fn replace_rejected(&self, order_id: &str) -> LiveError {
        LiveError::Venue(format!(
            "alpaca cannot modify multi-leg order {order_id}; cancel and resubmit instead"
        ))
    }
}

fn equity_capabilities() -> Vec<VenueCapability> {
    vec![
        capability(
            "equity_single_leg_submit",
            VenueCapabilitySupport::Supported,
            "market, limit, stop, and stop-limit orders on one equity symbol",
        ),
        capability(
            "equity_time_in_force",
            VenueCapabilitySupport::Supported,
            "day, gtc, and ioc time in force",
        ),
        capability(
            "equity_replace",
            VenueCapabilitySupport::Supported,
            "quantity, time in force, and prices; symbol, side, and order type are fixed",
        ),
        capability(
            "equity_multi_leg",
            VenueCapabilitySupport::Unsupported,
            "multi-leg orders submit through the options adapter only",
        ),
        capability(
            "equity_crypto_pairs",
            VenueCapabilitySupport::Unsupported,
            "crypto pairs route to the deterministic AlpacaCryptoVenue",
        ),
        capability(
            "equity_brackets",
            VenueCapabilitySupport::Unsupported,
            "bracket, oto, oco, take-profit, and stop-loss attachments are rejected",
        ),
    ]
}

fn crypto_capabilities() -> Vec<VenueCapability> {
    vec![
        capability(
            "crypto_fractional_submit",
            VenueCapabilitySupport::Supported,
            "fractional market and limit orders on BASE/QUOTE pairs, continuous across UTC days",
        ),
        capability(
            "crypto_replace",
            VenueCapabilitySupport::Supported,
            "atomic replace with symbol and side fixed",
        ),
        capability(
            "crypto_fee_reconciliation",
            VenueCapabilitySupport::Supported,
            "every fill reconciles the local maker-taker model against the venue fee schedule",
        ),
        capability(
            "crypto_position_snapshot",
            VenueCapabilitySupport::Unsupported,
            "positions belong to the accounting hub; the venue validates the command only",
        ),
    ]
}

/// Validate that every leg is an OSI option on one underlying root.
pub fn require_single_underlying(order: &MultiLegOrder) -> LiveResult<()> {
    let mut root: Option<String> = None;
    for leg in &order.legs {
        let osi = OsiSymbol::new(leg.symbol.as_str())
            .map_err(|_| LiveError::Venue("leg is not an OSI option contract".into()))?;
        match &root {
            None => root = Some(osi.root().to_string()),
            Some(existing) if existing != osi.root() => {
                return Err(LiveError::Venue(
                    "every leg must share one underlying root".into(),
                ));
            }
            Some(_) => {}
        }
    }
    Ok(())
}

/// Validate a symbol is an option contract.
pub fn require_option_symbol(symbol: &Symbol) -> LiveResult<()> {
    if is_osi_symbol(symbol.as_str()) {
        Ok(())
    } else {
        Err(LiveError::Venue(
            "symbol must be an OSI option contract".into(),
        ))
    }
}

/// The signed spread direction: the first leg's side classifies the group.
pub fn spread_side(legs: &[harness_trading::orders::OrderLeg]) -> Side {
    legs.first().map(|leg| leg.side).unwrap_or(Side::Buy)
}
