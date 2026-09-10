//! Live Alpaca equity broker boundary.
//!
//! The synchronous kernel talks to the broker through a fire-and-forget
//! command surface, while the async shell owns the REST transport. This port
//! keeps the same split: [`AlpacaOrderDirectory`] and [`AlpacaExecutionInbox`]
//! are deterministic and network-free, and [`AlpacaBroker`] wraps an injectable
//! [`AsyncBrokerTransport`] implemented over `tokio` in production and by a
//! recording fake in tests.

use std::collections::{HashMap, VecDeque};

use crate::accounting::{ExecutionReport, Fill};
use crate::error::{require_text, LiveError, LiveResult};
use crate::orders::{OrderRequest, SingleLegOrder};
use crate::venues::scripted::VenueOutcome;

/// An HTTP response returned by an [`AsyncBrokerTransport`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TransportResponse {
    /// HTTP status code.
    pub status: u16,
    /// Response body.
    pub body: String,
}

impl TransportResponse {
    /// Whether the response status is 2xx.
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
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
#[derive(Clone, Debug, Default)]
pub struct AlpacaExecutionInbox {
    directory: AlpacaOrderDirectory,
    outcomes: VecDeque<VenueOutcome>,
    sequence: i64,
}

impl AlpacaExecutionInbox {
    /// Construct an empty inbox.
    pub fn new() -> Self {
        Self::default()
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
            Some(limit) => self.outcomes.drain(..limit.min(self.outcomes.len())).collect(),
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
        let response = self
            .transport
            .request("POST", &url, Some(body))
            .await?;
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

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::{
        AlpacaBroker, AlpacaClientOrderIdSource, AlpacaOrderBinding, AlpacaOrderDirectory,
        AsyncBrokerTransport, TransportResponse,
    };
    use crate::error::LiveResult;
    use crate::orders::{OrderType, Side, SingleLegOrder, TimeInForce};
    use crate::symbol::Symbol;

    #[derive(Default)]
    struct RecordingTransport {
        requests: RefCell<Vec<(String, String, Option<String>)>>,
        status: u16,
    }

    impl AsyncBrokerTransport for RecordingTransport {
        async fn request(
            &self,
            method: &str,
            url: &str,
            body: Option<String>,
        ) -> LiveResult<TransportResponse> {
            self.requests
                .borrow_mut()
                .push((method.to_string(), url.to_string(), body));
            Ok(TransportResponse {
                status: self.status,
                body: String::new(),
            })
        }
    }

    fn order() -> SingleLegOrder {
        SingleLegOrder::new(
            "o1",
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            "1".parse().unwrap(),
            OrderType::Market,
            TimeInForce::Day,
            None,
            None,
            None,
        )
        .unwrap()
    }

    #[test]
    fn directory_round_trips_identity() {
        let mut directory = AlpacaOrderDirectory::new();
        directory.bind(AlpacaOrderBinding {
            client_order_id: "strat-1".into(),
            order_id: "o1".into(),
        });
        assert_eq!(directory.client_order_id_for("o1"), Some("strat-1"));
        assert_eq!(
            directory.binding_for_client("strat-1").unwrap().order_id,
            "o1"
        );
    }

    #[tokio::test]
    async fn broker_submits_over_async_transport() {
        let transport = RecordingTransport {
            status: 200,
            ..Default::default()
        };
        let source = AlpacaClientOrderIdSource::new("strat").unwrap();
        let mut broker = AlpacaBroker::new(transport, "https://paper-api.alpaca.markets", source)
            .unwrap();
        let receipt = broker.submit(&order()).await.unwrap();
        assert_eq!(receipt.status_code, 200);
        assert_eq!(broker.directory().client_order_id_for("o1"), Some("strat-1"));
    }
}
