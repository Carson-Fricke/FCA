//! Float64 quantitative-analysis kernels for the native core.
//!
//! The first family is the Cox-Ross-Rubinstein (CRR) binomial option pricer:
//! a pure rollback kernel with tree greeks plus bracketed bisection
//! inversions for implied time to expiry and implied underlying price. These
//! kernels are float64 research math, deliberately distinct from the pinned
//! fixed-point engine decimals. Determinism still holds — every routine is a
//! pure function of its inputs using IEEE-754 operations only, with no clock,
//! no randomness, and no allocation — but results are approximate by
//! construction and converge with the binomial step count.
//!
//! Conventions:
//! - Time to expiry is in years; rates, dividend yield, and volatility are
//!   continuously compounded annuals; theta is returned per year.
//! - The flag byte carries ``call`` in bit 0 and ``american`` in bit 1.
//! - Invalid elements (non-positive spot, strike, or volatility, negative
//!   time, out-of-range step counts, non-finite inputs, failed bracket)
//!   produce NaN instead of an error so a batch never fails as a whole.

const std = @import("std");

/// Largest supported binomial step count, also the stack-buffer capacity.
pub const max_steps: u32 = 1024;

/// Flag bits shared with the C ABI and the Python facade.
pub const flags = struct {
    pub const call: u8 = 1;
    pub const american: u8 = 2;
};

/// Price and tree greeks for one input node.
pub const Greeks = struct {
    price: f64,
    delta: f64,
    gamma: f64,
    /// Per year (multiply by 31_536_000 for per second at the 365-day year).
    theta: f64,
    /// Per unit of volatility (one-sided finite difference at h = 1e-4).
    vega: f64,
};

/// Volatility bump used for the one-sided vega finite difference.
const vega_bump: f64 = 1e-4;

const Captures = struct {
    one_up: f64,
    one_down: f64,
    two_up: f64,
    two_mid: f64,
    two_down: f64,
};

fn finitePositive(value: f64) bool {
    return std.math.isFinite(value) and value > 0;
}

fn intrinsic(kind_call: bool, spot: f64, strike: f64) f64 {
    return if (kind_call) @max(spot - strike, 0) else @max(strike - spot, 0);
}

fn stepUp(sigma: f64, t: f64, steps: u32) f64 {
    const dt = t / @as(f64, @floatFromInt(steps));
    return @exp(sigma * @sqrt(dt));
}

/// Validate raw scalar inputs; returns false when the element must be NaN.
fn validInputs(s: f64, k: f64, t: f64, r: f64, q: f64, sigma: f64) bool {
    if (!finitePositive(s) or !finitePositive(k)) return false;
    if (!std.math.isFinite(t) or t < 0) return false;
    if (!std.math.isFinite(r) or !std.math.isFinite(q)) return false;
    if (t == 0) return true;
    return finitePositive(sigma);
}

/// One CRR rollback pass. When ``captures`` is non-null it receives the
/// slice-one and slice-two node values used by the tree greeks.
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
    captures: ?*Captures,
) f64 {
    const n: usize = steps;
    const dt = t / @as(f64, @floatFromInt(n));
    const u = @exp(sigma * @sqrt(dt));
    const d = 1.0 / u;
    const growth = @exp((r - q) * dt);
    const p = (growth - d) / (u - d);
    const disc = @exp(-r * dt);
    if (!std.math.isFinite(p) or p <= 0 or p >= 1) return std.math.nan(f64);

    var buffer: [max_steps + 1]f64 = undefined;
    const v = buffer[0 .. n + 1];
    const ratio = d / u;

    var node_spot = s * std.math.pow(f64, u, @floatFromInt(n));
    var j: usize = 0;
    while (j <= n) : (j += 1) {
        v[j] = intrinsic(kind_call, node_spot, k);
        node_spot *= ratio;
    }

    var i: usize = n;
    while (i > 0) {
        i -= 1;
        var spot = s * std.math.pow(f64, u, @floatFromInt(i));
        j = 0;
        while (j <= i) : (j += 1) {
            const continuation = disc * (p * v[j] + (1 - p) * v[j + 1]);
            v[j] = if (american)
                @max(continuation, intrinsic(kind_call, spot, k))
            else
                continuation;
            spot *= ratio;
        }
        switch (i) {
            1 => {
                if (captures) |cap| {
                    cap.one_up = v[0];
                    cap.one_down = v[1];
                }
            },
            2 => {
                if (captures) |cap| {
                    cap.two_up = v[0];
                    cap.two_mid = v[1];
                    cap.two_down = v[2];
                }
            },
            else => {},
        }
    }
    return v[0];
}

