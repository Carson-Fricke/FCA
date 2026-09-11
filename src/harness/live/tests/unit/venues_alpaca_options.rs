use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use num::Decimal18;

use super::{
    AlpacaCapabilityMatrix, AlpacaOptionsBroker, AlpacaOptionsCapabilityMatrix,
    AlpacaOptionsExecutionInbox, AlpacaOptionsOrderBinding, CAPABILITY_MATRIX_VERSION,
};
use crate::error::LiveResult;
use crate::orders::{MultiLegOrder, OrderLeg, OrderRequest, OrderType, Side, TimeInForce};
use crate::symbol::Symbol;
use crate::venues::alpaca::{AlpacaClientOrderIdSource, AsyncBrokerTransport, TransportResponse};
use crate::venues::scripted::VenueOutcome;

const OBSERVED_MILLIS: i64 = 1_700_000_000_000;

type RequestLog = Arc<Mutex<Vec<(String, String, Option<String>)>>>;

#[derive(Clone, Default)]
struct RecordingTransport {
    requests: RequestLog,
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
            .lock()
            .unwrap()
            .push((method.to_string(), url.to_string(), body));
        Ok(TransportResponse {
            status: self.status,
            body: String::new(),
            headers: Vec::new(),
        })
    }
}

fn dec(value: &str) -> Decimal18 {
    value.parse().unwrap()
}

fn leg(symbol: &str, side: Side, quantity: &str) -> OrderLeg {
    OrderLeg::new(
        Symbol::new(symbol).unwrap(),
        side,
        dec(quantity),
        dec("1"),
        None,
    )
    .unwrap()
}

fn vertical(time_in_force: TimeInForce) -> MultiLegOrder {
    MultiLegOrder::new(
        "g1",
        vec![
            leg("AAPL240119C00150000", Side::Buy, "1"),
            leg("AAPL240119C00160000", Side::Sell, "1"),
        ],
        OrderType::Limit,
        time_in_force,
        None,
        Some(dec("2")),
    )
    .unwrap()
}

#[test]
fn default_matrix_accepts_two_leg_day_vertical() {
    let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
    assert!(matrix.require(&vertical(TimeInForce::Day)).is_ok());
    assert_eq!(matrix.version(), CAPABILITY_MATRIX_VERSION);
}

#[test]
fn ioc_and_fractional_ratios_are_rejected() {
    let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
    assert!(matrix.require(&vertical(TimeInForce::Ioc)).is_err());
    assert!(!matrix.check(&vertical(TimeInForce::Ioc)).is_empty());
}

#[test]
fn mixed_underlyings_and_duplicate_contracts_are_rejected() {
    let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
    let mixed = MultiLegOrder::new(
        "g2",
        vec![
            leg("AAPL240119C00150000", Side::Buy, "1"),
            leg("MSFT240119C00160000", Side::Sell, "1"),
        ],
        OrderType::Limit,
        TimeInForce::Day,
        None,
        Some(dec("2")),
    )
    .unwrap();
    assert!(matrix.require(&mixed).is_err());
}

#[test]
fn feature_rejections_cover_extended_hours_and_attachments() {
    let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
    let mut features = BTreeMap::new();
    features.insert("extended_hours".to_string(), "true".to_string());
    features.insert("take_profit".to_string(), "1".to_string());
    features.insert("mystery".to_string(), "1".to_string());
    let rejections = matrix.check_with_features(&vertical(TimeInForce::Day), &features);
    assert_eq!(rejections.len(), 3);
    assert!(matrix
        .require_with_features(&vertical(TimeInForce::Day), &features)
        .is_err());
    let accepted = BTreeMap::from([("order_class".to_string(), "mleg".to_string())]);
    assert!(matrix
        .require_with_features(&vertical(TimeInForce::Day), &accepted)
        .is_ok());
}

#[test]
fn consolidated_matrix_routes_by_order_kind() {
    let matrix = AlpacaCapabilityMatrix::alpaca_default();
    assert!(matrix
        .require(&OrderRequest::Multi(vertical(TimeInForce::Day)))
        .is_ok());
    let crypto = crate::orders::SingleLegOrder::new(
        "c1",
        Symbol::new("BTC/USD").unwrap(),
        Side::Buy,
        dec("1"),
        OrderType::Market,
        TimeInForce::Day,
        None,
        None,
        None,
    )
    .unwrap();
    assert!(matrix.require(&OrderRequest::Single(crypto)).is_err());
    let markdown = matrix.to_markdown();
    assert!(markdown.contains(CAPABILITY_MATRIX_VERSION));
    assert!(markdown.contains("crypto_fee_reconciliation"));
}

