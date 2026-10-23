//! Owns immutable replay outcomes and canonical artifact projection; no execution or publication.
use crate::backtest::fees::FeeAggregation;
use crate::backtest::fees::FeeAssessment;
use crate::backtest::fixture::DataPreflight;
use crate::backtest::manifest::BacktestManifest;
use crate::backtest::portfolio::portfolio_json;
use crate::backtest::portfolio::PortfolioSnapshot;
use harness_contracts::canonical_json::JsonValue;
use harness_trading::program::*;
use harness_trading::replay_errors::BacktestStatus;
use std::collections::BTreeMap;

use super::risk::RiskDecision;

/// The complete outcome of one native engine run.
#[derive(Clone, Debug)]
pub struct BacktestOutcome {
    /// Reproducibility manifest.
    pub manifest: BacktestManifest,
    /// Terminal status.
    pub status: BacktestStatus,
    /// Abort reason, if any.
    pub reason: Option<String>,
    /// Preflight evidence.
    pub preflight: DataPreflight,
    /// Bus dispatch hash.
    pub dispatch_hash: String,
    /// Bus dispatched count.
    pub dispatched_count: u64,
    /// Raw replay events consumed, including bars whose intervals end after the window.
    pub market_event_count: u64,
    /// Atomic study frames dispatched (not the raw bar count).
    pub bar_frame_count: u64,
    /// Approved orders.
    pub order_count: u64,
    /// Booked fills.
    pub fills: Vec<EngineFill>,
    /// Causal cash/inventory accounting with compact exact sample histories.
    pub valuations: super::valuation::ValuationHistory,
    /// Fee assessments.
    pub fee_assessments: Vec<FeeAssessment>,
    /// Fee aggregation.
    pub fee_totals: FeeAggregation,
    /// Portfolio snapshot.
    pub portfolio: PortfolioSnapshot,
    /// Risk decisions.
    pub risk_decisions: Vec<RiskDecision>,
    /// Halted strategies.
    pub halted_strategies: Vec<String>,
    /// Stopped strategies.
    pub stopped_strategies: Vec<String>,
    /// Teardown errors.
    pub teardown_errors: Vec<String>,
    /// Lookahead violations.
    pub lookahead_violations: Vec<String>,
    /// Custom finite numeric observations published by research or strategy code.
    pub custom_metrics: BTreeMap<String, String>,
    /// Host-validated structured exploratory results.
    pub exploratory_outputs: BTreeMap<String, serde_json::Value>,
}

impl BacktestOutcome {
    /// Whether the run replayed its complete fixture.
    #[must_use]
    pub fn completed(&self) -> bool {
        self.status == BacktestStatus::Completed
    }

    /// Return the canonical, JSON-ready run artifacts.
    #[must_use]
    pub fn artifacts(&self) -> JsonValue {
        let mut counts = BTreeMap::from([
            (
                "dispatched".to_string(),
                JsonValue::UInt(self.dispatched_count),
            ),
            (
                "market_events".to_string(),
                JsonValue::UInt(self.market_event_count),
            ),
            ("orders".to_string(), JsonValue::UInt(self.order_count)),
            (
                "fills".to_string(),
                JsonValue::UInt(self.fills.len() as u64),
            ),
            (
                "risk_decisions".to_string(),
                JsonValue::UInt(self.risk_decisions.len() as u64),
            ),
        ]);
        if self.preflight.bar_count > 0 {
            counts.insert(
                "bar_frames".to_string(),
                JsonValue::UInt(self.bar_frame_count),
            );
        }
        let mut entries = vec![
            ("manifest", self.manifest.to_json()),
            ("status", JsonValue::string(self.status.value())),
            (
                "reason",
                match &self.reason {
                    Some(reason) => JsonValue::String(reason.clone()),
                    None => JsonValue::Null,
                },
            ),
            ("preflight", self.preflight.to_json()),
            (
                "dispatch_hash",
                JsonValue::String(self.dispatch_hash.clone()),
            ),
            ("counts", JsonValue::Object(counts)),
            (
                "strategies",
                JsonValue::object([
                    ("halted", string_array(&self.halted_strategies)),
                    ("stopped", string_array(&self.stopped_strategies)),
                    ("teardown_errors", string_array(&self.teardown_errors)),
                    (
                        "lookahead_violations",
                        string_array(&self.lookahead_violations),
                    ),
                ]),
            ),
            (
                "fills",
                JsonValue::Array(self.fills.iter().map(fill_json).collect()),
            ),
            ("fees", fees_json(&self.fee_assessments, &self.fee_totals)),
            ("portfolio", portfolio_json(&self.portfolio)),
            (
                "risk",
                JsonValue::Array(self.risk_decisions.iter().map(risk_json).collect()),
            ),
        ];
        if !self.custom_metrics.is_empty() {
            entries.push((
                "custom_metrics",
                JsonValue::object(
                    self.custom_metrics
                        .iter()
                        .map(|(name, value)| (name.clone(), JsonValue::String(value.clone()))),
                ),
            ));
        }
        JsonValue::object(entries)
    }

    /// Return the stable byte-for-byte artifact document for this run.
    #[must_use]
    pub fn artifact_json(&self) -> String {
        self.artifacts().to_canonical_string()
    }