/// Price, delta, gamma, theta, and vega for one validated input node.
pub fn priceAndGreeks(
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
) Greeks {
    if (!validInputs(s, k, t, r, q, sigma)) return nanGreeks();
    if (t == 0) {
        const payoff = intrinsic(kind_call, s, k);
        return .{
            .price = payoff,
            .delta = if (payoff > 0)
                (if (kind_call) @as(f64, 1) else @as(f64, -1))
            else
                0,
            .gamma = 0,
            .theta = 0,
            .vega = 0,
        };
    }
    if (!finitePositive(sigma) or steps < 2 or steps > max_steps) return nanGreeks();

    var captures: Captures = undefined;
    const price_value = tree(s, k, t, r, q, sigma, kind_call, american, steps, &captures);
    if (!std.math.isFinite(price_value)) return nanGreeks();

    const u = stepUp(sigma, t, steps);
    const su = s * u;
    const sd = s / u;
    const su2 = su * u;
    const sd2 = sd / u;
    const dt = t / @as(f64, @floatFromInt(steps));

    const delta = (captures.one_up - captures.one_down) / (su - sd);
    const gamma =
        ((captures.two_up - captures.two_mid) / (su2 - s) -
            (captures.two_mid - captures.two_down) / (s - sd2)) /
        ((su2 - sd2) / 2);
    const theta = (captures.two_mid - price_value) / (2 * dt);
    const bumped = tree(
        s,
        k,
        t,
        r,
        q,
        sigma + vega_bump,
        kind_call,
        american,
        steps,
        null,
    );
    const vega = if (std.math.isFinite(bumped))
        (bumped - price_value) / vega_bump
    else
        std.math.nan(f64);

    return .{
        .price = price_value,
        .delta = delta,
        .gamma = gamma,
        .theta = theta,
        .vega = vega,
    };
}

fn nanGreeks() Greeks {
    return .{
        .price = std.math.nan(f64),
        .delta = std.math.nan(f64),
        .gamma = std.math.nan(f64),
        .theta = std.math.nan(f64),
        .vega = std.math.nan(f64),
    };
}

/// Price only, for callers that do not need greeks.
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
) f64 {
    if (!validInputs(s, k, t, r, q, sigma)) return std.math.nan(f64);
    if (t == 0) return intrinsic(kind_call, s, k);
    if (!finitePositive(sigma) or steps < 2 or steps > max_steps) {
        return std.math.nan(f64);
    }
    return tree(s, k, t, r, q, sigma, kind_call, american, steps, null);
}

const TimeTarget = struct {
    s: f64,
    k: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
};

fn timeResidual(context: TimeTarget, t: f64) f64 {
    return tree(
        context.s,
        context.k,
        t,
        context.r,
        context.q,
        context.sigma,
        context.kind_call,
        context.american,
        context.steps,
        null,
    );
}

const SpotTarget = struct {
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
    american: bool,
    steps: u32,
};

fn spotResidual(context: SpotTarget, s: f64) f64 {
    if (!(s > 0)) return std.math.nan(f64);
    return tree(
        s,
        context.k,
        context.t,
        context.r,
        context.q,
        context.sigma,
        context.kind_call,
        context.american,
        context.steps,
        null,
    );
}

/// Bisect ``context``-dependent ``residual`` for zero within
/// ``[lower, upper]`` where the residual must approach ``target``. Returns
/// NaN when the bracket does not sign-change around the target.
fn bisect(
    comptime Context: type,
    context: Context,
    comptime residual: fn (Context, f64) f64,
    target: f64,
    lower: f64,
    upper: f64,
    iterations: u32,
) f64 {
    var lo = lower;
    var hi = upper;
    var f_lo = residual(context, lo) - target;
    const f_hi = residual(context, hi) - target;
    if (!std.math.isFinite(f_lo) or !std.math.isFinite(f_hi)) return std.math.nan(f64);
    if (f_lo == 0) return lo;
    if (f_hi == 0) return hi;
    if ((f_lo < 0) == (f_hi < 0)) return std.math.nan(f64);

    var iter: u32 = 0;
    while (iter < iterations) : (iter += 1) {
        const mid = 0.5 * (lo + hi);
        if (mid <= lo or mid >= hi) break;
        const f_mid = residual(context, mid) - target;
        if (!std.math.isFinite(f_mid)) return std.math.nan(f64);
        if (f_mid == 0) return mid;
        if ((f_mid < 0) == (f_lo < 0)) {
            lo = mid;
            f_lo = f_mid;
        } else {
            hi = mid;
        }
    }
    return 0.5 * (lo + hi);
}

