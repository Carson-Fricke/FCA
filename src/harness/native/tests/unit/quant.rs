use super::{implied_time, implied_underlying, price, price_and_greeks, MAX_STEPS};

fn black_scholes(s: f64, k: f64, t: f64, r: f64, q: f64, sigma: f64, kind_call: bool) -> f64 {
    let root_t = t.sqrt();
    let d1 = ((s / k).ln() + (r - q + sigma * sigma / 2.0) * t) / (sigma * root_t);
    let d2 = d1 - sigma * root_t;
    if kind_call {
        s * (-q * t).exp() * norm_cdf(d1) - k * (-r * t).exp() * norm_cdf(d2)
    } else {
        k * (-r * t).exp() * norm_cdf(-d2) - s * (-q * t).exp() * norm_cdf(-d1)
    }
}

fn norm_cdf(x: f64) -> f64 {
    let ax = x.abs() / 2.0f64.sqrt();
    let tau = 1.0 / (1.0 + 0.3275911 * ax);
    let poly = tau
        * (0.254829592
            + tau
                * (-0.284496736 + tau * (1.421413741 + tau * (-1.453152027 + tau * 1.061405429))));
    let erf = 1.0 - poly * (-ax * ax).exp();
    0.5 * (1.0 + if x < 0.0 { -erf } else { erf })
}

#[test]
fn crr_european_options_converge_to_black_scholes() {
    let cases = [
        (450.0, 450.0, 0.15, true),
        (455.0, 445.0, 0.32, false),
        (600.0, 590.0, 0.08, true),
    ];
    for (s, k, sigma, kind_call) in cases {
        let reference = black_scholes(s, k, 2.0 / 365.0, 0.05, 0.012, sigma, kind_call);
        let value = price(s, k, 2.0 / 365.0, 0.05, 0.012, sigma, kind_call, false, 512);
        assert!((value - reference).abs() < 2e-3);
    }
}

#[test]
fn european_put_call_parity_holds_through_the_tree() {
    let (s, k, t, r, q, sigma) = (470.0, 465.0, 3.0 / 365.0, 0.045, 0.013, 0.21);
    let call = price(s, k, t, r, q, sigma, true, false, 400);
    let put = price(s, k, t, r, q, sigma, false, false, 400);
    let parity = s * (-q * t).exp() - k * (-r * t).exp();
    assert!((parity - (call - put)).abs() < 1e-8);
}

#[test]
fn american_put_is_worth_at_least_the_european_put() {
    let american = price(90.0, 100.0, 30.0 / 365.0, 0.05, 0.0, 0.25, false, true, 256);
    let european = price(
        90.0,
        100.0,
        30.0 / 365.0,
        0.05,
        0.0,
        0.25,
        false,
        false,
        256,
    );
    assert!(american >= european);
    assert!(american >= 100.0 - 90.0);
}

#[test]
fn at_the_money_theta_is_negative_and_greeks_stay_in_bounds() {
    let greeks = price_and_greeks(500.0, 500.0, 1.0 / 365.0, 0.04, 0.0, 0.30, true, true, 256);
    assert!(greeks.theta < 0.0);
    assert!(greeks.delta > 0.0 && greeks.delta < 1.0);
    assert!(greeks.gamma > 0.0);
    assert!(greeks.vega > 0.0);
    assert!(
        (black_scholes(500.0, 500.0, 1.0 / 365.0, 0.04, 0.0, 0.30, true) - greeks.price).abs()
            < 5e-3
    );
}

#[test]
fn zero_expiry_returns_the_intrinsic_value() {
    let call = price(105.0, 100.0, 0.0, 0.04, 0.0, 0.20, true, true, 128);
    let put = price(95.0, 100.0, 0.0, 0.04, 0.0, 0.20, false, true, 128);
    assert_eq!(call, 5.0);
    assert_eq!(put, 5.0);
}

#[test]
fn invalid_inputs_produce_nan_instead_of_errors() {
    assert!(price(100.0, 100.0, 0.01, 0.04, 0.0, 0.0, true, false, 64).is_nan());
    assert!(price(100.0, 100.0, 0.01, 0.04, 0.0, 0.20, true, false, 1).is_nan());
    assert!(price(
        100.0,
        100.0,
        0.01,
        0.04,
        0.0,
        0.20,
        true,
        false,
        MAX_STEPS + 1
    )
    .is_nan());
    assert!(price(-100.0, 100.0, 0.01, 0.04, 0.0, 0.20, true, false, 64).is_nan());
}

#[test]
fn implied_time_round_trips_through_the_pricer() {
    let target_t = 4.0 / (365.0 * 24.0);
    let target_price = price(500.0, 502.0, target_t, 0.04, 0.0, 0.35, true, true, 96);
    let solved = implied_time(
        500.0,
        502.0,
        0.04,
        0.0,
        0.35,
        true,
        true,
        96,
        target_price,
        target_t / 4.0,
        target_t * 4.0,
        64,
    );
    assert!(solved.is_finite());
    assert!((target_t - solved).abs() < 1e-9);
}

#[test]
fn implied_time_without_a_sign_changing_bracket_is_nan() {
    let solved = implied_time(
        500.0,
        502.0,
        0.04,
        0.0,
        0.35,
        true,
        true,
        96,
        1e6,
        1.0 / (365.0 * 24.0),
        1.0,
        32,
    );
    assert!(solved.is_nan());
}

#[test]
fn implied_underlying_round_trips_through_the_pricer() {
    let target_s = 503.75;
    let target_price = price(target_s, 500.0, 1.0 / 48.0, 0.04, 0.0, 0.30, true, true, 96);
    let solved = implied_underlying(
        500.0,
        1.0 / 48.0,
        0.04,
        0.0,
        0.30,
        true,
        true,
        96,
        target_price,
        target_s * 0.5,
        target_s * 1.5,
        64,
    );
    assert!(solved.is_finite());
    assert!((target_s - solved).abs() < 1e-6);
}
