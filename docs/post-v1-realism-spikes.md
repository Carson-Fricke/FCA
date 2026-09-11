# Post-v1 Realism Spikes

Status: research record for T131. These are timeboxed spikes that scope the
highest-value realism gaps between the v1 harness and live trading. Each spike
states its scope, the prototype or repository evidence, a cost-benefit
analysis, and a recommendation. Prototypes are deterministic and offline.

## How to reproduce the evidence

```powershell
# Split divergence prototype (T131 AC2)
uv run --no-sync python scripts/realism/corporate_actions_adjusted_vs_unadjusted.py
uv run --no-sync pytest scripts/realism/corporate_actions_adjusted_vs_unadjusted.py
```

The prototype uses only embedded, rounded vendor closes and exact `Decimal`
arithmetic, so it touches no network, clock, or entropy.

## Summary

| Spike | Gap | Effort | Priority | Recommendation |
| --- | --- | --- | --- | --- |
| S1 Corporate actions and price adjustment | Splits and dividends are invisible to bars, returns, indicators, and positions | 3-5 eng-days | P1 | Adopt split adjustment and split/position continuity for v1.x |
| S2 Borrow, short availability, and financing | Equity shorts are unconstrained and free | 5-8 eng-days | P2 | Spike the port now; implement availability + financing later |
| S3 Partial fills, displayed depth, and latency | Latency is metadata and cancel/replace is not wired | 3-5 eng-days | P1 | Wire latency scheduling and cancel semantics; add depth |
| S4 Slippage and market impact | Fills occur at the touch with no impact model | 3-5 eng-days | P1 | Add deterministic spread + participation impact |

## S1. Corporate actions and price adjustment

**Scope.** The v1 engine ingests raw, as-traded prices and models only two
listing lifecycle facts: terminal delistings and renames. Splits and cash
dividends are not represented anywhere in the data, engine, or strategy
contract. As a result a split is silently interpreted as a catastrophic one-day
return, and a held position appears to lose the split factor overnight.

**Evidence.**

- The corporate-action vocabulary is exactly `DELISTING` and `RENAME`
  (`src/harness/data/listing_events.py:42`). There is no split or dividend kind.
- The lake probe pins `adjustment` to `"raw"` and rejects anything else
  (`src/lake/tools/delisted_probe.py:55`), so no adjusted series enters the
  pipeline today.
- The native backtest adapter does not dispatch `on_corporate_action` at all,
  so a strategy cannot even observe the delisting and rename facts it does
  model through the Python hook. See
  `docs/strategy-authoring-guide.md`, section 10.
- The repository contains no split, dividend, or adjustment-factor table or
  ingestion job.

**Prototype.** `scripts/realism/corporate_actions_adjusted_vs_unadjusted.py`
computes the same Apple session window twice around the known 4-for-1 split of
2020-08-31: once as traded (unadjusted), once with the pre-split sessions
divided by the split factor (adjusted). Its deterministic output:

```text
session       unadjusted    adjusted     raw return   adj return
2020-08-24     503.43   125.8575           --          --
2020-08-25     499.30    124.825       -0.82%      -0.82%
2020-08-26     506.09   126.5225       +1.36%      +1.36%
2020-08-27     500.04     125.01       -1.20%      -1.20%
2020-08-28     499.23   124.8075       -0.16%      -0.16%
2020-08-31     129.04     129.04      -74.15%      +3.39%
2020-09-01     134.18     134.18       +3.98%      +3.98%
2020-09-02     131.40     131.40       -2.07%      -2.07%
2020-09-03     120.88     120.88       -8.01%      -8.01%
2020-09-04     120.96     120.96       +0.07%      +0.07%

split session: 2020-08-31 factor 4
split-day return: unadjusted -74.15% | adjusted +3.39%
window return:    unadjusted -75.97% | adjusted -3.89%
SMA3/SMA5 signal at split: flat | long
```

The divergence is material at every level: the split-day return is off by
77.54 percentage points, the ten-session window return is off by 72.08
percentage points, and the 3/5 moving-average regime is inverted (`flat` on the
raw series versus `long` on the adjusted series) on the split session. A
momentum, mean-reversion, or stop-based strategy fed the raw series would take
the wrong side or trigger a spurious liquidation. The prototype is covered by
four focused tests: the spurious split-day return, the cumulative divergence,
the signal flip, and adjustment determinism.

**Cost-benefit.** Cost is 3-5 engineering days for a useful slice: ingest a
split/adjustment-factor table, derive split-adjusted research series, and add a
split event that keeps positions, cost basis, resting orders, and subscriptions
continuous across the ex-date (the engine contract already has the shape for
`on_corporate_action`). Benefit is the removal of a catastrophic, systematic
error class that affects nearly every US large-cap over a multi-year window:
splits are common and dividends are near-universal, so any equity strategy with
a holding period beyond a quarter is exposed. Doing nothing leaves research
returns, indicator values, and PnL wrong by the adjustment factor at every
corporate action, which is unrecoverable after the fact without re-deriving the
series.

**Recommendation.** Adopt for v1.x as P1. Ship the minimal slice first: split
factors plus continuous back-adjustment for research, and split/position
continuity in the engine. Defer dividend tax-lot precision, withholding, and
index-level adjustment to a later pass. Add a golden artifact for a known split
so the adjustment stays deterministic.

## S2. Borrow, short-sale availability, and financing

**Scope.** The harness accepts negative equity positions with no availability
check, no locate cost, and no borrow financing accrual. A strategy can short an
unlimited quantity of any symbol for free.