/// Solve for time to expiry such that the CRR price equals ``target``,
/// bisecting within ``[t_lower, t_upper]``. NaN on a failed bracket.
pub fn impliedTime(
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
) f64 {
    if (bisect_iterations < 1) return std.math.nan(f64);
    if (!validInputs(s, k, t_lower, r, q, sigma)) return std.math.nan(f64);
    if (!validInputs(s, k, t_upper, r, q, sigma)) return std.math.nan(f64);
    if (!finitePositive(sigma) or steps < 2 or steps > max_steps) {
        return std.math.nan(f64);
    }
    if (!std.math.isFinite(target) or t_upper < t_lower) return std.math.nan(f64);
    if (t_lower == t_upper) return std.math.nan(f64);
    const context = TimeTarget{
        .s = s,
        .k = k,
        .r = r,
        .q = q,
        .sigma = sigma,
        .kind_call = kind_call,
        .american = american,
        .steps = steps,
    };
    return bisect(
        TimeTarget,
        context,
        timeResidual,
        target,
        t_lower,
        t_upper,
        bisect_iterations,
    );
}

/// Solve for the underlying spot such that the CRR price equals ``target``,
/// bisecting within ``[s_lower, s_upper]``. NaN on a failed bracket.
pub fn impliedUnderlying(
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
) f64 {
    if (bisect_iterations < 1) return std.math.nan(f64);
    if (!validInputs(s_lower, k, t, r, q, sigma)) return std.math.nan(f64);
    if (!validInputs(s_upper, k, t, r, q, sigma)) return std.math.nan(f64);
    if (!finitePositive(sigma) or steps < 2 or steps > max_steps) {
        return std.math.nan(f64);
    }
    if (!std.math.isFinite(target) or s_upper < s_lower) return std.math.nan(f64);
    if (s_lower == s_upper) return std.math.nan(f64);
    const context = SpotTarget{
        .k = k,
        .t = t,
        .r = r,
        .q = q,
        .sigma = sigma,
        .kind_call = kind_call,
        .american = american,
        .steps = steps,
    };
    return bisect(
        SpotTarget,
        context,
        spotResidual,
        target,
        s_lower,
        s_upper,
        bisect_iterations,
    );
}

test "crr european options converge to black-scholes" {
    const cases = [_]struct { s: f64, k: f64, sigma: f64, kind_call: bool }{
        .{ .s = 450, .k = 450, .sigma = 0.15, .kind_call = true },
        .{ .s = 455, .k = 445, .sigma = 0.32, .kind_call = false },
        .{ .s = 600, .k = 590, .sigma = 0.08, .kind_call = true },
    };
    for (cases) |case| {
        const reference = blackScholes(
            case.s,
            case.k,
            2.0 / 365.0,
            0.05,
            0.012,
            case.sigma,
            case.kind_call,
        );
        const value = price(
            case.s,
            case.k,
            2.0 / 365.0,
            0.05,
            0.012,
            case.sigma,
            case.kind_call,
            false,
            512,
        );
        try std.testing.expect(@abs(value - reference) < 2e-3);
    }
}

test "european put-call parity holds through the tree" {
    const s = 470.0;
    const k = 465.0;
    const t = 3.0 / 365.0;
    const r = 0.045;
    const q = 0.013;
    const sigma = 0.21;
    const call = price(s, k, t, r, q, sigma, true, false, 400);
    const put = price(s, k, t, r, q, sigma, false, false, 400);
    const parity = s * @exp(-q * t) - k * @exp(-r * t);
    try std.testing.expectApproxEqAbs(parity, call - put, 1e-8);
}

