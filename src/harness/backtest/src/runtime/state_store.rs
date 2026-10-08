//! Durable runtime-state snapshots for the paper runtime.
//!
//! Port of the deterministic core of `harness.runtime.state_store`: the
//! canonical [`RuntimeSnapshot`] contract, its domain-separated SHA-256
//! identity, the verify-then-commit [`RuntimeStateStore`], and the
//! [`SnapshotSchedule`] cadence. The captured records carry the pinned,
//! cross-language JSON encoding (epic T181): decimal scalars are tagged as
//! `{"$decimal": text}` and floats as big-endian IEEE-754 bit patterns, so a
//! snapshot round-trips byte-identically across the FFI boundary.
//!
//! The store's `capture`/`restore_shell` methods in Python read a `PaperShell`;
//! the shell port belongs to this same task but its kernel is large, so capture
//! is expressed through the public record constructors and the shell seam in
//! [`super::paper`].

use std::collections::BTreeMap;

use num::Decimal18;

use crate::canonical_json::JsonValue;
use crate::runtime::paper::{
    PaperOptions, PaperRuntimeShell, PaperShell, PaperShellRestore, VenueWatermarks,
};
use crate::sha256::{digest_hex, Sha256};

/// Snapshot format version.
pub const FORMAT_VERSION: u64 = 1;

/// Domain prefix for the snapshot identity hash.
const SNAPSHOT_DOMAIN: &[u8] = b"fca:harness-runtime-state-snapshot:v1\0";

/// Deterministic capture points for one runtime snapshot.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum SnapshotTrigger {
    /// A periodic capture.
    Periodic,
    /// A session-end capture.
    SessionEnd,
    /// A stop capture.
    Stop,
}

impl SnapshotTrigger {
    /// The stable lowercase value.
    #[must_use]
    pub const fn value(self) -> &'static str {
        match self {
            Self::Periodic => "periodic",
            Self::SessionEnd => "session_end",
            Self::Stop => "stop",
        }
    }

    /// Parse a trigger from its lowercase value.
    #[must_use]
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "periodic" => Some(Self::Periodic),
            "session_end" => Some(Self::SessionEnd),
            "stop" => Some(Self::Stop),
            _ => None,
        }
    }
}

/// State-store failure.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StateStoreError {
    /// No valid snapshot is stored.
    Missing(String),
    /// A stored snapshot or pointer has an unsupported version.
    Version(String),
    /// Snapshot bytes fail their canonical hash checks.
    Integrity(String),
    /// A payload cannot be persisted verifiably.
    Write(String),
    /// A snapshot cannot be restored.
    Restore(String),
}

impl std::fmt::Display for StateStoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Missing(message)
            | Self::Version(message)
            | Self::Integrity(message)
            | Self::Write(message)
            | Self::Restore(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for StateStoreError {}

/// Whole-object storage boundary for snapshot payloads and pointers.
pub trait SnapshotObjectStore {
    /// Atomically replace the object at a repository-relative path.
    fn replace(&mut self, relative_path: &str, payload: &[u8]);

    /// Return the full payload of one object, or `None` when absent.
    fn read(&self, relative_path: &str) -> Option<Vec<u8>>;
}

/// An in-memory [`SnapshotObjectStore`] for deterministic tests.
#[derive(Clone, Debug, Default)]
pub struct InMemorySnapshotStore {
    objects: BTreeMap<String, Vec<u8>>,
}

impl InMemorySnapshotStore {
    /// Create an empty store.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of stored objects.
    #[must_use]
    pub fn len(&self) -> usize {
        self.objects.len()
    }

    /// Whether the store holds no objects.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.objects.is_empty()
    }
}

impl SnapshotObjectStore for InMemorySnapshotStore {
    fn replace(&mut self, relative_path: &str, payload: &[u8]) {
        self.objects
            .insert(relative_path.to_string(), payload.to_vec());
    }

    fn read(&self, relative_path: &str) -> Option<Vec<u8>> {
        self.objects.get(relative_path).cloned()
    }
}

/// Normalize a string-exact decimal the way `_decimal_text` does.
///
/// Zero renders as `0`; otherwise trailing fractional zeros are removed and
/// the sign and integer part are preserved.
#[must_use]
pub fn decimal_text(value: &str) -> String {
    let value = value.trim();
    let (sign, unsigned) = match value.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", value.strip_prefix('+').unwrap_or(value)),
    };
    let (integer, fraction) = match unsigned.split_once('.') {
        Some((integer, fraction)) => (integer, fraction),
        None => (unsigned, ""),
    };
    let integer = integer.trim_start_matches('0');
    let integer = if integer.is_empty() { "0" } else { integer };
    let fraction = fraction.trim_end_matches('0');
    let all_zero = integer == "0" && fraction.is_empty();
    if all_zero {
        return "0".to_string();
    }
    if fraction.is_empty() {
        format!("{sign}{integer}")
    } else {
        format!("{sign}{integer}.{fraction}")
    }
}

/// Build the tagged `{"$decimal": text}` value the canonical encoder expects.
#[must_use]
pub fn decimal_tag(value: &str) -> JsonValue {
    JsonValue::object([("$decimal", JsonValue::String(decimal_text(value)))])
}

/// Compare two string-exact decimals by numeric value.
fn decimal_eq(left: &str, right: &str) -> bool {
    let left = left.parse::<Decimal18>().unwrap_or(Decimal18::ZERO);
    let right = right.parse::<Decimal18>().unwrap_or(Decimal18::ZERO);
    left == right
}

/// Freeze the runtime options into the snapshot's canonical option map.
fn options_to_json(options: &PaperOptions) -> BTreeMap<String, JsonValue> {
    BTreeMap::from([
        (
            "job_id".to_string(),
            JsonValue::String(options.job_id.clone()),
        ),
        (
            "initial_cash".to_string(),
            decimal_tag(&options.initial_cash),
        ),
        (
            "fill_model".to_string(),
            JsonValue::string(options.fill_model.value()),
        ),
        (
            "fee_precision".to_string(),
            JsonValue::String(options.fee_precision.clone()),
        ),
        (
            "abort_policy".to_string(),
            JsonValue::string(options.abort_policy.value()),
        ),
        (
            "strategy_halt_policy".to_string(),
            JsonValue::string(options.strategy_halt_policy.value()),
        ),
        (
            "teardown_policy".to_string(),
            JsonValue::string(options.teardown_policy.value()),
        ),
        (
            "session_policy".to_string(),
            JsonValue::string(options.session_policy.value()),
        ),
    ])
}