    /// Return the SHA-256 digest of the complete run artifact.
    #[must_use]
    pub fn artifact_digest(&self) -> String {
        harness_contracts::sha256::digest_hex(self.artifact_json().as_bytes())
    }

    /// Return the SHA-256 digest of the run outcome without provenance.
    #[must_use]
    pub fn outcome_digest(&self) -> String {
        let artifacts = self.artifacts();
        let JsonValue::Object(mut entries) = artifacts else {
            unreachable!("artifacts is an object")
        };
        entries.remove("manifest");
        entries.remove("preflight");
        harness_contracts::sha256::digest_hex(
            JsonValue::Object(entries).to_canonical_string().as_bytes(),
        )
    }
}

pub(super) fn string_array(values: &[String]) -> JsonValue {
    JsonValue::Array(
        values
            .iter()
            .map(|value| JsonValue::String(value.clone()))
            .collect(),
    )
}

fn fill_json(fill: &EngineFill) -> JsonValue {
    JsonValue::object([
        ("event_id", JsonValue::String(fill.event_id.clone())),
        (
            "timestamp",
            JsonValue::String(crate::backtest::manifest::utc_timestamp(
                fill.timestamp_ns,
                false,
            )),
        ),
        ("sequence", JsonValue::UInt(fill.sequence)),
        ("fill_id", JsonValue::String(fill.fill_id.clone())),
        ("order_id", JsonValue::String(fill.order_id.clone())),
        ("symbol", JsonValue::String(fill.symbol.clone())),
        ("side", JsonValue::String(fill.side.clone())),
        ("quantity", JsonValue::String(fill.quantity.to_string())),
        ("price", JsonValue::String(fill.price.to_string())),
        ("fee", JsonValue::String(fill.fee.to_string())),
        (
            "liquidity",
            match fill.liquidity.as_str() {
                "" => JsonValue::Null,
                value => JsonValue::String(value.to_string()),
            },
        ),
    ])
}

fn fees_json(assessments: &[FeeAssessment], totals: &FeeAggregation) -> JsonValue {
    JsonValue::object([
        (
            "assessments",
            JsonValue::Array(
                assessments
                    .iter()
                    .map(|assessment| {
                        JsonValue::object([
                            ("fill_id", JsonValue::String(assessment.fill_id.clone())),
                            (
                                "schedule_id",
                                JsonValue::String(assessment.schedule_id.clone()),
                            ),
                            ("currency", JsonValue::String(assessment.currency.clone())),
                            ("job_id", JsonValue::String(assessment.job_id.clone())),
                            (
                                "strategy_id",
                                JsonValue::String(assessment.strategy_id.clone()),
                            ),
                            (
                                "asset_class",
                                JsonValue::String(assessment.asset_class.clone()),
                            ),
                            ("total", JsonValue::String(assessment.total().to_string())),
                            (
                                "components",
                                JsonValue::Array(
                                    assessment
                                        .components()
                                        .iter()
                                        .map(|component| {
                                            JsonValue::object([
                                                (
                                                    "fee_id",
                                                    JsonValue::String(component.fee_id.clone()),
                                                ),
                                                (
                                                    "amount",
                                                    JsonValue::String(component.amount.to_string()),
                                                ),
                                            ])
                                        })
                                        .collect(),
                                ),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "by_run",
            JsonValue::Array(totals.by_run.iter().map(aggregate_json).collect()),
        ),
        (
            "by_strategy",
            JsonValue::Array(totals.by_strategy.iter().map(aggregate_json).collect()),
        ),
        (
            "by_asset_class",
            JsonValue::Array(totals.by_asset_class.iter().map(aggregate_json).collect()),
        ),
    ])
}

fn aggregate_json(aggregate: &crate::backtest::fees::FeeAggregate) -> JsonValue {
    JsonValue::object([
        ("key", JsonValue::String(aggregate.key.clone())),
        ("currency", JsonValue::String(aggregate.currency.clone())),
        ("amount", JsonValue::String(aggregate.amount.to_string())),
    ])
}

fn risk_json(decision: &RiskDecision) -> JsonValue {
    JsonValue::object([
        ("action", JsonValue::String(decision.action.clone())),
        ("order_id", JsonValue::String(decision.order_id.clone())),
        (
            "strategy_id",
            JsonValue::String(decision.strategy_id.clone()),
        ),
        (
            "reason",
            match &decision.reason {
                Some(reason) => JsonValue::String(reason.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "halt_scope",
            match &decision.halt_scope {
                Some(scope) => JsonValue::String(scope.clone()),
                None => JsonValue::Null,
            },
        ),
        (
            "audits",
            JsonValue::Array(
                decision
                    .audits
                    .iter()
                    .map(|audit| {
                        JsonValue::object([
                            ("sequence", JsonValue::UInt(audit.sequence)),
                            ("pass_number", JsonValue::UInt(audit.pass_number)),
                            ("rule_id", JsonValue::String(audit.rule_id.clone())),
                            ("action", JsonValue::String(audit.action.clone())),
                            (
                                "reason",
                                match &audit.reason {
                                    Some(reason) => JsonValue::String(reason.clone()),
                                    None => JsonValue::Null,
                                },
                            ),
                            ("halt", JsonValue::Bool(audit.halt)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ])
}
