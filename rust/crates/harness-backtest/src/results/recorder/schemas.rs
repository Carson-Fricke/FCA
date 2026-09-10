//! Artifact names, field schemas, and recorder schema metadata.
//!
//! This is the Rust port of `harness.results.recorder.schemas`. Every recorded
//! artifact carries harness metadata naming the recorder schema
//! (`recorder.<artifact>`), the harness schema version, and the string-exact
//! decimal encoding. The `orders` and `fills` schemas extend the core order and
//! fill schemas with link columns, and `execution_reports` is the one artifact
//! whose fields mirror the verbatim core execution-report schema.
//!
//! The Python recorder writes Parquet through `pyarrow`. The staged Rust port
//! has no Arrow/native dependency yet, so artifacts are serialized through the
//! deterministic [`crate::results::recorder::writer`] canonical encoding; the
//! field order and metadata below are preserved exactly so an Arrow-backed
//! writer can be dropped in without touching callers.

/// Harness schema version, mirroring `harness.core.types.common.SCHEMA_VERSION`.
pub const SCHEMA_VERSION: &str = "1";

/// Value used for `harness.decimal.encoding` in every recorder schema.
pub const DECIMAL_ENCODING: &str = "string-exact";

/// The logical Arrow type of one recorded field.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogicalType {
    /// UTF-8 string.
    Utf8,
    /// Unsigned 32-bit integer.
    UInt32,
    /// Unsigned 64-bit integer.
    UInt64,
    /// Boolean.
    Bool,
    /// Nanosecond timestamp with UTC time zone.
    TimestampNsUtc,
}

impl LogicalType {
    /// A stable short name for diagnostics and schema fingerprints.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Utf8 => "utf8",
            Self::UInt32 => "uint32",
            Self::UInt64 => "uint64",
            Self::Bool => "bool",
            Self::TimestampNsUtc => "timestamp_ns_utc",
        }
    }
}

/// One field in a recorded artifact schema.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FieldDef {
    /// Field name.
    pub name: &'static str,
    /// Logical type.
    pub logical_type: LogicalType,
    /// Whether the field admits null values.
    pub nullable: bool,
}

const fn field(name: &'static str, logical_type: LogicalType, nullable: bool) -> FieldDef {
    FieldDef {
        name,
        logical_type,
        nullable,
    }
}

/// Named Parquet artifacts produced by one recorded run.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Artifact {
    /// Strategy intents that may produce an order.
    Actions,
    /// Normalized single- and multi-leg orders carrying `action_id`.
    Orders,
    /// One row per risk pipeline decision.
    RiskDecisions,
    /// One row per invoked risk rule.
    RiskAudits,
    /// Fills extended with their action, decision, position, and equity links.
    Fills,
    /// OMS reports written with the verbatim core report schema.
    ExecutionReports,
    /// Position state before and after each accounting transition.
    PositionTransitions,
    /// Book, portfolio, and account valuation snapshots.
    Equity,
    /// Structured strategy emissions.
    Emissions,
    /// Position marks and strategy state markers.
    Markers,
    /// Optional full event stream.
    Events,
    /// Run metadata, row counts, and per-artifact byte digests.
    Manifest,
}

impl Artifact {
    /// Every artifact in stable recorder order.
    pub const ALL: [Self; 12] = [
        Self::Actions,
        Self::Orders,
        Self::RiskDecisions,
        Self::RiskAudits,
        Self::Fills,
        Self::ExecutionReports,
        Self::PositionTransitions,
        Self::Equity,
        Self::Emissions,
        Self::Markers,
        Self::Events,
        Self::Manifest,
    ];