fn as_object(value: &JsonValue) -> Result<&BTreeMap<String, JsonValue>, StateStoreError> {
    match value {
        JsonValue::Object(entries) => Ok(entries),
        _ => Err(StateStoreError::Integrity(
            "expected a JSON object".to_string(),
        )),
    }
}

fn as_array(value: &JsonValue) -> Result<&[JsonValue], StateStoreError> {
    match value {
        JsonValue::Array(items) => Ok(items),
        _ => Err(StateStoreError::Integrity(
            "expected a JSON array".to_string(),
        )),
    }
}

fn as_str(value: &JsonValue, name: &str) -> Result<String, StateStoreError> {
    match value {
        JsonValue::String(text) => Ok(text.clone()),
        _ => Err(StateStoreError::Integrity(format!(
            "{name} must be a string"
        ))),
    }
}

fn required<'a>(
    entries: &'a BTreeMap<String, JsonValue>,
    key: &str,
) -> Result<&'a JsonValue, StateStoreError> {
    entries
        .get(key)
        .ok_or_else(|| StateStoreError::Integrity(format!("missing {key}")))
}

fn as_u64(value: &JsonValue, name: &str) -> Result<u64, StateStoreError> {
    match value {
        JsonValue::UInt(number) => Ok(*number),
        JsonValue::Int(number) if *number >= 0 => Ok(*number as u64),
        _ => Err(StateStoreError::Integrity(format!(
            "{name} must be a non-negative integer"
        ))),
    }
}

fn as_i64(value: &JsonValue, name: &str) -> Result<i64, StateStoreError> {
    match value {
        JsonValue::Int(number) => Ok(*number),
        JsonValue::UInt(number) => i64::try_from(*number)
            .map_err(|_| StateStoreError::Integrity(format!("{name} is out of range"))),
        _ => Err(StateStoreError::Integrity(format!(
            "{name} must be an integer"
        ))),
    }
}

fn as_tagged_decimal(value: &JsonValue, name: &str) -> Result<String, StateStoreError> {
    match value {
        JsonValue::Object(entries) => match entries.get("$decimal") {
            Some(JsonValue::String(text)) => Ok(text.clone()),
            _ => Err(StateStoreError::Integrity(format!(
                "{name} must be a tagged decimal"
            ))),
        },
        JsonValue::String(text) => Ok(text.clone()),
        _ => Err(StateStoreError::Integrity(format!(
            "{name} must be a decimal"
        ))),
    }
}

fn object(entries: impl IntoIterator<Item = (&'static str, JsonValue)>) -> JsonValue {
    JsonValue::object(entries)
}

fn string_array(values: &[String]) -> JsonValue {
    JsonValue::Array(
        values
            .iter()
            .map(|value| JsonValue::String(value.clone()))
            .collect(),
    )
}

/// Exact serialized state of one fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillState {
    /// Originating event identifier.
    pub event_id: String,
    /// Fill time as an RFC3339 microsecond string.
    pub timestamp: String,
    /// Fill sequence.
    pub sequence: u64,
    /// Fill identifier.
    pub fill_id: String,
    /// Order identifier.
    pub order_id: String,
    /// Instrument symbol.
    pub symbol: String,
    /// Fill side.
    pub side: String,
    /// String-exact quantity.
    pub quantity: String,
    /// String-exact price.
    pub price: String,
    /// String-exact fee.
    pub fee: String,
    /// Liquidity flag, if any.
    pub liquidity: Option<String>,
}