test "american put is worth at least the european put" {
    const american = price(90.0, 100.0, 30.0 / 365.0, 0.05, 0.0, 0.25, false, true, 256);
    const european = price(90.0, 100.0, 30.0 / 365.0, 0.05, 0.0, 0.25, false, false, 256);
    try std.testing.expect(american >= european);
    try std.testing.expect(american >= 100.0 - 90.0);
}

test "at-the-money theta is negative and greeks stay in bounds" {
    const greeks = priceAndGreeks(500.0, 500.0, 1.0 / 365.0, 0.04, 0.0, 0.30, true, true, 256);
    try std.testing.expect(greeks.theta < 0);
    try std.testing.expect(greeks.delta > 0 and greeks.delta < 1);
    try std.testing.expect(greeks.gamma > 0);
    try std.testing.expect(greeks.vega > 0);
    try std.testing.expectApproxEqAbs(
        blackScholes(500.0, 500.0, 1.0 / 365.0, 0.04, 0.0, 0.30, true),
        greeks.price,
        5e-3,
    );
}

test "zero expiry returns the intrinsic value" {
    const call = price(105.0, 100.0, 0, 0.04, 0.0, 0.20, true, true, 128);
    const put = price(95.0, 100.0, 0, 0.04, 0.0, 0.20, false, true, 128);
    try std.testing.expectEqual(@as(f64, 5.0), call);
    try std.testing.expectEqual(@as(f64, 5.0), put);
}

test "invalid inputs produce NaN instead of errors" {
    const zero_vol = price(100.0, 100.0, 0.01, 0.04, 0.0, 0.0, true, false, 64);
    try std.testing.expect(std.math.isNan(zero_vol));
    const few_steps = price(100.0, 100.0, 0.01, 0.04, 0.0, 0.20, true, false, 1);
    try std.testing.expect(std.math.isNan(few_steps));
    const many_steps = price(
        100.0,
        100.0,
        0.01,
        0.04,
        0.0,
        0.20,
        true,
        false,
        max_steps + 1,
    );
    try std.testing.expect(std.math.isNan(many_steps));
    const negative_spot = price(-100.0, 100.0, 0.01, 0.04, 0.0, 0.20, true, false, 64);
    try std.testing.expect(std.math.isNan(negative_spot));
}

test "implied time round-trips through the pricer" {
    const target_t = 4.0 / (365.0 * 24.0);
    const target_price = price(500.0, 502.0, target_t, 0.04, 0.0, 0.35, true, true, 96);
    const solved = impliedTime(
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
    try std.testing.expect(std.math.isFinite(solved));
    try std.testing.expectApproxEqAbs(target_t, solved, 1e-9);
}

test "implied time without a sign-changing bracket is NaN" {
    const solved = impliedTime(
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
    try std.testing.expect(std.math.isNan(solved));
}

test "implied underlying round-trips through the pricer" {
    const target_s = 503.75;
    const target_price = price(target_s, 500.0, 1.0 / 48.0, 0.04, 0.0, 0.30, true, true, 96);
    const solved = impliedUnderlying(
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
    try std.testing.expect(std.math.isFinite(solved));
    try std.testing.expectApproxEqAbs(target_s, solved, 1e-6);
}

fn blackScholes(
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    sigma: f64,
    kind_call: bool,
) f64 {
    const root_t = @sqrt(t);
    const d1 = (@log(s / k) + (r - q + sigma * sigma / 2) * t) / (sigma * root_t);
    const d2 = d1 - sigma * root_t;
    if (kind_call) {
        return s * @exp(-q * t) * normCdf(d1) - k * @exp(-r * t) * normCdf(d2);
    }
    return k * @exp(-r * t) * normCdf(-d2) - s * @exp(-q * t) * normCdf(-d1);
}

fn normCdf(x: f64) f64 {
    // Abramowitz-Stegun 7.1.26 for erf (|error| < 1.5e-7), ample for the
    // Black-Scholes test reference comparisons at 1e-3 tolerance.
    const ax = @abs(x) / std.math.sqrt(2.0);
    const tau = 1.0 / (1.0 + 0.3275911 * ax);
    const poly = tau * (0.254829592 + tau * (-0.284496736 + tau * (1.421413741 +
        tau * (-1.453152027 + tau * 1.061405429))));
    const erf = 1.0 - poly * @exp(-ax * ax);
    return 0.5 * (1.0 + if (x < 0) -erf else erf);
}
