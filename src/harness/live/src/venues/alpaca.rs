//! Live Alpaca equity broker boundary.
//!
//! The synchronous kernel talks to the broker through a fire-and-forget
//! command surface, while the async shell owns the REST transport. This port
//! keeps the same split: [`AlpacaOrderDirectory`] and [`AlpacaExecutionInbox`]
//! are deterministic and network-free, and [`AlpacaBroker`] wraps an injectable
//! [`AsyncBrokerTransport`] implemented over `tokio` in production and by a
//! recording fake in tests.

use std::collections::HashMap;
use std::collections::VecDeque;

use num::Decimal18;

use crate::venues::scripted::VenueOutcome;
use harness_trading::accounting::ExecutionReport;
use harness_trading::accounting::Fill;
use harness_trading::error::require_text;
use harness_trading::error::LiveError;
use harness_trading::error::LiveResult;
use harness_trading::orders::ExecutionType;
use harness_trading::orders::OrderRequest;
use harness_trading::orders::OrderStatus;
use harness_trading::orders::Side;
use harness_trading::orders::SingleLegOrder;
use harness_trading::symbol::Symbol;

/// An HTTP response returned by an [`AsyncBrokerTransport`].
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TransportResponse {
    /// HTTP status code.
    pub status: u16,
    /// Response body.
    pub body: String,
    /// Response headers as `(name, value)` pairs.
    pub headers: Vec<(String, String)>,
}

impl TransportResponse {
    /// Whether the response status is 2xx.
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }

    /// Return the first header value matching `name`, case-insensitively.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// Parse a non-negative `Retry-After` delay in seconds, when present.
    pub fn retry_after_seconds(&self) -> Option<f64> {
        let seconds: f64 = self.header("retry-after")?.trim().parse().ok()?;
        if seconds.is_finite() && seconds >= 0.0 {
            Some(seconds)
        } else {
            None
        }
    }
}

/// The async REST command transport injected into [`AlpacaBroker`].
pub trait AsyncBrokerTransport {
    /// Perform one request and return the decoded response.
    fn request(
        &self,
        method: &str,
        url: &str,
        body: Option<String>,
    ) -> impl std::future::Future<Output = LiveResult<TransportResponse>>;
}

/// A monotonic, strategy-tagged client order identifier source.
#[derive(Clone, Debug)]
pub struct AlpacaClientOrderIdSource {
    strategy_tag: String,
    ordinal: u64,
}

impl AlpacaClientOrderIdSource {
    /// Construct an id source for one strategy instance.
    pub fn new(strategy_id: &str) -> LiveResult<Self> {
        Ok(Self {
            strategy_tag: require_text(strategy_id, "strategy_id")?,
            ordinal: 0,
        })
    }

    /// The strategy tag embedded in every identifier.
    pub fn strategy_tag(&self) -> &str {
        &self.strategy_tag
    }

    /// Mint the next client order identifier.
    pub fn next_client_order_id(&mut self) -> String {
        self.ordinal += 1;
        format!("{}-{}", self.strategy_tag, self.ordinal)
    }
}

/// A command acknowledgement returned to the kernel.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaCommandReceipt {
    /// The command name (`submit`, `cancel`, `replace`, `position_snapshot`).
    pub command: String,
    /// The client order identifier, when the command is order-scoped.
    pub client_order_id: Option<String>,
    /// The provider order identifier, when known.
    pub order_id: Option<String>,
    /// The HTTP status code returned by the provider.
    pub status_code: u16,
}

/// Immutable identity binding between a kernel order and a provider order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AlpacaOrderBinding {
    /// The client order identifier sent to the provider.
    pub client_order_id: String,
    /// The kernel order identifier.
    pub order_id: String,
}

/// Bidirectional identity map between kernel and provider order identifiers.
#[derive(Clone, Debug, Default)]
pub struct AlpacaOrderDirectory {
    by_client: HashMap<String, AlpacaOrderBinding>,
    chains: HashMap<String, Vec<String>>,
}

impl AlpacaOrderDirectory {
    /// Construct an empty directory.
    pub fn new() -> Self {
        Self::default()
    }

    /// Record a binding.
    pub fn bind(&mut self, binding: AlpacaOrderBinding) {
        self.by_client
            .insert(binding.client_order_id.clone(), binding);
    }

    /// Link a replacement client id to its original client id.
    pub fn link_replacement(&mut self, original_client_id: &str, replacement_client_id: &str) {
        self.chains
            .entry(original_client_id.to_string())
            .or_default()
            .push(replacement_client_id.to_string());
    }

