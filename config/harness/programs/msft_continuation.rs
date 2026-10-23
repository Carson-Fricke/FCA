// Corrected partial-fill accounting for the retained MSFT continuation research.
// MSFT intraday CONTINUATION policy (branch execution-9304e53b27ce4d8cb47e0144200653b7).
// Daily trend/momentum context + daily SDK-wavelet coarse-energy-share regime gate,
// 15-min pullback/breakout/naive trigger, in-session exits, after-cost accounting.
use harness_strategy_sdk::serde_json::Value;
use harness_strategy_sdk::building_blocks::{FillLedger, PendingOrder, cash_return_bps};
use harness_strategy_sdk::{
    export_strategy_program, BarFrame, Decimal, Fill, MarketConsumer, MetricPublisher, OrderSink,
    OrderType, ProgramStart, Side, StrategyProgram, StudyOutput, SubmittedOrder, Subscription,
    TimeInForce, WaveletIndicator,
};
use std::collections::VecDeque;

const SESSION_GAP_NS: i64 = 4 * 3600 * 1_000_000_000;

fn p_f64(v: &Value, k: &str, d: f64) -> f64 {
    v.get(k).and_then(Value::as_f64).unwrap_or(d)
}
fn p_usize(v: &Value, k: &str, d: usize) -> usize {
    v.get(k).and_then(Value::as_u64).map(|x| x as usize).unwrap_or(d)
}
fn p_i64(v: &Value, k: &str, d: i64) -> i64 {
    v.get(k).and_then(Value::as_i64).unwrap_or(d)
}
fn p_bool(v: &Value, k: &str, d: bool) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(d)
}
fn p_str(v: &Value, k: &str, d: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or(d).to_string()
}

// Linear-interpolated percentile of a sample.
fn pctile(vals: &[f64], q: f64) -> f64 {
    if vals.is_empty() {
        return f64::NAN;
    }
    let mut s: Vec<f64> = vals.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = s.len();
    if n == 1 {
        return s[0];
    }
    let qq = q.clamp(0.0, 100.0) / 100.0;
    let pos = qq * (n as f64 - 1.0);
    let lo = pos.floor() as usize;
    let hi = (lo + 1).min(n - 1);
    let frac = pos - lo as f64;
    s[lo] + frac * (s[hi] - s[lo])
}

fn r2_of(y: &[f64]) -> f64 {
    let n = y.len() as f64;
    if n < 2.0 {
        return 0.0;
    }
    let xs: Vec<f64> = (0..y.len()).map(|i| i as f64).collect();
    let mx = xs.iter().sum::<f64>() / n;
    let my = y.iter().sum::<f64>() / n;
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for i in 0..y.len() {
        let dx = xs[i] - mx;
        let dy = y[i] - my;
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
    }
    if sxx <= 0.0 || syy <= 0.0 {
        return 0.0;
    }
    (sxy * sxy) / (sxx * syy)
}

fn mean_of(v: &[f64]) -> f64 {
    if v.is_empty() {
        0.0
    } else {
        v.iter().sum::<f64>() / v.len() as f64
    }
}

#[derive(Clone)]
struct DailyState {
    available_at_ns: i64,
    dir: i8,
    momentum_bps: f64,
    share: f64,
    ok: bool,
}

struct Open {
    side: i8,
    qty: f64,
    entry_price: f64,
    entry_quantity: Decimal,
    entry_price_exact: Decimal,
    entry_time_ns: i64,
    entry_atr: f64,
    bars_held: i32,
    session_id: i64,
}

struct Episode {
    entry_time_ns: i64,
    exit_time_ns: i64,
    side: i8,
    gross_bps: f64,
    actual_net_bps: f64,
    bars_held: i32,
    session_id: i64,
    reason: String,
}

#[derive(Clone, Copy)]
enum Pending {
    Entry,
    Exit,
}

