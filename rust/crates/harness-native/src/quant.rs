//! Float64 quantitative-analysis kernels for the native core.
//!
//! The first family is the Cox-Ross-Rubinstein (CRR) binomial option pricer: a
//! pure rollback kernel with tree greeks plus bracketed bisection inversions
//! for implied time to expiry and implied underlying price. These kernels are
//! research math, deliberately distinct from the pinned fixed-point engine
//! decimals. Every routine is a pure function of its inputs using IEEE-754
//! operations only, with no clock, no randomness, and no allocation.
//!
//! Conventions:
//! - Time to expiry is in years; rates, dividend yield, and volatility are
//!   continuously compounded annuals; theta is returned per year.
//! - The flag byte carries `call` in bit 0 and `american` in bit 1.
//! - Invalid elements produce NaN instead of an error so a batch never fails
//!   as a whole.

/// Largest supported binomial step count, also the stack-buffer capacity.
pub const MAX_STEPS: u32 = 1024;

/// Call flag bit shared with the C ABI and the Python facade.
pub const FLAG_CALL: u8 = 1;
/// American-exercise flag bit.
pub const FLAG_AMERICAN: u8 = 2;

/// Price and tree greeks for one input node.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Greeks {
    /// Option price.
    pub price: f64,
    /// Spot delta.
    pub delta: f64,
    /// Spot gamma.
    pub gamma: f64,
    /// Per year (multiply by 31_536_000 for per second at the 365-day year).
    pub theta: f64,
    /// Per unit of volatility (one-sided finite difference at h = 1e-4).
    pub vega: f64,
}

/// Volatility bump used for the one-sided vega finite difference.
const VEGA_BUMP: f64 = 1e-4;

#[derive(Clone, Copy)]
struct Captures {
    one_up: f64,
    one_down: f64,
    two_up: f64,
    two_mid: f64,
    two_down: f64,
}

fn finite_positive(value: f64) -> bool {
    value.is_finite() && value > 0.0
}

fn intrinsic(kind_call: bool, spot: f64, strike: f64) -> f64 {
    if kind_call {
        (spot - strike).max(0.0)
    } else {
        (strike - spot).max(0.0)
    }
}

fn step_up(sigma: f64, t: f64, steps: u32) -> f64 {
    let dt = t / f64::from(steps);
    (sigma * dt.sqrt()).exp()
}

/// Validate raw scalar inputs; returns false when the element must be NaN.
fn valid_inputs(s: f64, k: f64, t: f64, r: f64, q: f64, sigma: f64) -> bool {
    if !finite_positive(s) || !finite_positive(k) {
        return false;
    }
    if !t.is_finite() || t < 0.0 {
        return false;
    }
    if !r.is_finite() || !q.is_finite() {
        return false;
    }
    if t == 0.0 {
        return true;
    }
    finite_positive(sigma)
}

/// One CRR rollback pass, optionally capturing the slice-one and slice-two
/// node values used by the tree greeks.
#[allow(clippy::too_many_arguments)]
fn tree(
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
    mut captures: Option<&mut Captures>,
) -> f64 {
    let n = steps as usize;
    let dt = t / n as f64;
    let u = (sigma * dt.sqrt()).exp();
    let d = 1.0 / u;
    let growth = ((r - q) * dt).exp();
    let p = (growth - d) / (u - d);
    let disc = (-r * dt).exp();
    if !p.is_finite() || p <= 0.0 || p >= 1.0 {
        return f64::NAN;
    }

    let mut buffer = [0.0f64; (MAX_STEPS + 1) as usize];
    let v = &mut buffer[..n + 1];
    let ratio = d / u;

    let mut node_spot = s * u.powf(n as f64);
    for slot in v.iter_mut() {
        *slot = intrinsic(kind_call, node_spot, k);
        node_spot *= ratio;
    }

    let mut i = n;
    while i > 0 {
        i -= 1;
        let mut spot = s * u.powf(i as f64);
        for j in 0..=i {
            let continuation = disc * (p * v[j] + (1.0 - p) * v[j + 1]);
            v[j] = if american {
                continuation.max(intrinsic(kind_call, spot, k))
            } else {
                continuation
            };
            spot *= ratio;
        }
        match i {
            1 => {
                if let Some(cap) = captures.as_deref_mut() {
                    cap.one_up = v[0];
                    cap.one_down = v[1];
                }
            }
            2 => {
                if let Some(cap) = captures.as_deref_mut() {
                    cap.two_up = v[0];
                    cap.two_mid = v[1];
                    cap.two_down = v[2];
                }
            }
            _ => {}
        }
    }
    v[0]
}

