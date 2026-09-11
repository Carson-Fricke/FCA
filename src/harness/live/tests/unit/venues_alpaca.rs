use std::cell::RefCell;

use super::{
    AlpacaBroker, AlpacaClientOrderIdSource, AlpacaExecutionInbox, AlpacaOrderBinding,
    AlpacaOrderDirectory, AsyncBrokerTransport, TransportResponse,
};
use crate::error::LiveResult;
use crate::orders::{ExecutionType, OrderStatus, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::scripted::VenueOutcome;

fn dec(value: &str) -> num::Decimal18 {
    value.parse().unwrap()
}

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
            headers: Vec::new(),
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
    let mut broker =
        AlpacaBroker::new(transport, "https://paper-api.alpaca.markets", source).unwrap();
    let receipt = broker.submit(&order()).await.unwrap();
    assert_eq!(receipt.status_code, 200);
    assert_eq!(
        broker.directory().client_order_id_for("o1"),
        Some("strat-1")
    );
}

const OBSERVED_MILLIS: i64 = 1_700_000_000_000;

fn fill_frame(event: &str, filled: &str) -> String {
    format!(
        "{{\"stream\":\"trade_updates\",\"data\":{{\"event\":\"{event}\",\
         \"at\":\"2023-11-14T22:13:20Z\",\"order\":{{\"client_order_id\":\"strat-1\",\
         \"symbol\":\"AAPL\",\"qty\":\"10\",\"filled_qty\":\"{filled}\",\
         \"filled_avg_price\":\"150.5\",\"side\":\"buy\"}},\
         \"execution\":{{\"id\":\"exec-1\",\"qty\":\"10\",\"price\":\"150.5\"}}}}}}"
    )
}

#[test]
fn inbox_normalizes_fill_and_resolves_identity() {
    let mut inbox = AlpacaExecutionInbox::new();
    inbox.directory_mut().bind(AlpacaOrderBinding {
        client_order_id: "strat-1".into(),
        order_id: "o1".into(),
    });
    let outcomes = inbox
        .push_frame(&fill_frame("fill", "10"), OBSERVED_MILLIS)
        .unwrap();
    assert_eq!(outcomes.len(), 2);
    match &outcomes[0] {
        VenueOutcome::Report(report) => {
            assert_eq!(report.order_id, "o1");
            assert_eq!(report.status, OrderStatus::Filled);
            assert_eq!(report.execution_type, ExecutionType::Fill);
            assert_eq!(report.order_quantity, dec("10"));
            assert_eq!(report.filled_quantity, dec("10"));
            assert_eq!(report.last_fill_price, Some(dec("150.5")));
        }
        other => panic!("expected report, got {other:?}"),
    }
    match &outcomes[1] {
        VenueOutcome::Fill(fill) => {
            assert_eq!(fill.fill_id, "alpaca:fill:exec-1");
            assert_eq!(fill.order_id, "o1");
            assert_eq!(fill.symbol.as_str(), "AAPL");
            assert_eq!(fill.side, Side::Buy);
            assert_eq!(fill.quantity, dec("10"));
            assert_eq!(fill.price, dec("150.5"));
        }
        other => panic!("expected fill, got {other:?}"),
    }
    assert_eq!(inbox.len(), 2);
}

#[test]
fn inbox_rejects_unsupported_event_and_bad_quantities() {
    let mut inbox = AlpacaExecutionInbox::new();
    let unknown = fill_frame("wat", "10");
    assert!(inbox.push_frame(&unknown, OBSERVED_MILLIS).is_err());
    let over = fill_frame("fill", "11");
    assert!(inbox.push_frame(&over, OBSERVED_MILLIS).is_err());
    let no_execution = "{\"stream\":\"trade_updates\",\"data\":{\"event\":\"fill\",\
        \"at\":\"2023-11-14T22:13:20Z\",\"order\":{\"client_order_id\":\"c\",\
        \"symbol\":\"AAPL\",\"qty\":\"10\",\"filled_qty\":\"10\",\"side\":\"buy\"}}}";
    assert!(inbox.push_frame(no_execution, OBSERVED_MILLIS).is_err());
    assert!(inbox.is_empty());
}

#[test]
fn inbox_enforces_timestamp_tolerances() {
    let mut inbox = AlpacaExecutionInbox::new();
    let stale = fill_frame("fill", "10");
    assert!(inbox.push_frame(&stale, OBSERVED_MILLIS + 600_000).is_err());
    assert!(inbox.push_frame(&stale, OBSERVED_MILLIS - 10_000).is_err());
    assert!(inbox.push_frame(&stale, OBSERVED_MILLIS).is_ok());
}