struct MsftCont {
    mom_lookback: usize,
    mom_strength: String,
    floor_bps: f64,
    r2_floor: f64,
    floor_sigma: f64,
    gate_mode: String,
    gate_q: f64,
    gate_min_hist: usize,
    rw_anchor: f64,
    trigger: String,
    ema_fast: usize,
    ema_alpha: f64,
    pullback_p: usize,
    breakout_b: usize,
    confirm: bool,
    q_intra: f64,
    intra_window: usize,
    atr_period: usize,
    atr_stop_mult: f64,
    time_stop_bars: i32,
    no_entry_last_k: i32,
    session_bars: i32,
    flatten_bar: i32,
    target_notional: f64,

    wav_daily: WaveletIndicator,
    wav_intra: WaveletIndicator,

    daily_closes: VecDeque<f64>,
    shares: VecDeque<f64>,
    daily_state: Option<DailyState>,

    ema: Option<f64>,
    atr_prev_close: Option<f64>,
    atr_win: VecDeque<f64>,
    atr_value: Option<f64>,
    hist_close: VecDeque<f64>,
    hist_high: VecDeque<f64>,
    hist_low: VecDeque<f64>,
    intra_shares: VecDeque<f64>,

    last_start_ns: Option<i64>,
    bars_into_session: i32,
    session_id: i64,
    entered_this_session: bool,

    pos: Option<Open>,
    pending: Option<Pending>,
    pending_order: Option<PendingOrder>,
    ledger: FillLedger,
    episode_cash: Decimal,
    episode_fees: Decimal,
    exit_reason_pending: String,

    order_seq: u64,
    episodes: Vec<Episode>,
    total_sessions: i64,
    overnight_violations: i64,
}

impl MsftCont {
    fn submit(&mut self, side: i8, qty: f64, ctx: &mut OrderSink) -> bool {
        let seq = self.order_seq;
        self.order_seq += 1;
        let shares = if self.pos.is_some() { self.ledger.quantity().abs() } else { Decimal::from_i128(qty.round().max(1.0) as i128) };
        let order = SubmittedOrder {
            order_id: format!("msftcv-{seq}"),
            symbol: "MSFT".to_string(),
            side: if side > 0 { Side::Buy } else { Side::Sell },
            quantity: shares,
            order_type: OrderType::Market,
            time_in_force: TimeInForce::Day,
            limit_price: None,
            stop_price: None,
            sequence: seq,
            event_id: format!("msftcv-evt-{seq}"),
        };
        let Ok(pending) = PendingOrder::new(&order.order_id, shares) else { return false; };
        if ctx.submit(order).is_err() { return false; }
        self.pending_order = Some(pending);
        true
    }

    fn on_daily(&mut self, frame: &BarFrame) {
        let bar = match frame.bars_by_symbol.get("MSFT") {
            Some(b) => *b,
            None => return,
        };
        let close = bar.close.to_f64();
        if !close.is_finite() || close <= 0.0 {
            return;
        }
        self.daily_closes.push_back(close);
        if self.daily_closes.len() > 260 {
            self.daily_closes.pop_front();
        }
        let n = self.mom_lookback;
        let l = self.daily_closes.len();
        if l < n + 1 {
            return;
        }
        let last = self.daily_closes[l - 1];
        let past = self.daily_closes[l - 1 - n];
        if past <= 0.0 {
            return;
        }
        let mom = 10000.0 * (last / past).ln();
        let strength_ok = match self.mom_strength.as_str() {
            "slope" => (mom / n as f64).abs() >= self.floor_bps,
            "vol_normalized" => {
                let (mut sum, mut sumsq, mut cnt) = (0.0, 0.0, 0.0);
                for i in (l - n)..l {
                    let c1 = self.daily_closes[i];
                    let c0 = self.daily_closes[i - 1];
                    if c0 > 0.0 && c1 > 0.0 {
                        let r = 10000.0 * (c1 / c0).ln();
                        sum += r;
                        sumsq += r * r;
                        cnt += 1.0;
                    }
                }
                if cnt < 2.0 {
                    return;
                }
                let m = sum / cnt;
                let var = (sumsq / cnt - m * m).max(0.0);
                let vol = var.sqrt();
                if vol <= 0.0 {
                    return;
                }
                (mom / vol).abs() >= self.floor_sigma
            }
            "r2" => {
                let ys: Vec<f64> = (0..=n).map(|k| self.daily_closes[l - 1 - k].ln()).collect();
                r2_of(&ys) >= self.r2_floor
            }
            _ => mom.abs() >= self.floor_bps,
        };
        let dir: i8 = if mom > 0.0 {
            1
        } else if mom < 0.0 {
            -1
        } else {
            0
        };
        let mut share = f64::NAN;
        let mut have_share = false;
        if let Some(s) = self.wav_daily.snapshot_at("MSFT", frame.available_at_ns) {
            let sh = s.coarse_energy_share().to_f64();
            if sh.is_finite() {
                share = sh;
                have_share = true;
                self.shares.push_back(sh);
                if self.shares.len() > 512 {
                    self.shares.pop_front();
                }
            }
        }
        let gate_ok = if self.gate_mode == "off" {
            true
        } else if !have_share {
            false
        } else if self.gate_mode == "rw_anchor" {
            share >= self.rw_anchor - 1e-12
        } else {
            let l2 = self.shares.len();
            if l2 < self.gate_min_hist {
                false
            } else {
                let v: Vec<f64> = self.shares.iter().copied().collect();
                share >= pctile(&v, self.gate_q)
            }
        };
        let ok = gate_ok && strength_ok && dir != 0;
        self.daily_state = Some(DailyState {
            available_at_ns: frame.available_at_ns,
            dir,
            momentum_bps: mom,
            share,
            ok,
        });
    }