    /// Return the binding for a client order identifier.
    pub fn binding_for_client(&self, client_order_id: &str) -> Option<&AlpacaOrderBinding> {
        self.by_client.get(client_order_id)
    }

    /// Return the confirmed replacement chain containing a client identifier.
    pub fn chain_for_client(&self, client_order_id: &str) -> Vec<AlpacaOrderBinding> {
        let mut chain = vec![client_order_id.to_string()];
        while let Some(next) = self
            .chains
            .get(chain.last().expect("non-empty"))
            .and_then(|links| links.first())
        {
            chain.push(next.clone());
        }
        chain
            .iter()
            .filter_map(|id| self.by_client.get(id).cloned())
            .collect()
    }

    /// Return the client order identifier for a kernel order identifier.
    pub fn client_order_id_for(&self, order_id: &str) -> Option<&str> {
        self.by_client
            .values()
            .find(|binding| binding.order_id == order_id)
            .map(|binding| binding.client_order_id.as_str())
    }
}

/// Deterministic inbox that normalizes broker outcomes for the kernel.
#[derive(Clone, Debug)]
pub struct AlpacaExecutionInbox {
    directory: AlpacaOrderDirectory,
    outcomes: VecDeque<VenueOutcome>,
    sequence: i64,
    max_future_skew_millis: i64,
    max_event_age_millis: i64,
}

impl Default for AlpacaExecutionInbox {
    fn default() -> Self {
        Self {
            directory: AlpacaOrderDirectory::new(),
            outcomes: VecDeque::new(),
            sequence: 0,
            max_future_skew_millis: 5_000,
            max_event_age_millis: 300_000,
        }
    }
}

impl AlpacaExecutionInbox {
    /// Construct an empty inbox with the default timestamp tolerances.
    pub fn new() -> Self {
        Self::default()
    }

    /// The maximum provider-timestamp future skew, in milliseconds.
    pub fn max_future_skew_millis(&self) -> i64 {
        self.max_future_skew_millis
    }

    /// Override the maximum provider-timestamp future skew, in milliseconds.
    pub fn set_max_future_skew_millis(&mut self, value: i64) -> LiveResult<()> {
        if value < 0 {
            return Err(LiveError::Venue(
                "max_future_skew_millis must not be negative".into(),
            ));
        }
        self.max_future_skew_millis = value;
        Ok(())
    }

    /// Override the maximum provider-timestamp age, in milliseconds.
    pub fn set_max_event_age_millis(&mut self, value: i64) -> LiveResult<()> {
        if value <= 0 {
            return Err(LiveError::Venue(
                "max_event_age_millis must be positive".into(),
            ));
        }
        self.max_event_age_millis = value;
        Ok(())
    }

    /// The identity directory shared with the broker.
    pub fn directory(&self) -> &AlpacaOrderDirectory {
        &self.directory
    }