    /// The stable lowercase artifact name used in files and manifests.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Actions => "actions",
            Self::Orders => "orders",
            Self::RiskDecisions => "risk_decisions",
            Self::RiskAudits => "risk_audits",
            Self::Fills => "fills",
            Self::ExecutionReports => "execution_reports",
            Self::PositionTransitions => "position_transitions",
            Self::Equity => "equity",
            Self::Emissions => "emissions",
            Self::Markers => "markers",
            Self::Events => "events",
            Self::Manifest => "manifest",
        }
    }

    /// Parse an artifact from its lowercase name.
    #[must_use]
    pub fn parse(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.value() == name)
    }

    /// The schema name carried in `harness.schema` metadata.
    #[must_use]
    pub const fn schema_name(self) -> &'static str {
        match self {
            Self::Actions => "recorder.actions",
            Self::Orders => "recorder.orders",
            Self::RiskDecisions => "recorder.risk_decisions",
            Self::RiskAudits => "recorder.risk_audits",
            Self::Fills => "recorder.fills",
            Self::ExecutionReports => "execution_report",
            Self::PositionTransitions => "recorder.position_transitions",
            Self::Equity => "recorder.equity",
            Self::Emissions => "recorder.emissions",
            Self::Markers => "recorder.markers",
            Self::Events => "recorder.events",
            Self::Manifest => "recorder.manifest",
        }
    }

    /// The recorded Parquet file name for this artifact.
    #[must_use]
    pub const fn file_name(self) -> &'static str {
        match self {
            Self::Actions => "actions.parquet",
            Self::Orders => "orders.parquet",
            Self::RiskDecisions => "risk_decisions.parquet",
            Self::RiskAudits => "risk_audits.parquet",
            Self::Fills => "fills.parquet",
            Self::ExecutionReports => "execution_reports.parquet",
            Self::PositionTransitions => "position_transitions.parquet",
            Self::Equity => "equity.parquet",
            Self::Emissions => "emissions.parquet",
            Self::Markers => "markers.parquet",
            Self::Events => "events.parquet",
            Self::Manifest => "manifest.parquet",
        }
    }

    /// The ordered fields recorded for this artifact.
    #[must_use]
    pub const fn fields(self) -> &'static [FieldDef] {
        match self {
            Self::Actions => ACTIONS_FIELDS,
            Self::Orders => ORDERS_FIELDS,
            Self::RiskDecisions => RISK_DECISIONS_FIELDS,
            Self::RiskAudits => RISK_AUDITS_FIELDS,
            Self::Fills => FILLS_FIELDS,
            Self::ExecutionReports => EXECUTION_REPORTS_FIELDS,
            Self::PositionTransitions => POSITION_TRANSITIONS_FIELDS,
            Self::Equity => EQUITY_FIELDS,
            Self::Emissions => EMISSIONS_FIELDS,
            Self::Markers => MARKERS_FIELDS,
            Self::Events => EVENTS_FIELDS,
            Self::Manifest => MANIFEST_FIELDS,
        }
    }

    /// The three metadata keys every recorder schema carries.
    #[must_use]
    pub fn metadata(self) -> Vec<(&'static str, String)> {
        vec![
            ("harness.schema", self.schema_name().to_string()),
            ("harness.schema.version", SCHEMA_VERSION.to_string()),
            ("harness.decimal.encoding", DECIMAL_ENCODING.to_string()),
        ]
    }
}

/// Every stream artifact, in stable recorder order, excluding the manifest.
pub const STREAM_ARTIFACTS: [Artifact; 11] = [
    Artifact::Actions,
    Artifact::Orders,
    Artifact::RiskDecisions,
    Artifact::RiskAudits,
    Artifact::Fills,
    Artifact::ExecutionReports,
    Artifact::PositionTransitions,
    Artifact::Equity,
    Artifact::Emissions,
    Artifact::Markers,
    Artifact::Events,
];