    fn check_trigger(&self, dir: i8, close: f64, ema: f64) -> bool {
        match self.trigger.as_str() {
            "naive" => true,
            "breakout" => {
                if self.hist_close.len() < self.breakout_b {
                    return false;
                }
                if dir > 0 {
                    let mut mx = f64::MIN;
                    for j in 0..self.breakout_b {
                        mx = mx.max(self.hist_high[self.hist_high.len() - 1 - j]);
                    }
                    close > mx
                } else {
                    let mut mn = f64::MAX;
                    for j in 0..self.breakout_b {
                        mn = mn.min(self.hist_low[self.hist_low.len() - 1 - j]);
                    }
                    close < mn
                }
            }
            _ => {
                if self.hist_close.len() < self.pullback_p {
                    return false;
                }
                let mut pulled = false;
                for j in 0..self.pullback_p {
                    let c = self.hist_close[self.hist_close.len() - 1 - j];
                    if dir > 0 && c < ema {
                        pulled = true;
                    }
                    if dir < 0 && c > ema {
                        pulled = true;
                    }
                }
                if !pulled {
                    return false;
                }
                if dir > 0 {
                    close > ema
                } else {
                    close < ema
                }
            }
        }
    }

    fn check_confirm(&self, dir: i8, frame: &BarFrame) -> bool {
        if self.bars_into_session < self.intra_window as i32 {
            return false;
        }
        let s = match self.wav_intra.snapshot_at("MSFT", frame.available_at_ns) {
            Some(s) => s,
            None => return false,
        };
        let sh = s.coarse_energy_share().to_f64();
        let tr = s.trend_bps().to_f64();
        if self.intra_shares.len() < self.intra_window {
            return false;
        }
        let v: Vec<f64> = self.intra_shares.iter().copied().collect();
        let thr = pctile(&v, self.q_intra);
        let sign_ok = (tr > 0.0 && dir > 0) || (tr < 0.0 && dir < 0);
        sh >= thr && sign_ok
    }