/// Price, delta, gamma, theta, and vega for one validated input node.
#[allow(clippy::too_many_arguments)]
pub fn price_and_greeks(
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
) -> Greeks {
    if !valid_inputs(s, k, t, r, q, sigma) {
        return nan_greeks();
    }
    if t == 0.0 {
        let payoff = intrinsic(kind_call, s, k);
        return Greeks {
            price: payoff,
            delta: if payoff > 0.0 {
                if kind_call {
                    1.0
                } else {
                    -1.0
                }
            } else {
                0.0
            },
            gamma: 0.0,
            theta: 0.0,
            vega: 0.0,
        };
    }
    if !finite_positive(sigma) || !(2..=MAX_STEPS).contains(&steps) {
        return nan_greeks();
    }

    let mut captures = Captures {
        one_up: 0.0,
        one_down: 0.0,
        two_up: 0.0,
        two_mid: 0.0,
        two_down: 0.0,
    };
    let price_value = tree(
        s,
        k,
        t,
        r,
        q,
        sigma,
        kind_call,
        american,
        steps,
        Some(&mut captures),
    );
    if !price_value.is_finite() {
        return nan_greeks();
    }

    let u = step_up(sigma, t, steps);
    let su = s * u;
    let sd = s / u;
    let su2 = su * u;
    let sd2 = sd / u;
    let dt = t / f64::from(steps);

    let delta = (captures.one_up - captures.one_down) / (su - sd);
    let gamma = ((captures.two_up - captures.two_mid) / (su2 - s)
        - (captures.two_mid - captures.two_down) / (s - sd2))
        / ((su2 - sd2) / 2.0);
    let theta = (captures.two_mid - price_value) / (2.0 * dt);
    let bumped = tree(
        s,
        k,
        t,
        r,
        q,
        sigma + VEGA_BUMP,
        kind_call,
        american,
        steps,
        None,
    );
    let vega = if bumped.is_finite() {
        (bumped - price_value) / VEGA_BUMP
    } else {
        f64::NAN
    };

    Greeks {
        price: price_value,
        delta,
        gamma,
        theta,
        vega,
    }
}

fn nan_greeks() -> Greeks {
    Greeks {
        price: f64::NAN,
        delta: f64::NAN,
        gamma: f64::NAN,
        theta: f64::NAN,
        vega: f64::NAN,
    }
}

/// Price only, for callers that do not need greeks.
#[allow(clippy::too_many_arguments)]
pub fn price(
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
) -> f64 {
    if !valid_inputs(s, k, t, r, q, sigma) {
        return f64::NAN;
    }
    if t == 0.0 {
        return intrinsic(kind_call, s, k);
    }
    if !finite_positive(sigma) || !(2..=MAX_STEPS).contains(&steps) {
        return f64::NAN;
    }
    tree(s, k, t, r, q, sigma, kind_call, american, steps, None)
}

#[derive(Clone, Copy)]
struct TimeTarget {
    s: f64,
    k: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
}

fn time_residual(context: TimeTarget, t: f64) -> f64 {
    tree(
        context.s,
        context.k,
        t,
        context.r,
        context.q,
        context.sigma,
        context.kind_call,
        context.american,
        context.steps,
        None,
    )
}

#[derive(Clone, Copy)]
struct SpotTarget {
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
}