const ACTIONS_FIELDS: &[FieldDef] = &[
    field("action_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("strategy_id", LogicalType::Utf8, false),
    field("book_id", LogicalType::Utf8, true),
    field("kind", LogicalType::Utf8, false),
    field("symbol", LogicalType::Utf8, true),
    field("side", LogicalType::Utf8, true),
    field("quantity", LogicalType::Utf8, true),
    field("order_id", LogicalType::Utf8, true),
    field("reason", LogicalType::Utf8, true),
    field("detail", LogicalType::Utf8, true),
];

const ORDERS_FIELDS: &[FieldDef] = &[
    field("order_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("event_id", LogicalType::Utf8, false),
    field("action_id", LogicalType::Utf8, true),
    field("strategy_id", LogicalType::Utf8, false),
    field("book_id", LogicalType::Utf8, true),
    field("kind", LogicalType::Utf8, false),
    field("symbol", LogicalType::Utf8, true),
    field("side", LogicalType::Utf8, true),
    field("quantity", LogicalType::Utf8, true),
    field("order_type", LogicalType::Utf8, false),
    field("time_in_force", LogicalType::Utf8, false),
    field("limit_price", LogicalType::Utf8, true),
    field("stop_price", LogicalType::Utf8, true),
    field("client_order_id", LogicalType::Utf8, true),
    field("legs", LogicalType::Utf8, false),
];

const RISK_DECISIONS_FIELDS: &[FieldDef] = &[
    field("decision_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("strategy_id", LogicalType::Utf8, false),
    field("action_id", LogicalType::Utf8, true),
    field("order_id", LogicalType::Utf8, false),
    field("action", LogicalType::Utf8, false),
    field("approved", LogicalType::Bool, false),
    field("halted", LogicalType::Bool, false),
    field("reason", LogicalType::Utf8, true),
    field("reasons", LogicalType::Utf8, false),
    field("audit_count", LogicalType::UInt32, false),
];

const RISK_AUDITS_FIELDS: &[FieldDef] = &[
    field("audit_id", LogicalType::Utf8, false),
    field("decision_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("rule_id", LogicalType::Utf8, false),
    field("pass_number", LogicalType::UInt32, false),
    field("audit_sequence", LogicalType::UInt64, false),
    field("action", LogicalType::Utf8, false),
    field("recheck", LogicalType::Bool, false),
    field("halt", LogicalType::Bool, false),
    field("order_id", LogicalType::Utf8, false),
    field("reason", LogicalType::Utf8, true),
    field("metrics", LogicalType::Utf8, false),
];

const FILLS_FIELDS: &[FieldDef] = &[
    field("fill_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("event_id", LogicalType::Utf8, false),
    field("order_id", LogicalType::Utf8, false),
    field("action_id", LogicalType::Utf8, true),
    field("risk_decision_id", LogicalType::Utf8, true),
    field("position_transition_id", LogicalType::Utf8, true),
    field("equity_snapshot_id", LogicalType::Utf8, true),
    field("symbol", LogicalType::Utf8, false),
    field("side", LogicalType::Utf8, false),
    field("quantity", LogicalType::Utf8, false),
    field("price", LogicalType::Utf8, false),
    field("fee", LogicalType::Utf8, false),
    field("liquidity", LogicalType::Utf8, true),
];

const EXECUTION_REPORTS_FIELDS: &[FieldDef] = &[
    field("event_id", LogicalType::Utf8, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("sequence", LogicalType::UInt64, false),
    field("report_id", LogicalType::Utf8, false),
    field("order_id", LogicalType::Utf8, false),
    field("status", LogicalType::Utf8, false),
    field("execution_type", LogicalType::Utf8, false),
    field("order_quantity", LogicalType::Utf8, false),
    field("filled_quantity", LogicalType::Utf8, false),
    field("remaining_quantity", LogicalType::Utf8, false),
    field("average_fill_price", LogicalType::Utf8, true),
    field("last_fill_quantity", LogicalType::Utf8, false),
    field("last_fill_price", LogicalType::Utf8, true),
    field("text", LogicalType::Utf8, true),
];

const POSITION_TRANSITIONS_FIELDS: &[FieldDef] = &[
    field("transition_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("account_id", LogicalType::Utf8, false),
    field("book_id", LogicalType::Utf8, true),
    field("symbol", LogicalType::Utf8, false),
    field("reason", LogicalType::Utf8, false),
    field("fill_id", LogicalType::Utf8, true),
    field("quantity_before", LogicalType::Utf8, false),
    field("quantity_after", LogicalType::Utf8, false),
    field("cost_basis", LogicalType::Utf8, false),
    field("cash_flow", LogicalType::Utf8, false),
    field("realized_pnl", LogicalType::Utf8, false),
    field("mark_price", LogicalType::Utf8, true),
    field("market_value", LogicalType::Utf8, true),
    field("unrealized_pnl", LogicalType::Utf8, true),
];

const EQUITY_FIELDS: &[FieldDef] = &[
    field("snapshot_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("account_id", LogicalType::Utf8, false),
    field("book_id", LogicalType::Utf8, true),
    field("source", LogicalType::Utf8, false),
    field("cash", LogicalType::Utf8, false),
    field("equity", LogicalType::Utf8, true),
    field("market_value", LogicalType::Utf8, true),
    field("realized_pnl", LogicalType::Utf8, false),
    field("unrealized_pnl", LogicalType::Utf8, true),
    field("total_pnl", LogicalType::Utf8, true),
    field("position_count", LogicalType::UInt32, false),
    field("fill_count", LogicalType::UInt64, false),
];

const EMISSIONS_FIELDS: &[FieldDef] = &[
    field("emission_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("strategy_id", LogicalType::Utf8, false),
    field("book_id", LogicalType::Utf8, true),
    field("name", LogicalType::Utf8, false),
    field("fields", LogicalType::Utf8, false),
];

const MARKERS_FIELDS: &[FieldDef] = &[
    field("marker_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("strategy_id", LogicalType::Utf8, false),
    field("scope", LogicalType::Utf8, false),
    field("key", LogicalType::Utf8, false),
    field("value", LogicalType::Utf8, true),
    field("detail", LogicalType::Utf8, true),
];

const EVENTS_FIELDS: &[FieldDef] = &[
    field("event_id", LogicalType::Utf8, false),
    field("sequence", LogicalType::UInt64, false),
    field("timestamp", LogicalType::TimestampNsUtc, false),
    field("event_type", LogicalType::Utf8, false),
    field("type_name", LogicalType::Utf8, false),
    field("symbol", LogicalType::Utf8, true),
    field("payload", LogicalType::Utf8, true),
];

const MANIFEST_FIELDS: &[FieldDef] = &[
    field("key", LogicalType::Utf8, false),
    field("value", LogicalType::Utf8, false),
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn artifact_names_round_trip() {
        for artifact in Artifact::ALL {
            assert_eq!(Artifact::parse(artifact.value()), Some(artifact));
        }
        assert_eq!(Artifact::parse("nope"), None);
    }

    #[test]
    fn stream_artifacts_exclude_the_manifest() {
        assert_eq!(STREAM_ARTIFACTS.len(), Artifact::ALL.len() - 1);
        assert!(!STREAM_ARTIFACTS.contains(&Artifact::Manifest));
    }

    #[test]
    fn every_field_name_is_unique_within_a_schema() {
        for artifact in Artifact::ALL {
            let fields = artifact.fields();
            let mut names: Vec<&str> = fields.iter().map(|field| field.name).collect();
            names.sort_unstable();
            names.dedup();
            assert_eq!(names.len(), fields.len(), "{artifact:?}");
        }
    }

    #[test]
    fn schema_metadata_matches_python() {
        let metadata = Artifact::Actions.metadata();
        assert_eq!(
            metadata[0],
            ("harness.schema", "recorder.actions".to_string())
        );
        assert_eq!(metadata[1], ("harness.schema.version", "1".to_string()));
        assert_eq!(
            metadata[2],
            ("harness.decimal.encoding", "string-exact".to_string())
        );
        assert_eq!(Artifact::ExecutionReports.schema_name(), "execution_report");
    }

    #[test]
    fn actions_field_order_matches_python() {
        let names: Vec<&str> = Artifact::Actions
            .fields()
            .iter()
            .map(|field| field.name)
            .collect();
        assert_eq!(
            names,
            vec![
                "action_id",
                "sequence",
                "timestamp",
                "strategy_id",
                "book_id",
                "kind",
                "symbol",
                "side",
                "quantity",
                "order_id",
                "reason",
                "detail",
            ]
        );
    }
}