    fn on_intraday(&mut self, frame: &BarFrame, ctx: &mut OrderSink) {
        let bar = match frame.bars_by_symbol.get("MSFT") {
            Some(b) => *b,
            None => return,
        };
        let close = bar.close.to_f64();
        let high = bar.high.to_f64();
        let low = bar.low.to_f64();
        if !(close.is_finite() && high.is_finite() && low.is_finite()) {
            return;
        }
        let start = frame.interval_start_ns;
        let mut new_session = false;
        match self.last_start_ns {
            None => {
                self.session_id = 1;
                self.bars_into_session = 0;
            }
            Some(ls) => {
                if start - ls > SESSION_GAP_NS {
                    self.session_id += 1;
                    self.bars_into_session = 0;
                    self.entered_this_session = false;
                    new_session = true;
                }
            }
        }
        self.last_start_ns = Some(start);
        self.bars_into_session += 1;
        if new_session {
            self.total_sessions += 1;
        }

        // Forced in-session flatten: a new session must never inherit a position.
        if new_session {
            if !self.ledger.quantity().is_zero() { self.overnight_violations += 1; }
            if let Some(o) = self.pos.as_ref() {
                if self.pending.is_none() {
                    let (side, qty) = (o.side, o.qty);
                    if self.submit(-side, qty, ctx) {
                        self.pending = Some(Pending::Exit);
                        self.exit_reason_pending = "session".to_string();
                    }
                }
            }
        }

        // Update stateful intraday indicators.
        self.ema = Some(match self.ema {
            None => close,
            Some(p) => self.ema_alpha * close + (1.0 - self.ema_alpha) * p,
        });
        let tr = match self.atr_prev_close {
            Some(pc) => (high - low).max((high - pc).abs()).max((low - pc).abs()),
            None => high - low,
        };
        self.atr_win.push_back(tr);
        while self.atr_win.len() > self.atr_period {
            self.atr_win.pop_front();
        }
        self.atr_value = if self.atr_win.len() == self.atr_period {
            Some(self.atr_win.iter().sum::<f64>() / self.atr_period as f64)
        } else {
            None
        };
        self.atr_prev_close = Some(close);

        // Manage an open position.
        if self.pending.is_none() {
            if let Some(o) = self.pos.as_mut() {
                o.bars_held += 1;
            }
            if let Some(o) = self.pos.as_ref() {
                let (side, entry_price, entry_atr, bars_held, qty) =
                    (o.side, o.entry_price, o.entry_atr, o.bars_held, o.qty);
                let mut reason: Option<&str> = None;
                if self.time_stop_bars > 0 && bars_held >= self.time_stop_bars {
                    reason = Some("time");
                } else if self.atr_stop_mult > 0.0 && entry_atr > 0.0 {
                    if side > 0 && close < entry_price - self.atr_stop_mult * entry_atr {
                        reason = Some("stop");
                    }
                    if side < 0 && close > entry_price + self.atr_stop_mult * entry_atr {
                        reason = Some("stop");
                    }
                }
                if reason.is_none() && self.bars_into_session >= self.flatten_bar {
                    reason = Some("flatten");
                }
                if let Some(r) = reason {
                    if self.submit(-side, qty, ctx) {
                        self.pending = Some(Pending::Exit);
                        self.exit_reason_pending = r.to_string();
                    }
                }
            }
        }

        // Consider a new entry.
        if self.pos.is_none() && self.pending.is_none() && !self.entered_this_session {
            if let Some(ds) = self.daily_state.clone() {
                if ds.available_at_ns <= frame.available_at_ns
                    && ds.ok
                    && ds.dir != 0
                    && self.bars_into_session <= self.session_bars - self.no_entry_last_k
                {
                    let ema = self.ema.unwrap_or(close);
                    let trig = self.check_trigger(ds.dir, close, ema);
                    let conf_ok = if self.confirm {
                        self.check_confirm(ds.dir, frame)
                    } else {
                        true
                    };
                    if trig && conf_ok {
                        let qty = (self.target_notional / close).round().max(1.0);
                        if self.submit(ds.dir, qty, ctx) {
                            self.pending = Some(Pending::Entry);
                        }
                    }
                }
            }
        }

        // Advance trailing histories (their content always excludes the current bar).
        self.hist_close.push_back(close);
        self.hist_high.push_back(high);
        self.hist_low.push_back(low);
        while self.hist_close.len() > 64 {
            self.hist_close.pop_front();
            self.hist_high.pop_front();
            self.hist_low.pop_front();
        }
        if self.confirm {
            if let Some(s) = self.wav_intra.snapshot_at("MSFT", frame.available_at_ns) {
                let sh = s.coarse_energy_share().to_f64();
                if sh.is_finite() {
                    self.intra_shares.push_back(sh);
                    if self.intra_shares.len() > 256 {
                        self.intra_shares.pop_front();
                    }
                }
            }
        }
    }
}