impl FillState {
    fn to_json(&self) -> JsonValue {
        object([
            ("event_id", JsonValue::String(self.event_id.clone())),
            ("timestamp", JsonValue::String(self.timestamp.clone())),
            ("sequence", JsonValue::UInt(self.sequence)),
            ("fill_id", JsonValue::String(self.fill_id.clone())),
            ("order_id", JsonValue::String(self.order_id.clone())),
            ("symbol", JsonValue::String(self.symbol.clone())),
            ("side", JsonValue::String(self.side.clone())),
            ("quantity", JsonValue::String(self.quantity.clone())),
            ("price", JsonValue::String(self.price.clone())),
            ("fee", JsonValue::String(self.fee.clone())),
            (
                "liquidity",
                match &self.liquidity {
                    Some(value) => JsonValue::String(value.clone()),
                    None => JsonValue::Null,
                },
            ),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        Ok(Self {
            event_id: as_str(required(entries, "event_id")?, "event_id")?,
            timestamp: as_str(required(entries, "timestamp")?, "timestamp")?,
            sequence: as_u64(required(entries, "sequence")?, "sequence")?,
            fill_id: as_str(required(entries, "fill_id")?, "fill_id")?,
            order_id: as_str(required(entries, "order_id")?, "order_id")?,
            symbol: as_str(required(entries, "symbol")?, "symbol")?,
            side: as_str(required(entries, "side")?, "side")?,
            quantity: as_str(required(entries, "quantity")?, "quantity")?,
            price: as_str(required(entries, "price")?, "price")?,
            fee: as_str(required(entries, "fee")?, "fee")?,
            liquidity: match required(entries, "liquidity")? {
                JsonValue::Null => None,
                other => Some(as_str(other, "liquidity")?),
            },
        })
    }
}

/// One virtual book's exact restorable accounting state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookState {
    /// Book identifier.
    pub book_id: String,
    /// String-exact initial cash.
    pub initial_cash: String,
    /// String-exact current cash.
    pub cash: String,
    /// Booked fill identifiers in application order.
    pub fill_ids: Vec<String>,
    /// Booked fills in application order.
    pub fills: Vec<FillState>,
    /// Captured position markers, sorted by symbol.
    pub markers: Vec<(String, String)>,
}

impl BookState {
    fn to_json(&self) -> JsonValue {
        object([
            ("book_id", JsonValue::String(self.book_id.clone())),
            ("initial_cash", decimal_tag(&self.initial_cash)),
            ("cash", decimal_tag(&self.cash)),
            ("fill_ids", string_array(&self.fill_ids)),
            (
                "fills",
                JsonValue::Array(self.fills.iter().map(FillState::to_json).collect()),
            ),
            (
                "markers",
                JsonValue::Array(
                    self.markers
                        .iter()
                        .map(|(symbol, price)| {
                            object([
                                ("symbol", JsonValue::String(symbol.clone())),
                                ("price", decimal_tag(price)),
                            ])
                        })
                        .collect(),
                ),
            ),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let fill_ids = as_array(required(entries, "fill_ids")?)?
            .iter()
            .map(|item| as_str(item, "fill id"))
            .collect::<Result<Vec<_>, _>>()?;
        let fills = as_array(required(entries, "fills")?)?
            .iter()
            .map(FillState::from_json)
            .collect::<Result<Vec<_>, _>>()?;
        if fill_ids.len() != fills.len() {
            return Err(StateStoreError::Integrity(
                "book fill identifiers and fills must have equal length".to_string(),
            ));
        }
        for (fill_id, fill) in fill_ids.iter().zip(fills.iter()) {
            if fill_id != &fill.fill_id {
                return Err(StateStoreError::Integrity(
                    "book fill identifiers must match the captured fills in order".to_string(),
                ));
            }
        }
        let markers = as_array(required(entries, "markers")?)?
            .iter()
            .map(|item| {
                let entries = as_object(item)?;
                Ok((
                    as_str(required(entries, "symbol")?, "symbol")?,
                    as_tagged_decimal(required(entries, "price")?, "price")?,
                ))
            })
            .collect::<Result<Vec<_>, StateStoreError>>()?;
        Ok(Self {
            book_id: as_str(required(entries, "book_id")?, "book_id")?,
            initial_cash: as_tagged_decimal(required(entries, "initial_cash")?, "initial_cash")?,
            cash: as_tagged_decimal(required(entries, "cash")?, "cash")?,
            fill_ids,
            fills,
            markers,
        })
    }
}

/// Exact serialized state of one seeded random source.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RandomState {
    /// Instance identifier.
    pub instance_id: String,
    /// State version.
    pub version: u64,
    /// Internal MT state words, least significant first.
    pub internal: Vec<u64>,
    /// Gaussian cache as a big-endian IEEE-754 hex bit pattern, if present.
    pub gauss_next_bits: Option<String>,
}

impl RandomState {
    fn to_json(&self) -> JsonValue {
        let gauss = match &self.gauss_next_bits {
            Some(bits) => object([("$float_bits", JsonValue::String(bits.clone()))]),
            None => JsonValue::Null,
        };
        object([
            ("instance_id", JsonValue::String(self.instance_id.clone())),
            ("version", JsonValue::UInt(self.version)),
            (
                "internal",
                JsonValue::Array(
                    self.internal
                        .iter()
                        .map(|word| JsonValue::UInt(*word))
                        .collect(),
                ),
            ),
            ("gauss_next", gauss),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let internal = as_array(required(entries, "internal")?)?
            .iter()
            .map(|item| as_u64(item, "random internal state"))
            .collect::<Result<Vec<_>, _>>()?;
        let gauss_next_bits = match required(entries, "gauss_next")? {
            JsonValue::Null => None,
            JsonValue::Object(gauss) => match gauss.get("$float_bits") {
                Some(JsonValue::String(bits)) => Some(bits.clone()),
                _ => {
                    return Err(StateStoreError::Integrity(
                        "random gauss state must decode to a float".to_string(),
                    ))
                }
            },
            _ => {
                return Err(StateStoreError::Integrity(
                    "random gauss state must decode to a float".to_string(),
                ))
            }
        };
        Ok(Self {
            instance_id: as_str(required(entries, "instance_id")?, "instance_id")?,
            version: as_u64(required(entries, "version")?, "random version")?,
            internal,
            gauss_next_bits,
        })
    }
}

/// One strategy's frozen state values at the capture point.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyState {
    /// Instance identifier.
    pub instance_id: String,
    /// Tagged canonical values.
    pub values: BTreeMap<String, JsonValue>,
}

impl StrategyState {
    fn to_json(&self) -> JsonValue {
        object([
            ("instance_id", JsonValue::String(self.instance_id.clone())),
            ("values", JsonValue::Object(self.values.clone())),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let values = as_object(required(entries, "values")?)?;
        Ok(Self {
            instance_id: as_str(required(entries, "instance_id")?, "instance_id")?,
            values: values.clone(),
        })
    }
}

/// Risk-gate identity and per-strategy gate inputs at the capture point.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RiskState {
    /// Rejection policy.
    pub on_reject: String,
    /// Configured rule order.
    pub rule_ids: Vec<String>,
    /// Per-strategy daily loss, tagged as decimals.
    pub daily_losses: BTreeMap<String, String>,
}

impl RiskState {
    fn to_json(&self) -> JsonValue {
        object([
            ("on_reject", JsonValue::String(self.on_reject.clone())),
            ("rule_ids", string_array(&self.rule_ids)),
            (
                "daily_losses",
                JsonValue::Object(
                    self.daily_losses
                        .iter()
                        .map(|(key, value)| (key.clone(), decimal_tag(value)))
                        .collect(),
                ),
            ),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let rule_ids = as_array(required(entries, "rule_ids")?)?
            .iter()
            .map(|item| as_str(item, "rule_id"))
            .collect::<Result<Vec<_>, _>>()?;
        let daily_losses = as_object(required(entries, "daily_losses")?)?
            .iter()
            .map(|(key, value)| Ok((key.clone(), as_tagged_decimal(value, "daily loss")?)))
            .collect::<Result<BTreeMap<_, _>, StateStoreError>>()?;
        Ok(Self {
            on_reject: as_str(required(entries, "on_reject")?, "on_reject")?,
            rule_ids,
            daily_losses,
        })
    }
}

/// The simulated venue's id and event-sequence counters at the capture point.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VenueState {
    /// Venue identifier ordinal.
    pub id_ordinal: u64,
    /// Arena event sequence; `-1` precedes the first event.
    pub event_sequence: i64,
}

impl VenueState {
    fn to_json(self) -> JsonValue {
        object([
            ("id_ordinal", JsonValue::UInt(self.id_ordinal)),
            ("event_sequence", JsonValue::Int(self.event_sequence)),
        ])
    }

    fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        Ok(Self {
            id_ordinal: as_u64(required(entries, "id_ordinal")?, "venue id ordinal")?,
            event_sequence: as_i64(required(entries, "event_sequence")?, "venue event sequence")?,
        })
    }
}

/// Frozen, canonical, hash-verifiable full runtime state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeSnapshot {
    /// Snapshot format version.
    pub format_version: u64,
    /// Run identifier.
    pub job_id: String,
    /// Capture trigger.
    pub trigger: SnapshotTrigger,
    /// Caller capture sequence.
    pub sequence: u64,
    /// Events dispatched at capture.
    pub dispatched_count: u64,
    /// Market events consumed at capture.
    pub market_event_count: u64,
    /// Tagged runtime options.
    pub options: BTreeMap<String, JsonValue>,
    /// Captured books.
    pub books: Vec<BookState>,
    /// Captured marks, tagged as decimals.
    pub prices: BTreeMap<String, String>,
    /// Per-strategy order counts.
    pub order_counts: BTreeMap<String, u64>,
    /// Order-to-book attribution.
    pub order_owners: BTreeMap<String, String>,
    /// Strategy state, sorted by instance id in the canonical form.
    pub strategy_state: Vec<StrategyState>,
    /// Random states, sorted by instance id in the canonical form.
    pub random_states: Vec<RandomState>,
    /// Risk-gate identity and inputs.
    pub risk: RiskState,
    /// Optional venue counters.
    pub venue: Option<VenueState>,
}

impl RuntimeSnapshot {
    /// Return the canonical JSON-ready snapshot content.
    #[must_use]
    pub fn to_json(&self) -> JsonValue {
        let mut content = object([
            ("format_version", JsonValue::UInt(self.format_version)),
            ("job_id", JsonValue::String(self.job_id.clone())),
            ("trigger", JsonValue::string(self.trigger.value())),
            ("sequence", JsonValue::UInt(self.sequence)),
            ("dispatched_count", JsonValue::UInt(self.dispatched_count)),
            (
                "market_event_count",
                JsonValue::UInt(self.market_event_count),
            ),
            ("options", JsonValue::Object(self.options.clone())),
            (
                "books",
                JsonValue::Array(self.books.iter().map(BookState::to_json).collect()),
            ),
            (
                "prices",
                JsonValue::Object(
                    self.prices
                        .iter()
                        .map(|(key, value)| (key.clone(), decimal_tag(value)))
                        .collect(),
                ),
            ),
            (
                "order_counts",
                JsonValue::Object(
                    self.order_counts
                        .iter()
                        .map(|(key, value)| (key.clone(), JsonValue::UInt(*value)))
                        .collect(),
                ),
            ),
            (
                "order_owners",
                JsonValue::Object(
                    self.order_owners
                        .iter()
                        .map(|(key, value)| (key.clone(), JsonValue::String(value.clone())))
                        .collect(),
                ),
            ),
            (
                "strategy_state",
                JsonValue::Array(
                    self.strategy_state
                        .iter()
                        .map(StrategyState::to_json)
                        .collect(),
                ),
            ),
            (
                "random_states",
                JsonValue::Array(
                    self.random_states
                        .iter()
                        .map(RandomState::to_json)
                        .collect(),
                ),
            ),
            ("risk", self.risk.to_json()),
        ]);
        let JsonValue::Object(ref mut entries) = content else {
            unreachable!("snapshot content is an object")
        };
        if let Some(venue) = self.venue {
            entries.insert("venue".to_string(), venue.to_json());
        }
        content
    }

    /// Rebuild one snapshot from parsed canonical JSON.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Integrity`] when the document is malformed.
    pub fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let trigger = as_str(required(entries, "trigger")?, "trigger")?;
        let trigger = SnapshotTrigger::parse(&trigger).ok_or_else(|| {
            StateStoreError::Integrity(format!("unknown snapshot trigger {trigger:?}"))
        })?;
        let prices = as_object(required(entries, "prices")?)?
            .iter()
            .map(|(key, value)| Ok((key.clone(), as_tagged_decimal(value, "price")?)))
            .collect::<Result<BTreeMap<_, _>, StateStoreError>>()?;
        let order_counts = as_object(required(entries, "order_counts")?)?
            .iter()
            .map(|(key, value)| Ok((key.clone(), as_u64(value, "order count")?)))
            .collect::<Result<BTreeMap<_, _>, StateStoreError>>()?;
        let order_owners = as_object(required(entries, "order_owners")?)?
            .iter()
            .map(|(key, value)| Ok((key.clone(), as_str(value, "order owner")?)))
            .collect::<Result<BTreeMap<_, _>, StateStoreError>>()?;
        let venue = match entries.get("venue") {
            None | Some(JsonValue::Null) => None,
            Some(value) => Some(VenueState::from_json(value)?),
        };
        let mut snapshot = Self {
            format_version: as_u64(required(entries, "format_version")?, "format_version")?,
            job_id: as_str(required(entries, "job_id")?, "job_id")?,
            trigger,
            sequence: as_u64(required(entries, "sequence")?, "sequence")?,
            dispatched_count: as_u64(required(entries, "dispatched_count")?, "dispatched_count")?,
            market_event_count: as_u64(
                required(entries, "market_event_count")?,
                "market_event_count",
            )?,
            options: as_object(required(entries, "options")?)?.clone(),
            books: as_array(required(entries, "books")?)?
                .iter()
                .map(BookState::from_json)
                .collect::<Result<Vec<_>, _>>()?,
            prices,
            order_counts,
            order_owners,
            strategy_state: as_array(required(entries, "strategy_state")?)?
                .iter()
                .map(StrategyState::from_json)
                .collect::<Result<Vec<_>, _>>()?,
            random_states: as_array(required(entries, "random_states")?)?
                .iter()
                .map(RandomState::from_json)
                .collect::<Result<Vec<_>, _>>()?,
            risk: RiskState::from_json(required(entries, "risk")?)?,
            venue,
        };
        snapshot.normalize();
        Ok(snapshot)
    }

    fn normalize(&mut self) {
        self.strategy_state
            .sort_by(|left, right| left.instance_id.cmp(&right.instance_id));
        self.random_states
            .sort_by(|left, right| left.instance_id.cmp(&right.instance_id));
        for book in &mut self.books {
            book.markers.sort_by(|left, right| left.0.cmp(&right.0));
        }
    }

    /// Return the canonical UTF-8 bytes the snapshot identity hashes.
    #[must_use]
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut bytes = SNAPSHOT_DOMAIN.to_vec();
        bytes.extend_from_slice(self.to_json().to_canonical_string_utf8().as_bytes());
        bytes
    }

