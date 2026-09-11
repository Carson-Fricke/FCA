use super::{ScriptedStep, ScriptedVenue, UnexpectedVenueCall, VenueCall, VenueOperation};
use crate::orders::{OrderRequest, OrderType, Side, SingleLegOrder, TimeInForce};
use crate::symbol::Symbol;

fn order(order_id: &str) -> OrderRequest {
    OrderRequest::Single(
        SingleLegOrder::new(
            order_id,
            Symbol::new("AAPL").unwrap(),
            Side::Buy,
            "1".parse().unwrap(),
            OrderType::Market,
            TimeInForce::Day,
            None,
            None,
            None,
        )
        .unwrap(),
    )
}

#[test]
fn scripted_venue_matches_calls_in_order() {
    let step = ScriptedStep {
        call: VenueCall::new(VenueOperation::Submit, Some("o1".to_string()), None, None).unwrap(),
        outcomes: vec![],
    };
    let mut venue = ScriptedVenue::new(vec![step]);
    venue.submit(&order("o1")).unwrap();
    assert_eq!(venue.calls().len(), 1);
    venue.assert_exhausted().unwrap();
}

#[test]
fn scripted_venue_rejects_mismatch() {
    let step = ScriptedStep {
        call: VenueCall::new(VenueOperation::Cancel, Some("o1".to_string()), None, None).unwrap(),
        outcomes: vec![],
    };
    let mut venue = ScriptedVenue::new(vec![step]);
    assert!(venue.submit(&order("o1")).is_err());
    assert_eq!(venue.remaining_steps(), 1);
    let _ = UnexpectedVenueCall {
        message: String::new(),
    };
}