impl MarketConsumer for MsftCont {
    type Context = OrderSink;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![
            Subscription::Bars {
                symbols: vec!["MSFT".to_string()],
                interval: "1d".to_string(),
            },
            Subscription::Bars {
                symbols: vec!["MSFT".to_string()],
                interval: "15min".to_string(),
            },
        ]
    }
    fn indicators(&mut self) -> Vec<&mut dyn harness_strategy_sdk::Indicator> {
        vec![&mut self.wav_daily, &mut self.wav_intra]
    }
    fn on_bar(&mut self, frame: &BarFrame, ctx: &mut OrderSink) {
        match frame.interval {
            "1d" => self.on_daily(frame),
            "15min" => self.on_intraday(frame, ctx),
            _ => {}
        }
    }
}

impl StrategyProgram for MsftCont {
    fn new(parameters: Value) -> Self {
        let mom_lookback = p_usize(&parameters, "mom_lookback", 10).clamp(2, 120);
        let mom_strength = p_str(&parameters, "mom_strength", "raw_log");
        let floor_bps = p_f64(&parameters, "floor_bps", 0.0);
        let r2_floor = p_f64(&parameters, "r2_floor", 0.0);
        let floor_sigma = p_f64(&parameters, "floor_sigma", 0.0);
        let gate_mode = p_str(&parameters, "gate_mode", "rolling_pct");
        let gate_q = p_f64(&parameters, "gate_q", 60.0).clamp(0.0, 100.0);
        let gate_min_hist = p_usize(&parameters, "gate_min_hist", 20).max(2);
        let mut window = p_usize(&parameters, "window", 32).clamp(2, 4096);
        window = window.next_power_of_two().min(4096);
        let mut coarse_support = p_usize(&parameters, "coarse_support", 8).max(2);
        coarse_support = coarse_support.next_power_of_two().min(window);
        if coarse_support < 2 {
            coarse_support = 2;
        }
        let rw_anchor = rw_anchor(window, coarse_support);
        let trigger = p_str(&parameters, "trigger", "pullback");
        let ema_fast = p_usize(&parameters, "ema_fast", 8).max(1);
        let pullback_p = p_usize(&parameters, "pullback_p", 2).max(1);
        let breakout_b = p_usize(&parameters, "breakout_b", 8).max(1);
        let confirm = p_bool(&parameters, "confirm", false);
        let q_intra = p_f64(&parameters, "q_intra", 60.0).clamp(0.0, 100.0);
        let mut intra_window = p_usize(&parameters, "intra_window", 16).clamp(2, 4096);
        intra_window = intra_window.next_power_of_two().min(4096);
        let atr_period = p_usize(&parameters, "atr_period", 14).max(1);
        let atr_stop_mult = p_f64(&parameters, "atr_stop_mult", 0.0).max(0.0);
        let time_stop_bars = p_i64(&parameters, "time_stop_bars", 0) as i32;
        let no_entry_last_k = p_i64(&parameters, "no_entry_last_k", 3) as i32;
        let session_bars = p_i64(&parameters, "session_bars", 26) as i32;
        let flatten_bar = p_i64(&parameters, "flatten_bar", 25) as i32;
        let target_notional = p_f64(&parameters, "target_notional", 45000.0).max(1000.0);

        let intra_cs = 8usize.min(intra_window).next_power_of_two().min(intra_window);

        let wav_daily = WaveletIndicator::new(
            vec!["MSFT".to_string()],
            "1d".to_string(),
            window,
            coarse_support,
        )
        .unwrap_or_else(|_| {
            WaveletIndicator::new(vec!["MSFT".to_string()], "1d".to_string(), 32, 8).unwrap()
        });
        let wav_intra = WaveletIndicator::new(
            vec!["MSFT".to_string()],
            "15min".to_string(),
            intra_window,
            intra_cs,
        )
        .unwrap_or_else(|_| {
            WaveletIndicator::new(vec!["MSFT".to_string()], "15min".to_string(), 16, 8).unwrap()
        });

        Self {
            mom_lookback,
            mom_strength,
            floor_bps,
            r2_floor,
            floor_sigma,
            gate_mode,
            gate_q,
            gate_min_hist,
            rw_anchor,
            trigger,
            ema_fast,
            ema_alpha: 2.0 / (ema_fast as f64 + 1.0),
            pullback_p,
            breakout_b,
            confirm,
            q_intra,
            intra_window,
            atr_period,
            atr_stop_mult,
            time_stop_bars,
            no_entry_last_k,
            session_bars,
            flatten_bar,
            target_notional,
            wav_daily,
            wav_intra,
            daily_closes: VecDeque::new(),
            shares: VecDeque::new(),
            daily_state: None,
            ema: None,
            atr_prev_close: None,
            atr_win: VecDeque::new(),
            atr_value: None,
            hist_close: VecDeque::new(),
            hist_high: VecDeque::new(),
            hist_low: VecDeque::new(),
            intra_shares: VecDeque::new(),
            last_start_ns: None,
            bars_into_session: 0,
            session_id: 0,
            entered_this_session: false,
            pos: None,
            pending: None,
            pending_order: None,
            ledger: FillLedger::new("MSFT"),
            episode_cash: Decimal::ZERO,
            episode_fees: Decimal::ZERO,
            exit_reason_pending: String::new(),
            order_seq: 0,
            episodes: Vec::new(),
            total_sessions: 0,
            overnight_violations: 0,
        }
    }