    /// Return the domain-separated SHA-256 identity of this snapshot.
    #[must_use]
    pub fn snapshot_id(&self) -> String {
        digest_hex(&self.canonical_bytes())
    }

    /// Return the canonical stored envelope for this snapshot.
    #[must_use]
    pub fn payload_bytes(&self) -> Vec<u8> {
        object([
            ("snapshot_id", JsonValue::String(self.snapshot_id())),
            ("snapshot", self.to_json()),
        ])
        .to_canonical_string_utf8()
        .into_bytes()
    }
}

/// Version-checked pointer to one persisted snapshot payload.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredSnapshot {
    /// Snapshot format version.
    pub format_version: u64,
    /// Run identifier.
    pub job_id: String,
    /// Snapshot identity.
    pub snapshot_id: String,
    /// SHA-256 of the payload bytes.
    pub payload_sha256: String,
    /// Payload byte length.
    pub byte_length: u64,
}

impl StoredSnapshot {
    /// Return the canonical JSON-ready pointer record.
    #[must_use]
    pub fn to_json(&self) -> JsonValue {
        object([
            ("format_version", JsonValue::UInt(self.format_version)),
            ("job_id", JsonValue::String(self.job_id.clone())),
            ("snapshot_id", JsonValue::String(self.snapshot_id.clone())),
            (
                "payload_sha256",
                JsonValue::String(self.payload_sha256.clone()),
            ),
            ("byte_length", JsonValue::UInt(self.byte_length)),
        ])
    }

    /// Rebuild a pointer from parsed canonical JSON.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError`] when the pointer is malformed or versioned
    /// outside this build.
    pub fn from_json(value: &JsonValue) -> Result<Self, StateStoreError> {
        let entries = as_object(value)?;
        let format_version = as_u64(required(entries, "format_version")?, "format_version")?;
        if format_version != FORMAT_VERSION {
            return Err(StateStoreError::Version(format!(
                "snapshot format version {format_version} is not supported; this build reads \
                 version {FORMAT_VERSION}"
            )));
        }
        let pointer = Self {
            format_version,
            job_id: as_str(required(entries, "job_id")?, "job_id")?,
            snapshot_id: as_str(required(entries, "snapshot_id")?, "snapshot_id")?,
            payload_sha256: as_str(required(entries, "payload_sha256")?, "payload_sha256")?,
            byte_length: as_u64(required(entries, "byte_length")?, "byte_length")?,
        };
        if pointer.byte_length == 0 {
            return Err(StateStoreError::Integrity(
                "byte_length must be positive".to_string(),
            ));
        }
        Ok(pointer)
    }
}

/// Deterministic periodic capture cadence over admitted market events.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SnapshotSchedule {
    /// Events between periodic captures.
    pub every_events: u64,
}

impl SnapshotSchedule {
    /// Create a schedule.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError`] when `every_events` is zero.
    pub fn new(every_events: u64) -> Result<Self, StateStoreError> {
        if every_events == 0 {
            return Err(StateStoreError::Integrity(
                "every_events must be positive".to_string(),
            ));
        }
        Ok(Self { every_events })
    }

    /// Whether a periodic snapshot is due after this many events.
    #[must_use]
    pub fn due(&self, market_event_count: u64) -> bool {
        market_event_count > 0 && market_event_count.is_multiple_of(self.every_events)
    }
}

fn payload_key(job_id: &str, snapshot_id: &str) -> String {
    format!("runs/{job_id}/snapshots/{snapshot_id}.json")
}

fn pointer_key(job_id: &str) -> String {
    format!("runs/{job_id}/latest.json")
}

/// Persist, verify, and restore canonical runtime state snapshots.
pub struct RuntimeStateStore {
    job_id: String,
    stores: Vec<Box<dyn SnapshotObjectStore>>,
}

impl RuntimeStateStore {
    /// Create a store bound to one run identifier.
    #[must_use]
    pub fn new(job_id: impl Into<String>) -> Self {
        Self {
            job_id: job_id.into(),
            stores: Vec::new(),
        }
    }

    /// Register a primary or mirror store, in priority order.
    pub fn add_store(&mut self, store: Box<dyn SnapshotObjectStore>) {
        self.stores.push(store);
    }

    /// The deployment run identifier.
    #[must_use]
    pub fn job_id(&self) -> &str {
        &self.job_id
    }

    /// Persist one snapshot atomically and return its pointer record.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Write`] when a store fails to persist the
    /// payload or pointer verbatim.
    pub fn save(&mut self, snapshot: &RuntimeSnapshot) -> Result<StoredSnapshot, StateStoreError> {
        if snapshot.job_id != self.job_id {
            return Err(StateStoreError::Integrity(format!(
                "snapshot run {:?} does not match store run {:?}",
                snapshot.job_id, self.job_id
            )));
        }
        let payload = snapshot.payload_bytes();
        let pointer = StoredSnapshot {
            format_version: FORMAT_VERSION,
            job_id: snapshot.job_id.clone(),
            snapshot_id: snapshot.snapshot_id(),
            payload_sha256: digest_hex(&payload),
            byte_length: payload.len() as u64,
        };
        let payload_key = payload_key(&snapshot.job_id, &snapshot.snapshot_id());
        let pointer_bytes = pointer.to_json().to_canonical_string_utf8().into_bytes();
        let pointer_key = pointer_key(&snapshot.job_id);
        for store in &mut self.stores {
            store.replace(&payload_key, &payload);
            if store.read(&payload_key).as_deref() != Some(payload.as_slice()) {
                return Err(StateStoreError::Write(format!(
                    "snapshot payload at {payload_key:?} did not persist verbatim"
                )));
            }
        }
        for store in &mut self.stores {
            store.replace(&pointer_key, &pointer_bytes);
            if store.read(&pointer_key).as_deref() != Some(pointer_bytes.as_slice()) {
                return Err(StateStoreError::Write(format!(
                    "snapshot pointer at {pointer_key:?} did not persist verbatim"
                )));
            }
        }
        Ok(pointer)
    }

