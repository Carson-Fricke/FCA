//! Pure numerical model checks complement the HTTP option-selection behavior suite.
use lake_contracts::OptionRight;
use lake_ingestion::options::resolve::implied_delta;

#[test]
fn implied_call_and_put_delta_match_the_known_atm_solution() {
    let price = 7.9655674554;
    let call = implied_delta(price, 100.0, 100.0, 1.0, 0.0, 0.0, OptionRight::Call).unwrap();
    let put = implied_delta(price, 100.0, 100.0, 1.0, 0.0, 0.0, OptionRight::Put).unwrap();
    assert!((call - 0.5398278373).abs() < 0.000001);
    assert!((put + 0.4601721627).abs() < 0.000001);
    assert!((call - put - 1.0).abs() < 0.000001);
}

#[test]
fn impossible_prices_and_nonfinite_or_expired_inputs_have_no_model_delta() {
    for (price, spot, years) in [
        (101.0, 100.0, 1.0),
        (0.0, 100.0, 1.0),
        (5.0, 0.0, 1.0),
        (5.0, 100.0, 0.0),
        (f64::NAN, 100.0, 1.0),
    ] {
        assert_eq!(
            implied_delta(price, spot, 100.0, years, 0.0, 0.0, OptionRight::Call),
            None
        );
    }
}