    fn on_start(&mut self, _start: &ProgramStart) {}

    fn on_fill(&mut self, fill: &Fill) {
        let before_cash = self.ledger.cash_flow();
        let before_fees = self.ledger.fees();
        if !self.ledger.apply(fill).expect("valid exact booked fill") { return; }
        let Some(order) = self.pending_order.as_ref() else { return; };
        if order.order_id() != fill.order_id { return; }
        match self.pending {
            Some(Pending::Entry) => {
                if self.pos.is_none() {
                    self.episode_cash = before_cash;
                    self.episode_fees = before_fees;
                    self.pos = Some(Open {
                        side: if fill.side == "buy" { 1 } else { -1 },
                        qty: 0.0, entry_price: 0.0, entry_quantity: Decimal::ZERO,
                        entry_price_exact: Decimal::ZERO, entry_time_ns: fill.timestamp_ns,
                        entry_atr: self.atr_value.unwrap_or(0.0), bars_held: 0,
                        session_id: self.session_id,
                    });
                }
                let position = self.pos.as_mut().expect("open entry");
                position.qty = self.ledger.quantity().abs().to_f64();
                position.entry_quantity = self.ledger.quantity().abs();
                position.entry_price_exact = self.ledger.average_fill_price(fill.order_id)
                    .expect("filled order has weighted price");
                position.entry_price = position.entry_price_exact.to_f64();
                self.entered_this_session = true;
                if order.is_filled(&self.ledger) {
                    self.pending = None;
                    self.pending_order = None;
                }
            }
            Some(Pending::Exit) => {
                // A partial exit leaves both the position and order live.
                if !order.is_filled(&self.ledger) || !self.ledger.quantity().is_zero() { return; }
                if let Some(position) = self.pos.take() {
                    let gross = self.ledger.cash_flow() - self.episode_cash;
                    let actual_fees = self.ledger.fees() - self.episode_fees;
                    let quantity = position.entry_quantity;
                    let price = position.entry_price_exact;
                    self.episodes.push(Episode {
                        entry_time_ns: position.entry_time_ns, exit_time_ns: fill.timestamp_ns,
                        side: position.side,
                        gross_bps: cash_return_bps(gross, quantity, price).expect("entry notional"),
                        actual_net_bps: cash_return_bps(gross - actual_fees, quantity, price).expect("entry notional"),
                        bars_held: position.bars_held, session_id: position.session_id,
                        reason: self.exit_reason_pending.clone(),
                    });
                }
                self.pending = None;
                self.pending_order = None;
            }
            None => {}
        }
    }

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("actual_position_quantity", self.ledger.quantity().to_f64());
        let _ = metrics.publish("actual_fee_total", self.ledger.fees().to_f64());
        let _ = metrics.publish("pending_orders", f64::from(self.pending_order.is_some()));
        let _ = metrics.publish("actual_net_mean_bps", mean_of(&self.episodes.iter().map(|e| e.actual_net_bps).collect::<Vec<_>>()));
        let eps = &self.episodes;
        let count = eps.len();
        let gross: Vec<f64> = eps.iter().map(|e| e.gross_bps).collect();
        let _ = metrics.publish("episodes", count as f64);
        let _ = metrics.publish("gross_mean_bps", mean_of(&gross));
        for (name, rt) in [
            ("net_mean_rt0", 0.0_f64),
            ("net_mean_rt1", 1.0),
            ("net_mean_rt2", 2.0),
            ("net_mean_rt4", 4.0),
            ("net_mean_rt10", 10.0),
        ] {
            let net: Vec<f64> = eps.iter().map(|e| e.gross_bps - rt).collect();
            let _ = metrics.publish(name, mean_of(&net));
        }
        let net4: Vec<f64> = eps.iter().map(|e| e.gross_bps - 4.0).collect();
        let wins = net4.iter().filter(|x| **x > 0.0).count();
        let _ = metrics.publish(
            "win_rate_rt4",
            if count > 0 { wins as f64 / count as f64 } else { 0.0 },
        );
        let mut sorted = gross.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let med = if count > 0 { sorted[count / 2] } else { 0.0 };
        let _ = metrics.publish("gross_median_bps", med);
        let mut sess: Vec<i64> = eps.iter().map(|e| e.session_id).collect();
        sess.sort();
        sess.dedup();
        let _ = metrics.publish("distinct_entry_sessions", sess.len() as f64);
        let held: Vec<f64> = eps.iter().map(|e| e.bars_held as f64).collect();
        let _ = metrics.publish("mean_bars_held", mean_of(&held));
        let nf = |r: &str| eps.iter().filter(|e| e.reason == r).count();
        let d = if count > 0 { count as f64 } else { 1.0 };
        let _ = metrics.publish("frac_exit_time", nf("time") as f64 / d);
        let _ = metrics.publish("frac_exit_stop", nf("stop") as f64 / d);
        let _ = metrics.publish("frac_exit_flatten", nf("flatten") as f64 / d);
        let _ = metrics.publish("frac_exit_session", nf("session") as f64 / d);
        if count > 0 {
            let mut times: Vec<i64> = eps.iter().map(|e| e.entry_time_ns).collect();
            times.sort();
            let mid = times[count / 2];
            let h1: Vec<f64> = eps
                .iter()
                .filter(|e| e.entry_time_ns < mid)
                .map(|e| e.gross_bps - 4.0)
                .collect();
            let h2: Vec<f64> = eps
                .iter()
                .filter(|e| e.entry_time_ns >= mid)
                .map(|e| e.gross_bps - 4.0)
                .collect();
            let _ = metrics.publish("net_mean_rt4_h1", mean_of(&h1));
            let _ = metrics.publish("net_mean_rt4_h2", mean_of(&h2));
            let _ = metrics.publish("n_h1", h1.len() as f64);
            let _ = metrics.publish("n_h2", h2.len() as f64);
        }
        let _ = metrics.publish("total_sessions", self.total_sessions as f64);
        let _ = metrics.publish("overnight_violations", self.overnight_violations as f64);
        let mut rows = Vec::new();
        for e in eps {
            rows.push(vec![
                Value::from(e.entry_time_ns),
                Value::from(e.exit_time_ns),
                Value::from(e.side as i64),
                Value::from(e.gross_bps),
                Value::from(e.bars_held as i64),
                Value::from(e.session_id),
                Value::from(e.reason.clone()),
            ]);
        }
        let _ = metrics.publish_output(
            "episodes",
            &StudyOutput::Table {
                columns: vec![
                    "entry_ns".to_string(),
                    "exit_ns".to_string(),
                    "side".to_string(),
                    "gross_bps".to_string(),
                    "bars_held".to_string(),
                    "session_id".to_string(),
                    "reason".to_string(),
                ],
                rows,
            },
        );
    }
}

fn rw_anchor(window: usize, coarse_support: usize) -> f64 {
    let mut support = 2usize;
    let mut total = 0.0f64;
    let mut coarse = 0.0f64;
    while support <= window {
        let energy = (support as f64) - 1.0;
        total += energy;
        if support >= coarse_support {
            coarse += energy;
        }
        support *= 2;
    }
    if total > 0.0 {
        coarse / total
    } else {
        0.0
    }
}

export_strategy_program!(MsftCont);