    /// Return the latest valid snapshot, falling back to mirrors.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError`] when no store holds a valid snapshot.
    pub fn load(&self) -> Result<RuntimeSnapshot, StateStoreError> {
        let mut failures: Vec<StateStoreError> = Vec::new();
        for store in &self.stores {
            match self.load_from(store.as_ref()) {
                Ok(snapshot) => return Ok(snapshot),
                Err(error) => failures.push(error),
            }
        }
        if failures.is_empty() {
            return Err(StateStoreError::Missing(format!(
                "no snapshot is stored for run {:?}",
                self.job_id
            )));
        }
        for priority in [0usize, 1, 2] {
            for failure in &failures {
                let rank = match failure {
                    StateStoreError::Integrity(_) => 0,
                    StateStoreError::Version(_) => 1,
                    StateStoreError::Missing(_) => 2,
                    _ => 3,
                };
                if rank == priority {
                    return Err(failure.clone());
                }
            }
        }
        Err(failures.remove(0))
    }

    /// Return the pointer record of the latest stored snapshot.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError`] when no store holds a valid pointer.
    pub fn latest(&self) -> Result<StoredSnapshot, StateStoreError> {
        let key = pointer_key(&self.job_id);
        let mut errors: Vec<StateStoreError> = Vec::new();
        for store in &self.stores {
            let Some(raw) = store.read(&key) else {
                errors.push(StateStoreError::Missing(format!(
                    "object {key:?} is not available in the snapshot store"
                )));
                continue;
            };
            let text = String::from_utf8(raw).map_err(|error| {
                StateStoreError::Integrity(format!("object {key:?} is not valid UTF-8: {error}"))
            })?;
            let value = parse_json(&text, &key)?;
            return StoredSnapshot::from_json(&value);
        }
        if errors.is_empty() {
            return Err(StateStoreError::Missing(format!(
                "no snapshot pointer is stored for run {:?}",
                self.job_id
            )));
        }
        Err(errors.remove(0))
    }

    /// Capture the full restorable state of one paper shell into a snapshot.
    ///
    /// Everything is read through the shell's public supervision surface, and
    /// no wall-clock value enters the snapshot. When `order_owners` is `None`
    /// the attribution is derived from the captured books.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Integrity`] when the shell or options belong
    /// to another run, when `strategy_ids` is empty, or when a book references
    /// a fill the session history does not contain.
    #[allow(clippy::too_many_arguments)]
    pub fn capture(
        &self,
        shell: &dyn PaperShell,
        options: &PaperOptions,
        strategy_ids: &[String],
        trigger: SnapshotTrigger,
        sequence: u64,
        order_owners: Option<&BTreeMap<String, String>>,
        random_states: &[RandomState],
    ) -> Result<RuntimeSnapshot, StateStoreError> {
        if shell.job_id() != self.job_id {
            return Err(StateStoreError::Integrity(format!(
                "shell run {:?} does not match store run {:?}",
                shell.job_id(),
                self.job_id
            )));
        }
        if options.job_id != self.job_id {
            return Err(StateStoreError::Integrity(format!(
                "options run {:?} does not match store run {:?}",
                options.job_id, self.job_id
            )));
        }
        let mut ids: Vec<String> = Vec::new();
        for instance_id in strategy_ids {
            if !ids.contains(instance_id) {
                ids.push(instance_id.clone());
            }
        }
        if ids.is_empty() {
            return Err(StateStoreError::Integrity(
                "strategy_ids must not be empty".to_string(),
            ));
        }
        let result = shell.result();
        let fills_by_id: BTreeMap<&str, &FillState> = result
            .fills
            .iter()
            .map(|fill| (fill.fill_id.as_str(), fill))
            .collect();
        let mut books: Vec<BookState> = Vec::new();
        for book in shell.books() {
            for fill_id in book.fill_ids() {
                if !fills_by_id.contains_key(fill_id.as_str()) {
                    return Err(StateStoreError::Integrity(format!(
                        "book {:?} references fill {fill_id:?} that the session fill \
                         history does not contain",
                        book.book_id
                    )));
                }
            }
            books.push(BookState {
                book_id: book.book_id.clone(),
                initial_cash: book.initial_cash.clone(),
                cash: book.cash.clone(),
                fill_ids: book.fill_ids(),
                fills: book.fills.clone(),
                markers: book.markers.clone(),
            });
        }
        let mut order_counts: BTreeMap<String, u64> = BTreeMap::new();
        let mut daily_losses: BTreeMap<String, String> = BTreeMap::new();
        for instance_id in &ids {
            order_counts.insert(instance_id.clone(), shell.order_count(instance_id));
            daily_losses.insert(instance_id.clone(), shell.daily_loss(instance_id));
        }
        let risk = RiskState {
            on_reject: shell.on_reject().to_string(),
            rule_ids: shell.rule_ids().to_vec(),
            daily_losses,
        };
        let order_owners = match order_owners {
            Some(owners) => owners.clone(),
            None => {
                let mut owners: BTreeMap<String, String> = BTreeMap::new();
                for book_state in &books {
                    for fill in &book_state.fills {
                        owners
                            .entry(fill.order_id.clone())
                            .or_insert_with(|| book_state.book_id.clone());
                    }
                }
                owners
            }
        };
        let strategy_state: Vec<StrategyState> = shell
            .strategy_state_export()
            .into_iter()
            .map(|(instance_id, values)| StrategyState {
                instance_id,
                values: match values {
                    JsonValue::Object(entries) => entries,
                    _ => BTreeMap::new(),
                },
            })
            .collect();
        let watermarks = shell.venue_watermarks();
        Ok(RuntimeSnapshot {
            format_version: FORMAT_VERSION,
            job_id: self.job_id.clone(),
            trigger,
            sequence,
            dispatched_count: result.dispatched_count,
            market_event_count: result.market_event_count,
            options: options_to_json(options),
            books,
            prices: shell.prices().clone(),
            order_counts,
            order_owners,
            strategy_state,
            random_states: random_states.to_vec(),
            risk,
            venue: Some(VenueState {
                id_ordinal: watermarks.id_ordinal,
                event_sequence: watermarks.event_sequence,
            }),
        })
    }

    /// Rebuild a fresh paper shell whose supervision state matches a snapshot.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Restore`] when the snapshot belongs to
    /// another run or its books disagree with the options.
    pub fn restore_shell(
        &self,
        snapshot: &RuntimeSnapshot,
        options: PaperOptions,
    ) -> Result<PaperRuntimeShell, StateStoreError> {
        self.validate_snapshot_run(snapshot)?;
        if options.job_id != self.job_id {
            return Err(StateStoreError::Restore(format!(
                "options run {:?} does not match store run {:?}",
                options.job_id, self.job_id
            )));
        }
        let books: Vec<(String, String)> = snapshot
            .books
            .iter()
            .map(|book| (book.book_id.clone(), book.initial_cash.clone()))
            .collect();
        let mut shell = PaperRuntimeShell::new(options, books);
        self.restore_into_shell(snapshot, &mut shell)?;
        Ok(shell)
    }