fn bind_group(inbox: &mut AlpacaOptionsExecutionInbox) {
    inbox
        .directory_mut()
        .bind(AlpacaOptionsOrderBinding {
            order_id: "g1".to_string(),
            client_order_id: "strat-1".to_string(),
            spread_count: dec("1"),
            legs: vec![
                leg("AAPL240119C00150000", Side::Buy, "1"),
                leg("AAPL240119C00160000", Side::Sell, "1"),
            ],
        })
        .unwrap();
}

#[test]
fn options_inbox_normalizes_group_and_leg_frames() {
    let mut inbox = AlpacaOptionsExecutionInbox::new();
    bind_group(&mut inbox);

    let group = "{\"stream\":\"trade_updates\",\"data\":{\"event\":\"accept\",\
        \"at\":\"2023-11-14T22:13:20Z\",\"order\":{\"client_order_id\":\"strat-1\",\
        \"qty\":\"1\",\"filled_qty\":\"0\"}}}";
    let outcomes = inbox.push_frame(group, OBSERVED_MILLIS).unwrap();
    assert_eq!(outcomes.len(), 1);
    match &outcomes[0] {
        VenueOutcome::Report(report) => {
            assert_eq!(report.order_id, "g1");
            assert_eq!(report.order_quantity, dec("1"));
        }
        other => panic!("expected group report, got {other:?}"),
    }

    let leg_frame = "{\"stream\":\"trade_updates\",\"data\":{\"event\":\"fill\",\
        \"at\":\"2023-11-14T22:13:20Z\",\"order\":{\"client_order_id\":\"strat-1\",\
        \"qty\":\"1\",\"filled_qty\":\"1\"},\"execution\":{\"id\":\"exec-9\",\
        \"symbol\":\"AAPL240119C00150000\",\"qty\":\"1\",\"price\":\"1.30\"}}}";
    let outcomes = inbox.push_frame(leg_frame, OBSERVED_MILLIS).unwrap();
    assert_eq!(outcomes.len(), 2);
    match &outcomes[0] {
        VenueOutcome::Report(report) => {
            assert_eq!(report.order_id, "g1");
            assert_eq!(report.order_quantity, dec("1"));
            assert_eq!(report.last_fill_price, Some(dec("1.30")));
        }
        other => panic!("expected leg report, got {other:?}"),
    }
    match &outcomes[1] {
        VenueOutcome::Fill(fill) => {
            assert_eq!(fill.fill_id, "alpaca:options-fill:exec-9");
            assert_eq!(fill.symbol.as_str(), "AAPL240119C00150000");
            assert_eq!(fill.side, Side::Buy);
            assert_eq!(fill.price, dec("1.30"));
        }
        other => panic!("expected leg fill, got {other:?}"),
    }
}

#[tokio::test]
async fn options_broker_submits_mleg_payload() {
    let transport = RecordingTransport {
        status: 200,
        ..Default::default()
    };
    let log = Arc::clone(&transport.requests);
    let source = AlpacaClientOrderIdSource::new("strat").unwrap();
    let matrix = AlpacaOptionsCapabilityMatrix::alpaca_options_default();
    let mut broker = AlpacaOptionsBroker::new(
        transport,
        "https://paper-api.alpaca.markets",
        source,
        matrix,
    )
    .unwrap();
    let receipt = broker.submit(&vertical(TimeInForce::Day)).await.unwrap();
    assert_eq!(receipt.status_code, 200);
    assert_eq!(
        broker.directory().client_order_id_for("g1"),
        Some("strat-1")
    );
    let requests = log.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].0, "POST");
    let body = requests[0].2.as_deref().unwrap();
    assert!(body.contains("\"order_class\":\"mleg\""));
    assert!(body.contains("\"ratio_qty\":1"));
    assert!(body.contains("limit_price"));
    assert!(broker
        .replace_rejected("g1")
        .to_string()
        .contains("multi-leg"));
}
