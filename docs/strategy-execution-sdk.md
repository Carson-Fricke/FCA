# Strategy execution SDK

Strategies supply signals and execution policy. The harness owns order identities,
order lifecycle, partial fills, inventory, cash, fees, exposure and financial returns.
Read the serving `execution` and `program` SDK references before authoring.

## Managed inventory

Inside `on_bar`, `on_quote` or `on_trade`, use the `OrderSink` context:

```rust
// Positive inventory is long; negative inventory is short.
ctx.target_position("MSFT", Decimal::from_i128(10)).expect("target request");
ctx.close_position("MSFT").expect("close request");
```

These are alternative policy actions, not a sequence to execute together.
Repeated targets do not duplicate pending entry or exit orders. A changed target
cancels conflicting working exposure and reconciles after already scheduled fills
have booked. Partial fills remain actual inventory until their closing fills book.
Managed targets use market/GTC orders; the strategy chooses when to set or close
the target. A risk rejection is retained as an order outcome and stops automatic
target retries. Another explicit target request is a new policy action.

Do not maintain `pending_entry`, assumed inventory, an exit-size accumulator or a
cash ledger to implement these operations. A strategy can still retain its signal
state, entry rationale, holding-time rule or desired exposure.

## Explicit orders

For limit/stop execution policy, use `ctx.place` with a typed constructor:

```rust
let order_id = ctx.place(Order::limit("MSFT", Side::Buy, quantity, limit_price)
    .with_time_in_force(TimeInForce::Gtc)).expect("order request");
ctx.cancel(&order_id).expect("cancel request");
```

`Order::market`, `Order::limit`, `Order::stop` and `Order::stop_limit` specify the
required prices without manual flags, identities, event IDs or sequences. The
supported time-in-force policies are `Day`, `Gtc` and `Ioc`. Day orders expire at
the acquired exchange session close; GTC orders remain working; IOC cancels its
unfilled remainder after its initial execution opportunity. Stop-limit activation
remains armed until its limit can execute. Invalid orders and risk rejection do
not create fills. Orders and cancellation are scoped to the owning strategy.
Placing an explicit order ends the managed target for that symbol; already
requested cancellations still complete. A later target request resumes management.

A cancel cannot erase a fill already scheduled by the venue. The harness waits
for that fill and cancels the unfilled remainder. A target change uses the same
rule before submitting the remaining inventory adjustment.

## Booked state and returns

`ctx.execution()` is an immutable view of this strategy instance at the current
causal boundary. It exposes:

- `quantity(symbol)`, position average price, realized/unrealized P&L and marked value;
- allocated initial cash, current cash, actual fees, equity and gross exposure.

`ctx.orders()` / `ctx.order(id)` expose owned order status, booked filled quantity,
remaining quantity and rejection reason. `ctx.closed_trades()` supplies canonical
FIFO slices with exact allocated fees, cash P&L and entry-notional
`gross_return_bps` / `net_return_bps`. These histories are shared by the engine,
serialized when they change, and decoded by the SDK only when requested. Reading
current position accounting does not copy or parse the entire trading history.

The final view is available through `MetricPublisher.execution()` in `on_stop`.
`MetricPublisher.orders()` and `closed_trades()` provide the final histories.
It is absent for observation-only studies. Financial values remain exact `Decimal`
values; convert once at a statistical or metric boundary. An unmarked open position
has explicitly unavailable marked values. Submission does not change the callback's
booked snapshot, and a pending scheduled fill is not reported as booked inventory.

Closed trades use the same FIFO matcher and fee allocation as `metrics.json`.
Each matched partial close is a trade slice, not necessarily an entire flat-to-flat
episode. Virtual-book realized P&L uses average cost and recognizes fees immediately;
FIFO slices allocate opening fees to their matched quantity. Their partition can
differ while inventory is open, and their totals reconcile when flat. Do not pair
fills or reconstruct either convention in a strategy.

`ExecutionSnapshot.account_return()` divides the change in equity by allocated
initial capital. `capital_utilization()` divides gross marked exposure by that
allocation. These are distinct from a trade's return on filled entry notional.
These helpers return `Result<Option<Decimal>, DecimalError>`: missing marks or
nonpositive allocated capital are unavailable, and arithmetic errors remain errors.
Final job metrics and HTML reports include initial capital, final cash/equity and
peak gross exposure across the complete causal valuation history. A one-share
prototype on a million-dollar account therefore displays its small capital use.

Custom strategy metrics remain observations. They cannot replace canonical
financial quantities or fill in an undefined canonical statistic.

## Current contract

SDK ABI 8 exposes execution snapshots and managed actions through demand-driven
execution queries. Account and history views are serialized only when requested;
unchanged order and closed-trade histories retain their serialized caches across
callbacks. Each account view uses the current immutable callback boundary.
Loader identity and SDK
content hashes require rebuilding and revalidating authored artifacts. There is
one current ABI; old binaries are rejected rather than interpreted with a new layout.
Historical immutable job evidence is not rewritten.

The behavior fixtures are `research/tests/fixtures/managed_strategy.rs` and
`research/tests/managed_execution.rs`. They compile an authored strategy without
any fill ledger, exercise long/short partial fills, cancellation and all four order constructors,
and reconcile its SDK observations to the published backtest accounting.