    /// Replay captured state into one existing shell's public surface.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Restore`] when a book is missing, its initial
    /// cash disagrees, or replaying its fills does not reproduce the captured
    /// cash exactly.
    pub fn restore_into_shell(
        &self,
        snapshot: &RuntimeSnapshot,
        shell: &mut dyn PaperShellRestore,
    ) -> Result<(), StateStoreError> {
        self.validate_snapshot_run(snapshot)?;
        let mut marks: BTreeMap<String, String> = BTreeMap::new();
        for book_state in &snapshot.books {
            let Some(book) = shell.shell_book_mut(&book_state.book_id) else {
                return Err(StateStoreError::Restore(format!(
                    "shell has no book {:?}",
                    book_state.book_id
                )));
            };
            if !decimal_eq(&book.initial_cash, &book_state.initial_cash) {
                return Err(StateStoreError::Restore(format!(
                    "book {:?} initial cash {:?} does not match the captured {:?}",
                    book_state.book_id, book.initial_cash, book_state.initial_cash
                )));
            }
            for fill in &book_state.fills {
                book.apply_fill(fill).map_err(StateStoreError::Restore)?;
            }
            if !decimal_eq(&book.cash, &book_state.cash) {
                return Err(StateStoreError::Restore(format!(
                    "replayed book {:?} cash {:?} does not match the captured {:?}",
                    book_state.book_id, book.cash, book_state.cash
                )));
            }
            for (symbol, price) in &book_state.markers {
                match marks.get(symbol) {
                    Some(previous) if previous != price => {
                        return Err(StateStoreError::Restore(format!(
                            "captured books disagree on the mark for {symbol:?}"
                        )));
                    }
                    Some(_) => {}
                    None => {
                        marks.insert(symbol.clone(), price.clone());
                    }
                }
            }
        }
        let mut session_fills: Vec<FillState> = Vec::new();
        for book_state in &snapshot.books {
            session_fills.extend(book_state.fills.iter().cloned());
        }
        session_fills.sort_by_key(|fill| fill.sequence);
        if !session_fills.is_empty() {
            shell.restore_fills(&session_fills);
        }
        shell.restore_session_counts(snapshot.dispatched_count, snapshot.market_event_count);
        shell.restore_risk(&snapshot.risk.on_reject, &snapshot.risk.rule_ids);
        shell.restore_daily_losses(&snapshot.risk.daily_losses);
        shell.restore_prices(marks);
        for (instance_id, count) in &snapshot.order_counts {
            for _ in 0..*count {
                shell.count_order(instance_id);
            }
        }
        for (order_id, instance_id) in &snapshot.order_owners {
            shell.own_order(order_id, instance_id);
        }
        let state: BTreeMap<String, JsonValue> = snapshot
            .strategy_state
            .iter()
            .map(|record| {
                (
                    record.instance_id.clone(),
                    JsonValue::Object(record.values.clone()),
                )
            })
            .collect();
        shell.restore_strategy_state(state);
        let mut id_ordinal = 0u64;
        let mut event_sequence = -1i64;
        if let Some(venue) = snapshot.venue {
            id_ordinal = venue.id_ordinal;
            event_sequence = venue.event_sequence;
        }
        for book_state in &snapshot.books {
            for fill in &book_state.fills {
                event_sequence = event_sequence.max(fill.sequence as i64);
            }
        }
        shell.restore_venue_watermarks(VenueWatermarks {
            id_ordinal,
            event_sequence,
        });
        Ok(())
    }

    /// Rebuild each captured random source record.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Restore`] when the snapshot belongs to
    /// another run.
    pub fn restore_randoms(
        &self,
        snapshot: &RuntimeSnapshot,
    ) -> Result<Vec<RandomState>, StateStoreError> {
        self.validate_snapshot_run(snapshot)?;
        Ok(snapshot.random_states.clone())
    }

    /// Rebuild each strategy's frozen state values from a snapshot.
    ///
    /// # Errors
    ///
    /// Returns [`StateStoreError::Restore`] when the snapshot belongs to
    /// another run.
    pub fn restore_strategy_state(
        &self,
        snapshot: &RuntimeSnapshot,
    ) -> Result<BTreeMap<String, BTreeMap<String, JsonValue>>, StateStoreError> {
        self.validate_snapshot_run(snapshot)?;
        Ok(snapshot
            .strategy_state
            .iter()
            .map(|record| (record.instance_id.clone(), record.values.clone()))
            .collect())
    }

    fn validate_snapshot_run(&self, snapshot: &RuntimeSnapshot) -> Result<(), StateStoreError> {
        if snapshot.job_id != self.job_id {
            return Err(StateStoreError::Restore(format!(
                "snapshot run {:?} does not match store run {:?}",
                snapshot.job_id, self.job_id
            )));
        }
        Ok(())
    }

    fn load_from(
        &self,
        store: &dyn SnapshotObjectStore,
    ) -> Result<RuntimeSnapshot, StateStoreError> {
        let pointer_key = pointer_key(&self.job_id);
        let raw = store.read(&pointer_key).ok_or_else(|| {
            StateStoreError::Missing(format!(
                "object {pointer_key:?} is not available in the snapshot store"
            ))
        })?;
        let text = String::from_utf8(raw).map_err(|error| {
            StateStoreError::Integrity(format!(
                "object {pointer_key:?} is not valid UTF-8: {error}"
            ))
        })?;
        let pointer = StoredSnapshot::from_json(&parse_json(&text, &pointer_key)?)?;
        if pointer.job_id != self.job_id {
            return Err(StateStoreError::Integrity(format!(
                "snapshot pointer at {pointer_key:?} references run {:?}",
                pointer.job_id
            )));
        }
        let payload_key = payload_key(&self.job_id, &pointer.snapshot_id);
        let payload = store.read(&payload_key).ok_or_else(|| {
            StateStoreError::Missing(format!(
                "object {payload_key:?} is not available in the snapshot store"
            ))
        })?;
        if payload.len() as u64 != pointer.byte_length {
            return Err(StateStoreError::Integrity(format!(
                "snapshot payload at {payload_key:?} has an unexpected length"
            )));
        }
        if digest_hex(&payload) != pointer.payload_sha256 {
            return Err(StateStoreError::Integrity(format!(
                "snapshot payload at {payload_key:?} fails its SHA-256 digest"
            )));
        }
        let envelope_text = String::from_utf8(payload).map_err(|error| {
            StateStoreError::Integrity(format!(
                "snapshot payload at {payload_key:?} is not valid UTF-8: {error}"
            ))
        })?;
        let envelope = parse_json(&envelope_text, &payload_key)?;
        let entries = as_object(&envelope)?;
        let declared = as_str(required(entries, "snapshot_id")?, "snapshot_id")?;
        let snapshot = RuntimeSnapshot::from_json(required(entries, "snapshot")?)?;
        if snapshot.snapshot_id() != declared {
            return Err(StateStoreError::Integrity(format!(
                "snapshot content hashes to {:?} but the envelope declares {declared:?}",
                snapshot.snapshot_id()
            )));
        }
        if snapshot.job_id != self.job_id {
            return Err(StateStoreError::Integrity(format!(
                "snapshot run {:?} does not match store run {:?}",
                snapshot.job_id, self.job_id
            )));
        }
        Ok(snapshot)
    }
}