**Evidence.**

- No borrow, locate, short-availability, or financing term appears anywhere in
  the Rust engine or Python surface.
- Strategy book accounting is symmetric for buys and sells
  (`src/harness/host/src/context.rs:200`), so a sell with no position simply
  books a negative position.
- The only short-specific constraint is the pessimistic delisting path, which
  raises `UnknownDelistingPriceError` when a short's final price is unknown
  (`src/harness/data/listing_events.py:200`).
- Options shorts are collateralized (`src/harness/live/src/risk/collateral.rs`),
  but equity shorts are not.

**Cost-benefit.** Cost is 5-8 engineering days to add a borrow-availability
port (per-symbol shares available and a locate flag), a per-symbol borrow rate,
rejection of short opens that exceed availability, and daily financing accrual
into cash. Benefit is realism for short, market-neutral, and hedge strategies,
and it closes a gap that can make an unprofitable short look profitable. The
risk of not doing it is optimistic, capacity-free short backtests that
overstate achievable returns.

**Recommendation.** Spike the borrow port and rate model now (1 day), but
implement it after S1 and S3, which affect every strategy rather than only
short ones. Priority P2.

## S3. Partial fills, displayed depth, and latency

**Scope.** The fill models constrain size by displayed quantity and support a
partial-fill remainder policy, but response latency is inert metadata, there is
no queue position or level depth, and the strategy-side cancel/replace path is
not wired.

**Evidence.**

- `LatencySizeConfig.response_latency_us` is documented as metadata "consumed
  by an execution adapter; it does not alter the decision snapshot"
  (`src/harness/native/src/latency_size.rs:20`); it is never used to schedule a
  delayed response.
- `FillModelOptions.response_latency_ns` defaults to zero and the default
  `SimpleTouch` plugin declares zero latency
  (`src/harness/backtest/src/backtest/fills.rs:59`).
- `displayed_size_constrained` is always true for latency-size, but there is no
  depth curve behind the top-of-book size.
- `orders.cancel` is a no-op and `orders.replace` submits a new order without
  cancelling the original (`src/harness/host/src/context.rs:152`), so the
  cancel/partial-fill pattern in `strategies/harness_examples/mean_reversion.py`
  cannot execute against the native context.

**Cost-benefit.** Cost is 3-5 engineering days: schedule order responses by
`response_latency_ns` inside the venue, implement cancel and true replace in the
native order sink, and extend the size model with a deterministic depth curve
(level prices and sizes) instead of a single displayed size. Benefit is
fidelity for any strategy whose edge depends on queue priority, partial
execution, or reaction time, and it makes the shipped cancel/replace example
meaningful. Not doing it overstates fill certainty on resting limit orders and
makes latency-sensitive strategies look better than they are.

**Recommendation.** P1. Latency scheduling and cancel/replace are contract
correctness fixes, not optional realism; do them with S4. Keep the depth model
deterministic (seeded through the context RNG is not acceptable; use a fixed
size curve) so the golden tests stay stable.

## S4. Slippage and market impact

**Scope.** There is no slippage or market-impact model. A market order fills at
the visible touch, and a resting limit fills at its limit or the trade-through,
with no spread cost beyond the touch and no participation-based impact for
size.

**Evidence.**

- A repository search for slippage, impact, and spread-cost terms across the
  Rust engine returns no model.
- `simple_touch` and `latency_size` price fills off the visible quote side
  (`src/harness/native/src/latency_size.rs:7`); the fee model is the only
  cost after the price.
- The backtest fee path derives commission and a notional-bps component, but
  hard-codes TAF to zero and does not read the richer standalone schedule
  (`src/harness/backtest/src/backtest/fees.rs:358`). `config/fees/us-retail.yaml`
  carries SEC/TAF values but is referenced by no loader.

**Cost-benefit.** Cost is 3-5 engineering days for a deterministic first model:
a half-spread cost on marketable orders, a fixed or square-root participation
impact proportional to order size over displayed depth, and a fee schedule
loader that consumes the existing `us-retail` fixture (including SEC and TAF).
Benefit is preventing size-blind backtests from claiming capacity that does not
exist and preventing high-turnover strategies from ignoring spread and
regulatory costs. The cost of not doing it is systematically optimistic
performance for any strategy that trades more than the touch and for sizes that
move the book.

**Recommendation.** P1. Ship spread plus a small participation term first,
behind an explicit fill-model option so existing golden runs are unchanged
until the model is opted into; wire the fee schedule loader in the same pass.

## Cross-cutting constraints

- Every prototype and model must stay deterministic: no wall-clock, no unseeded
  randomness, no network inside the synchronous kernel. Seeded randomness is
  available only through `context.random`.
- Realism features belong behind explicit configuration, so the existing
  deterministic golden artifacts and parity tests stay stable by default.
- The v1 split, borrow, latency, and impact gaps are documented against the
  implemented contract in `.github/architecture/repository.md` and
  `docs/strategy-authoring-guide.md`.

## Out of scope

- Tick-level order-book reconstruction and queue-position simulation.
- Tax-lot accounting for dividends and corporate-action cost-basis changes.
- Cross-asset financing (crypto funding, FX carry).

## Residual

- The split prototype demonstrates the divergence with an embedded, rounded
  window; it is evidence of the gap, not a production adjustment implementation.
- `config/fees/us-retail.yaml` remains unwired; S4 notes it as the natural
  input for the fee-schedule loader.
- None of these spikes change engine behavior; they are scoped for follow-up
  tasks.