fn spot_residual(context: SpotTarget, s: f64) -> f64 {
    if s <= 0.0 {
        return f64::NAN;
    }
    tree(
        s,
        context.k,
        context.t,
        context.r,
        context.q,
        context.sigma,
        context.kind_call,
        context.american,
        context.steps,
        None,
    )
}

/// Bisect `residual(context, x) - target` for zero within `[lower, upper]`.
/// Returns NaN when the bracket does not sign-change around the target.
fn bisect<C: Copy>(
    context: C,
    residual: fn(C, f64) -> f64,
    target: f64,
    lower: f64,
    upper: f64,
    iterations: u32,
) -> f64 {
    let mut lo = lower;
    let mut hi = upper;
    let mut f_lo = residual(context, lo) - target;
    let f_hi = residual(context, hi) - target;
    if !f_lo.is_finite() || !f_hi.is_finite() {
        return f64::NAN;
    }
    if f_lo == 0.0 {
        return lo;
    }
    if f_hi == 0.0 {
        return hi;
    }
    if (f_lo < 0.0) == (f_hi < 0.0) {
        return f64::NAN;
    }

    let mut iter = 0u32;
    while iter < iterations {
        iter += 1;
        let mid = 0.5 * (lo + hi);
        if mid <= lo || mid >= hi {
            break;
        }
        let f_mid = residual(context, mid) - target;
        if !f_mid.is_finite() {
            return f64::NAN;
        }
        if f_mid == 0.0 {
            return mid;
        }
        if (f_mid < 0.0) == (f_lo < 0.0) {
            lo = mid;
            f_lo = f_mid;
        } else {
            hi = mid;
        }
    }
    0.5 * (lo + hi)
}

/// Solve for time to expiry such that the CRR price equals `target`, bisecting
/// within `[t_lower, t_upper]`. NaN on a failed bracket.
#[allow(clippy::too_many_arguments)]
pub fn implied_time(
    s: f64,
    k: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
    target: f64,
    t_lower: f64,
    t_upper: f64,
    bisect_iterations: u32,
) -> f64 {
    if bisect_iterations < 1 {
        return f64::NAN;
    }
    if !valid_inputs(s, k, t_lower, r, q, sigma) || !valid_inputs(s, k, t_upper, r, q, sigma) {
        return f64::NAN;
    }
    if !finite_positive(sigma) || !(2..=MAX_STEPS).contains(&steps) {
        return f64::NAN;
    }
    if !target.is_finite() || t_upper < t_lower || t_lower == t_upper {
        return f64::NAN;
    }
    bisect(
        TimeTarget {
            s,
            k,
            r,
            q,
            sigma,
            kind_call,
            american,
            steps,
        },
        time_residual,
        target,
        t_lower,
        t_upper,
        bisect_iterations,
    )
}

/// Solve for the underlying spot such that the CRR price equals `target`,
/// bisecting within `[s_lower, s_upper]`. NaN on a failed bracket.
#[allow(clippy::too_many_arguments)]
pub fn implied_underlying(
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
    target: f64,
    s_lower: f64,
    s_upper: f64,
    bisect_iterations: u32,
) -> f64 {
    if bisect_iterations < 1 {
        return f64::NAN;
    }
    if !valid_inputs(s_lower, k, t, r, q, sigma) || !valid_inputs(s_upper, k, t, r, q, sigma) {
        return f64::NAN;
    }
    if !finite_positive(sigma) || !(2..=MAX_STEPS).contains(&steps) {
        return f64::NAN;
    }
    if !target.is_finite() || s_upper < s_lower || s_lower == s_upper {
        return f64::NAN;
    }
    bisect(
        SpotTarget {
            k,
            t,
            r,
            q,
            sigma,
            kind_call,
            american,
            steps,
        },
        spot_residual,
        target,
        s_lower,
        s_upper,
        bisect_iterations,
    )
}

#[cfg(test)]
mod tests {
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
                    * (-0.284496736
                        + tau * (1.421413741 + tau * (-1.453152027 + tau * 1.061405429))));
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
}