/// Parse a tiny canonical JSON document into [`JsonValue`].
///
/// The state store only ever reads back JSON that its own canonical encoder
/// wrote, so this minimal parser covers objects, arrays, strings, integers,
/// booleans, and null.
///
/// # Errors
///
/// Returns [`StateStoreError::Integrity`] on malformed input.
pub fn parse_json(text: &str, name: &str) -> Result<JsonValue, StateStoreError> {
    let mut parser = JsonParser {
        bytes: text.as_bytes(),
        position: 0,
    };
    let value = parser.value().map_err(|message| {
        StateStoreError::Integrity(format!("object {name:?} is not valid JSON: {message}"))
    })?;
    parser.skip_whitespace();
    if parser.position != parser.bytes.len() {
        return Err(StateStoreError::Integrity(format!(
            "object {name:?} has trailing JSON content"
        )));
    }
    Ok(value)
}

struct JsonParser<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl JsonParser<'_> {
    fn value(&mut self) -> Result<JsonValue, String> {
        self.skip_whitespace();
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(JsonValue::String(self.string()?)),
            Some(b't') => self.literal("true", JsonValue::Bool(true)),
            Some(b'f') => self.literal("false", JsonValue::Bool(false)),
            Some(b'n') => self.literal("null", JsonValue::Null),
            Some(b'-') | Some(b'0'..=b'9') => self.number(),
            other => Err(format!("unexpected byte {other:?}")),
        }
    }

    fn object(&mut self) -> Result<JsonValue, String> {
        self.expect(b'{')?;
        let mut entries = BTreeMap::new();
        self.skip_whitespace();
        if self.peek() == Some(b'}') {
            self.position += 1;
            return Ok(JsonValue::Object(entries));
        }
        loop {
            self.skip_whitespace();
            let key = self.string()?;
            self.skip_whitespace();
            self.expect(b':')?;
            let value = self.value()?;
            entries.insert(key, value);
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b'}') => {
                    self.position += 1;
                    return Ok(JsonValue::Object(entries));
                }
                other => return Err(format!("expected ',' or '}}', got {other:?}")),
            }
        }
    }

    fn array(&mut self) -> Result<JsonValue, String> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_whitespace();
        if self.peek() == Some(b']') {
            self.position += 1;
            return Ok(JsonValue::Array(items));
        }
        loop {
            items.push(self.value()?);
            self.skip_whitespace();
            match self.peek() {
                Some(b',') => self.position += 1,
                Some(b']') => {
                    self.position += 1;
                    return Ok(JsonValue::Array(items));
                }
                other => return Err(format!("expected ',' or ']', got {other:?}")),
            }
        }
    }

    fn string(&mut self) -> Result<String, String> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let byte = self.next().ok_or("unterminated string")?;
            match byte {
                b'"' => return Ok(out),
                b'\\' => {
                    let escape = self.next().ok_or("unterminated escape")?;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{08}'),
                        b'f' => out.push('\u{0c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let code = self.hex4()?;
                            let character = char::from_u32(code)
                                .ok_or_else(|| "invalid unicode escape".to_string())?;
                            out.push(character);
                        }
                        other => return Err(format!("invalid escape {other:?}")),
                    }
                }
                other if other < 0x80 => out.push(char::from(other)),
                _ => {
                    self.position -= 1;
                    let rest = std::str::from_utf8(&self.bytes[self.position..])
                        .map_err(|error| error.to_string())?;
                    let character = rest.chars().next().ok_or("truncated UTF-8")?;
                    out.push(character);
                    self.position += character.len_utf8();
                }
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let mut code = 0u32;
        for _ in 0..4 {
            let byte = self.next().ok_or("truncated unicode escape")?;
            let digit = char::from(byte)
                .to_digit(16)
                .ok_or_else(|| "invalid unicode escape".to_string())?;
            code = code * 16 + digit;
        }
        Ok(code)
    }

    fn number(&mut self) -> Result<JsonValue, String> {
        let start = self.position;
        if self.peek() == Some(b'-') {
            self.position += 1;
        }
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.position += 1;
        }
        let text = std::str::from_utf8(&self.bytes[start..self.position])
            .map_err(|error| error.to_string())?;
        if let Ok(value) = text.parse::<u64>() {
            return Ok(JsonValue::UInt(value));
        }
        text.parse::<i64>()
            .map(JsonValue::Int)
            .map_err(|error| error.to_string())
    }

    fn literal(&mut self, expected: &str, value: JsonValue) -> Result<JsonValue, String> {
        if self.bytes[self.position..].starts_with(expected.as_bytes()) {
            self.position += expected.len();
            Ok(value)
        } else {
            Err(format!("expected {expected:?}"))
        }
    }

    fn expect(&mut self, byte: u8) -> Result<(), String> {
        if self.next() == Some(byte) {
            Ok(())
        } else {
            Err(format!("expected {:?}", char::from(byte)))
        }
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.position += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.position).copied()
    }

    fn next(&mut self) -> Option<u8> {
        let byte = self.peek()?;
        self.position += 1;
        Some(byte)
    }
}

/// Hash helper used by tests to inspect payload digests.
#[must_use]
pub fn payload_sha256(payload: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(payload);
    hasher.finalize_hex()
}