    /// Mutable access to the identity directory.
    pub fn directory_mut(&mut self) -> &mut AlpacaOrderDirectory {
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

    /// Queue an execution report.
    pub fn push_report(&mut self, report: ExecutionReport) {
        self.outcomes.push_back(VenueOutcome::Report(report));
    }

    /// Queue a fill.
    pub fn push_fill(&mut self, fill: Fill) {
        self.outcomes.push_back(VenueOutcome::Fill(fill));
    }

    /// Normalize and atomically enqueue one decoded Alpaca trade-update frame.
    ///
    /// `frame` is the JSON text delivered on the trade-updates WebSocket and
    /// `observed_at_millis` is the async observer's wall-clock stamp. The inbox
    /// performs no clock, network, or random access of its own.
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
        let data = required_object(object, "data")?;
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
        let event_name = required_text_field(data, "event")?;
        let (status, execution_type) = trade_update_outcome(&event_name).ok_or_else(|| {
            LiveError::Venue(format!(
                "unsupported Alpaca broker trade update event: {event_name:?}"
            ))
        })?;
        let order = required_object(data, "order")?;
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

        let client_order_id = required_text_field(order, "client_order_id")?;
        let symbol_text = required_text_field(order, "symbol")?;
        let order_quantity = required_positive_decimal(order, "qty")?;
        let filled_quantity = required_nonnegative_decimal(order, "filled_qty")?;
        if filled_quantity > order_quantity {
            return Err(LiveError::Venue(
                "Alpaca field filled_qty cannot exceed qty".into(),
            ));
        }
        let average_fill_price = optional_positive_decimal(order, "filled_avg_price")?;
        let order_id = self
            .directory
            .binding_for_client(&client_order_id)
            .map(|binding| binding.order_id.clone())
            .unwrap_or_else(|| client_order_id.clone());
        let symbol = Symbol::new(&symbol_text)?;

        let mut outcomes = Vec::new();
        let (last_fill_quantity, last_fill_price) = if is_fill_execution(execution_type) {
            let execution = execution.ok_or_else(|| {
                LiveError::Venue(format!(
                    "Alpaca {event_name} update requires an execution object"
                ))
            })?;
            let execution_id = required_text_field(execution, "id")?;
            let fill_quantity = required_positive_decimal(execution, "qty")?;
            let fill_price = required_positive_decimal(execution, "price")?;
            let side = parse_side(&required_text_field(order, "side")?)?;
            outcomes.push(VenueOutcome::Fill(Fill::new(
                &format!("alpaca:fill:{execution_id}"),
                &order_id,
                symbol,
                side,
                fill_quantity,
                fill_price,
                Decimal18::ZERO,
                None,
            )?));
            (fill_quantity, Some(fill_price))
        } else {
            (Decimal18::ZERO, None)
        };

        let report_sequence = self.sequence + outcomes.len() as i64;
        let report = ExecutionReport::new(
            &format!("alpaca:report:{client_order_id}:{report_sequence}"),
            &order_id,
            status,
            execution_type,
            order_quantity,
            filled_quantity,
            order_quantity - filled_quantity,
            average_fill_price,
            last_fill_quantity,
            last_fill_price,
            Some(format!("alpaca:{event_name}")),
        )?;
        outcomes.insert(0, VenueOutcome::Report(report));
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

fn required_object<'a>(
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

fn required_text_field(
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

/// The async Alpaca equity broker shell.
pub struct AlpacaBroker<T: AsyncBrokerTransport> {
    transport: T,
    base_url: String,
    id_source: AlpacaClientOrderIdSource,
    directory: AlpacaOrderDirectory,
}

impl<T: AsyncBrokerTransport> AlpacaBroker<T> {
    /// Construct a broker over an injected transport.
    pub fn new(
        transport: T,
        base_url: &str,
        id_source: AlpacaClientOrderIdSource,
    ) -> LiveResult<Self> {
        Ok(Self {
            transport,
            base_url: require_text(base_url, "base_url")?,
            id_source,
            directory: AlpacaOrderDirectory::new(),
        })
    }

    /// The identity directory.
    pub fn directory(&self) -> &AlpacaOrderDirectory {
        &self.directory
    }

    /// Submit a single-leg equity order and bind its client identifier.
    pub async fn submit(&mut self, order: &SingleLegOrder) -> LiveResult<AlpacaCommandReceipt> {
        let client_order_id = self.id_source.next_client_order_id();
        let url = format!("{}/v2/orders", self.base_url);
        let body = format!(
            "{{\"symbol\":\"{}\",\"qty\":\"{}\",\"side\":\"{}\"}}",
            order.symbol.as_str(),
            order.quantity,
            order.side.as_str()
        );
        let response = self.transport.request("POST", &url, Some(body)).await?;
        if !response.is_success() {
            return Err(LiveError::Venue(format!(
                "alpaca submit failed with status {}",
                response.status
            )));
        }
        self.directory.bind(AlpacaOrderBinding {
            client_order_id: client_order_id.clone(),
            order_id: order.order_id.clone(),
        });
        Ok(AlpacaCommandReceipt {
            command: "submit".into(),
            client_order_id: Some(client_order_id),
            order_id: Some(order.order_id.clone()),
            status_code: response.status,
        })
    }

    /// Cancel an order by kernel order identifier.
    pub async fn cancel(&mut self, order_id: &str) -> LiveResult<AlpacaCommandReceipt> {
        let client = self
            .directory
            .client_order_id_for(order_id)
            .map(str::to_string)
            .ok_or_else(|| LiveError::Venue(format!("unknown alpaca order {order_id:?}")))?;
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
    pub fn replace_rejected(&self, order: &OrderRequest) -> LiveError {
        LiveError::Venue(format!(
            "alpaca does not support replacing order {}",
            order.order_id()
        ))
    }

    /// Fetch provider positions as raw JSON rows.
    pub async fn positions(&self) -> LiveResult<Vec<String>> {
        let url = format!("{}/v2/positions", self.base_url);
        let response = self.transport.request("GET", &url, None).await?;
        if !response.is_success() {
            return Err(LiveError::Venue(format!(
                "alpaca positions failed with status {}",
                response.status
            )));
        }
        Ok(if response.body.is_empty() {
            Vec::new()
        } else {
            vec![response.body]
        })
    }
}
