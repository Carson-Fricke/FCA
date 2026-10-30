# fills

This capability follows the ownership boundaries in `.github/architecture/repository.md`.

- `decision.rs`: One exact fill decision and the immutable market event that caused it.
- `fees.rs`: Exact, configuration-driven fee calculation and aggregation.
- `latency_size.rs`: Deterministic latency and displayed-size constrained fill decisions.
- `mid_touch.rs`: Pessimistic midpoint fill decisions for quoted option markets.
- `mod.rs`: Deterministic fill and fee models.
- `trades.rs`: Pure checked FIFO fill matching, exact fee allocation and
  entry-notional return calculations shared by SDK execution views and final
  backtest publication. Callers own booked fill identity and ordering.
- `simple_touch.rs`: Deterministic top-of-book touch decisions for single-leg orders.
- `trade_through.rs`: Strict deterministic trade-print fill decisions.
